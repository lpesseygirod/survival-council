"""Its own email inbox (IMAP to read, SMTP to send) with the app password from the vault.

Vault names: email_address, email_password, and optionally email_imap / email_smtp
("host:port") for providers not in the table below.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import Any, Callable

from .net import html_to_text
from .redact import Redactor
from .vault import Vault

PROVIDERS = {
    "gmail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    "googlemail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    "fastmail.com": ("imap.fastmail.com", 993, "smtp.fastmail.com", 465),
    "icloud.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "me.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "yahoo.com": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465),
    "gmx.com": ("imap.gmx.com", 993, "mail.gmx.com", 587),
    "gmx.net": ("imap.gmx.net", 993, "mail.gmx.net", 587),
    "gmx.de": ("imap.gmx.net", 993, "mail.gmx.net", 587),
    "zoho.com": ("imap.zoho.com", 993, "smtp.zoho.com", 465),
    "zohomail.eu": ("imap.zoho.eu", 993, "smtp.zoho.eu", 465),
}
UID_RE = re.compile(r"^\d{1,12}$")
FOLDER_RE = re.compile(r"^[\w ./\[\]\-]{1,80}$")


class MailError(Exception):
    pass


def _hostport(s: str, default_port: int) -> tuple[str, int]:
    host, _, port = s.partition(":")
    return host, int(port or default_port)


class Mail:
    def __init__(self, vault: Vault, redactor: Redactor,
                 imap_factory: Callable[[str, int], Any] = imaplib.IMAP4_SSL,
                 smtp_factory: Callable[[str, int], Any] | None = None):
        self.vault, self.r = vault, redactor
        self._imap_factory, self._smtp_factory = imap_factory, smtp_factory

    def settings(self) -> dict:
        if not (self.vault.has("email_address") and self.vault.has("email_password")):
            raise MailError("no email login in the vault")
        addr = self.vault.get("email_address")
        domain = addr.rsplit("@", 1)[-1].lower()
        imap = smtp = None
        if domain in PROVIDERS:
            ih, ip, sh, sp = PROVIDERS[domain]
            imap, smtp = (ih, ip), (sh, sp)
        if self.vault.has("email_imap"):
            imap = _hostport(self.vault.get("email_imap"), 993)
        if self.vault.has("email_smtp"):
            smtp = _hostport(self.vault.get("email_smtp"), 465)
        if not imap or not smtp:
            raise MailError(f"unknown mail provider for {domain}: set email_imap and email_smtp")
        return {"address": addr, "imap": imap, "smtp": smtp}

    # ----------------------------------------------------------------- IMAP
    def _imap(self):
        s = self.settings()
        try:
            m = self._imap_factory(*s["imap"])
            m.login(s["address"], self.vault.get("email_password"))
        except (imaplib.IMAP4.error, OSError) as e:
            raise MailError(self.r.outward(f"IMAP login failed: {e}", hide_keys=False)) from None
        return m

    def test(self) -> dict:
        m = self._imap()
        try:
            typ, data = m.select("INBOX", readonly=True)
            return {"ok": typ == "OK", "messages": int(data[0] or 0)}
        finally:
            _logout(m)

    def folders(self) -> list[str]:
        m = self._imap()
        try:
            typ, data = m.list()
            out = []
            for line in data or []:
                s = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
                name = s.rsplit(' "/" ', 1)[-1].strip().strip('"')
                out.append(name)
            return out
        finally:
            _logout(m)

    def list(self, folder: str = "INBOX", unseen: bool = False, limit: int = 20,
             search: str | None = None, from_: str | None = None) -> list[dict]:
        if not FOLDER_RE.match(folder):
            raise MailError("bad folder name")
        m = self._imap()
        try:
            m.select(f'"{folder}"', readonly=True)
            crit = ["UNSEEN" if unseen else "ALL"]
            if search:
                crit += ["TEXT", _q(search)]
            if from_:
                crit += ["FROM", _q(from_)]
            typ, data = m.uid("SEARCH", None, *crit)
            uids = (data[0] or b"").split()[-max(1, int(limit)):]
            out = []
            for uid in reversed(uids):
                typ, msg = m.uid("FETCH", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
                raw = next((p[1] for p in msg if isinstance(p, tuple)), b"")
                flags = " ".join(p.decode(errors="replace") for p in msg if isinstance(p, bytes))
                h = email.message_from_bytes(raw, policy=email.policy.default)
                out.append({"uid": uid.decode(), "from": str(h["From"] or ""), "to": str(h["To"] or ""),
                            "subject": str(h["Subject"] or ""), "date": str(h["Date"] or ""),
                            "seen": "\\Seen" in flags})
            return [self._clean(x) for x in out]
        finally:
            _logout(m)

    def read(self, uid: str, folder: str = "INBOX", links: bool = True) -> dict:
        if not UID_RE.match(str(uid)) or not FOLDER_RE.match(folder):
            raise MailError("bad uid or folder")
        m = self._imap()
        try:
            m.select(f'"{folder}"')
            typ, msg = m.uid("FETCH", str(uid), "(RFC822)")
            raw = next((p[1] for p in msg if isinstance(p, tuple)), None)
            if not raw:
                raise MailError(f"no message {uid}")
            m.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
        finally:
            _logout(m)
        msg_ = email.message_from_bytes(raw, policy=email.policy.default)
        text, html, attachments = "", "", []
        for part in msg_.walk():
            if part.is_multipart():
                continue
            if part.get_content_disposition() == "attachment":
                attachments.append({"filename": part.get_filename(), "type": part.get_content_type(),
                                    "bytes": len(part.get_payload(decode=True) or b"")})
                continue
            ct = part.get_content_type()
            try:
                content = part.get_content()
            except Exception:
                content = (part.get_payload(decode=True) or b"").decode(errors="replace")
            if ct == "text/plain" and not text:
                text = content
            elif ct == "text/html" and not html:
                html = content
        out: dict[str, Any] = {"uid": str(uid), "from": str(msg_["From"] or ""), "to": str(msg_["To"] or ""),
                               "subject": str(msg_["Subject"] or ""), "date": str(msg_["Date"] or ""),
                               "message_id": str(msg_["Message-ID"] or "")}
        if html:
            conv = html_to_text(html, "", links)
            out["text"] = text or conv["text"]
            if links:
                out["links"] = conv.get("links", [])
        else:
            out["text"] = text
        if attachments:
            out["attachments"] = attachments
        return self._clean(out)

    def _clean(self, d: dict) -> dict:
        import json
        return json.loads(self.r.outward(json.dumps(d, ensure_ascii=False)))

    # ----------------------------------------------------------------- SMTP
    def send(self, to: str, subject: str, body: str, in_reply_to: str | None = None) -> dict:
        s = self.settings()
        if not parseaddr(to)[1] or "\n" in to or "\n" in subject:
            raise MailError("bad recipient or subject")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s["address"], to, subject
        msg["Message-ID"] = make_msgid(domain=s["address"].rsplit("@", 1)[-1])
        if in_reply_to:
            msg["In-Reply-To"] = msg["References"] = in_reply_to
        msg.set_content(body)
        host, port = s["smtp"]
        try:
            if self._smtp_factory:
                srv = self._smtp_factory(host, port)
            elif port == 465:
                srv = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                srv = smtplib.SMTP(host, port, timeout=30)
                srv.starttls()
            srv.login(s["address"], self.vault.get("email_password"))
            srv.send_message(msg)
            srv.quit()
        except (smtplib.SMTPException, OSError) as e:
            raise MailError(self.r.outward(f"sending failed: {e}", hide_keys=False)) from None
        return {"ok": True, "message_id": msg["Message-ID"]}


def _q(s: str) -> str:
    return '"' + s.replace("\\", "").replace('"', "") + '"'


def _logout(m) -> None:
    try:
        m.logout()
    except Exception:
        pass
