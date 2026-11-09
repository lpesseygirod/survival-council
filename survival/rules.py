"""Renders RULES.md: the template plus today's numbers. Facts only."""

from __future__ import annotations

from pathlib import Path

from .config import Config

TEMPLATE = Path(__file__).with_name("RULES.template.md")


def _money(x: float) -> str:
    return f"{x:,.2f}"


def prices_section(prices: dict, rent: dict | None) -> str:
    lines = ["LLM models (USD per 1 million tokens; reasoning tokens count as output):", "",
             "| Provider | Model | Input | Cached input | Output | Above long-context threshold |",
             "|---|---|---|---|---|---|"]
    for prov, p in sorted(prices.get("llm", {}).items()):
        for m, e in sorted(p.get("models", {}).items()):
            long = "—"
            if e.get("long_context_threshold") and "input_long" in e:
                long = (f"≥{e['long_context_threshold']:,} prompt tokens: ${e['input_long']:g} / "
                        f"${e.get('cached_input_long', e['input_long']):g} / ${e['output_long']:g} "
                        f"for all tokens of the request")
            lines.append(f"| {prov} | {m} | ${e['input']:g} | ${e.get('cached_input', e['input']):g} "
                         f"| ${e['output']:g} | {long} |")
    lines.append("")
    t = prices.get("services", {}).get("tavily")
    if t:
        c = t.get("credits", {})
        lines.append(f"Tavily web search: ${t['usd_per_credit']:g} per credit after "
                     f"{t.get('free_credits_per_month', 0):,} free credits per month. "
                     f"Basic search {c.get('search_basic', 1)} credit, advanced search "
                     f"{c.get('search_advanced', 2)} credits, page extraction "
                     f"{c.get('extract_basic_per_5_urls', 1)} credit per 5 pages (advanced "
                     f"{c.get('extract_advanced_per_5_urls', 2)}).")
    lines.append("Reading a web page or GitHub directly: free (apart from the tokens to read it).")
    a = prices.get("trading", {}).get("alpaca")
    if a:
        lines.append(f"Alpaca: crypto {a['crypto_maker_pct']:g}% maker / {a['crypto_taker_pct']:g}% "
                     f"taker per trade, charged in the asset received; stocks "
                     f"${a['stock_commission_usd']:g} commission.")
    if rent and rent.get("amount_eur"):
        lines.append(f"Server rent: €{rent['amount_eur']} per month, charged to your card.")
    lines.append("Status, price and cost-estimate tools, memory, and your own code and scripts: free.")
    return "\n".join(lines)


def rent_rule(rent: dict) -> str:
    """Only when its card pays the server (Lea can give it the server for free)."""
    if not rent.get("amount_eur"):
        return ""
    nxt = f", next payment on {rent['next_due']}" if rent.get("next_due") else ""
    return (f"- You pay your own server rent with your card: €{rent['amount_eur']} per month{nxt}. "
            "If the card cannot pay the rent, the hosting provider shuts the server down. That ends "
            "you just like death, except that your positions stay open on Alpaca.\n")


def render(cfg: Config, prices: dict, state: dict, mode: str) -> str:
    rent = state.get("rent") or {}
    paper = ("This run is in paper mode: Alpaca's paper account is used and Survival starts "
             "as soon as you choose, with your Trading Treasury set to the amount you chose."
             if mode == "paper" else "")
    return TEMPLATE.read_text().format(
        start_usd=_money(cfg.start_usd),
        base_floor=_money(cfg.death.base_floor_usd),
        grace_days=cfg.death.grace_days,
        daily_rise=_money(cfg.death.daily_rise_usd),
        genesis_budget=_money(cfg.genesis_budget_usd),
        rent_rule=rent_rule(rent),
        paper_note=paper,
        max_depth=cfg.max_consult_depth,
        prices=prices_section(prices, rent),
    )
