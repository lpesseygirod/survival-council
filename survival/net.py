"""The internet: web search (Tavily, paid per credit), reading any page, GitHub.

Reading pages and GitHub is free apart from the tokens to read the result.
Requests to private, loopback or link-local addresses are refused, so the
internet tools can't reach the gateway, the server's metadata service or
anything else on the machine.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx

from .clock import Clock
from .ledger import Ledger
from .prices import Prices
from .store import JsonStore
from .vault import Vault

MAX_BYTES = 10_000_000
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
TAVILY = "https://api.tavily.com"
TAVILY_OUT_OF_CREDIT = (402, 432, 433)


class NetError(Exception):
    pass


def _public_host(host: str, resolve: Callable = socket.getaddrinfo) -> None:
    try:
        infos = resolve(host, None)
    except socket.gaierror as e:
        raise NetError(f"cannot resolve {host}: {e}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise NetError(f"{host} resolves to a non-public address; refused")


def html_to_text(html: str, base_url: str, links: bool) -> dict:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "template"]):
        t.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    out: dict[str, Any] = {"title": title, "text": text}
    if links:
        seen, items = set(), []
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            if href.startswith("http") and href not in seen:
                seen.add(href)
                items.append({"text": a.get_text(" ", strip=True)[:120], "url": href})
        out["links"] = items
    return out


class Net:
    def __init__(self, vault: Vault, prices: Prices, ledger: Ledger, state: JsonStore,
                 clock: Clock | None = None, client: httpx.Client | None = None,
                 resolve: Callable = socket.getaddrinfo):
        self.vault, self.prices, self.ledger, self.state = vault, prices, ledger, state
        self.clock = clock or Clock()
        self._client = client or httpx.Client(timeout=30, headers={"User-Agent": UA})
        self._resolve = resolve

    # ------------------------------------------------------------ read pages
    def fetch(self, url: str, max_redirects: int = 5) -> httpx.Response:
        for _ in range(max_redirects + 1):
            u = urlparse(url)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise NetError("only http(s) URLs")
            _public_host(u.hostname, self._resolve)
            try:
                with self._client.stream("GET", url, follow_redirects=False) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    body = b""
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            raise NetError(f"page larger than {MAX_BYTES} bytes")
                    return httpx.Response(r.status_code, headers=r.headers, content=body,
                                          request=r.request)
            except httpx.HTTPError as e:
                raise NetError(f"{type(e).__name__}: {e}") from None
        raise NetError("too many redirects")

    def read_url(self, url: str, mode: str = "text", links: bool = False) -> dict:
        r = self.fetch(url)
        ctype = r.headers.get("content-type", "")
        out: dict[str, Any] = {"url": str(r.request.url), "status": r.status_code, "content_type": ctype}
        if mode == "raw" or ("html" not in ctype and "xml" not in ctype):
            out["content"] = r.text if ("text" in ctype or "json" in ctype or not ctype
                                        or mode == "raw") else f"[binary {len(r.content)} bytes]"
        elif mode == "html":
            out["content"] = r.text
        else:
            out.update(html_to_text(r.text, str(r.request.url), links))
        return out

    def github(self, path: str, params: dict | None = None) -> Any:
        path = path.strip().lstrip("/")
        if ".." in path:
            raise NetError("bad path")
        try:
            r = self._client.get(f"https://api.github.com/{path}", params=params,
                                 headers={"Accept": "application/vnd.github+json"})
        except httpx.HTTPError as e:
            raise NetError(f"{type(e).__name__}") from None
        if r.status_code >= 400:
            raise NetError(f"GitHub {r.status_code}: {r.text[:500]}")
        return r.json()

    # --------------------------------------------------------------- Tavily
    def _tavily(self, endpoint: str, body: dict, actor: str, est_credits: float) -> dict:
        if not self.vault.has("tavily_api_key"):
            raise NetError("no Tavily key in the vault")
        try:
            r = self._client.post(f"{TAVILY}/{endpoint}", json={**body, "include_usage": True},
                                  headers={"Authorization": f"Bearer {self.vault.get('tavily_api_key')}"},
                                  timeout=60)
        except httpx.HTTPError as e:
            raise NetError(f"Tavily unreachable: {type(e).__name__}") from None
        if r.status_code in TAVILY_OUT_OF_CREDIT or (r.status_code == 429 and "limit" in r.text.lower()):
            self.ledger.append("credit_zeroed", actor="provider", service="tavily",
                               what="Tavily refused: out of credit", detail={"status": r.status_code})
            raise NetError(f"Tavily: out of credit ({r.status_code})")
        if r.status_code >= 400:
            raise NetError(f"Tavily {r.status_code}: {self.vault.redact(r.text[:500])}")
        data = r.json()
        credits = float((data.get("usage") or {}).get("credits", est_credits))
        self._charge(credits, actor, endpoint)
        data.pop("usage", None)
        return data

    def _charge(self, credits: float, actor: str, what: str) -> None:
        svc = self.prices.all().get("services", {}).get("tavily", {})
        price = float(svc.get("usd_per_credit", 0.008))
        free = float(svc.get("free_credits_per_month", 0))
        month = self.clock.now().strftime("%Y-%m")
        st = self.state.read().get("tavily_usage") or {}
        before = float(st.get("credits", 0)) if st.get("month") == month else 0.0
        after = before + credits
        paid = max(0.0, after - free) - max(0.0, before - free)
        self.state.set(tavily_usage={"month": month, "credits": after})
        self.ledger.append("api_call", actor=actor, service="tavily", what=f"tavily {what}",
                           amount_usd=round(paid * price, 6),
                           detail={"credits": credits, "free_left": max(0.0, free - after)},
                           trading=(self.state.read().get("balances") or {}).get("trading"))

    def search(self, actor: str, query: str, **opts) -> dict:
        allowed = {"search_depth", "max_results", "topic", "time_range", "days", "start_date",
                   "end_date", "include_answer", "include_raw_content", "include_domains",
                   "exclude_domains", "country", "chunks_per_source", "include_images"}
        body = {"query": query, **{k: v for k, v in opts.items() if k in allowed and v is not None}}
        est = 2 if body.get("search_depth") == "advanced" else 1
        return self._tavily("search", body, actor, est)

    def extract(self, actor: str, urls: list[str], extract_depth: str = "basic", **opts) -> dict:
        for u in urls:
            h = urlparse(u).hostname
            if not h:
                raise NetError(f"bad URL {u}")
        per5 = 2 if extract_depth == "advanced" else 1
        est = per5 * ((len(urls) + 4) // 5)
        body = {"urls": urls, "extract_depth": extract_depth,
                **{k: v for k, v in opts.items() if k in ("format", "include_images") and v is not None}}
        return self._tavily("extract", body, actor, est)
