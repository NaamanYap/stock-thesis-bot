"""Format pitch output for Telegram (HTML parse mode)."""

from __future__ import annotations

from html import escape

import config
import history
from market_data import MarketData
from pitch import Pitch, street_view
from valuation import pct

TG_LIMIT = 4096
RATING_ICON = {"BUY": "\U0001F7E2", "HOLD": "\U0001F7E1", "SELL": "\U0001F534"}


def summary_card(pitch: Pitch, data: MarketData) -> str:
    """The at-a-glance message that lands in chat ahead of the PDF."""
    rating = str(pitch.get("recommendation", "")).upper()
    icon = RATING_ICON.get(rating, "⚪")
    currency = pitch.get("currency") or data.currency
    current = pitch.get("current_price") or data.price
    intrinsic = pitch.get("intrinsic_price")
    upside = pitch.get("upside_pct")

    lines = [
        f"<b>{escape(pitch.get('company') or data.name)}</b>",
        f"<code>{escape(data.exchange or pitch.get('exchange', ''))}: "
        f"{escape(pitch.get('ticker') or data.symbol)}</code>",
        "",
        f"Current      <b>{currency} {current:,.2f}</b>" if current else "Current      n.a.",
    ]
    if intrinsic:
        lines.append(f"Intrinsic    <b>{currency} {intrinsic:,.2f}</b>")
    if isinstance(upside, (int, float)):
        lines.append(f"Upside       <b>{pct(upside)}</b>")
    conviction = pitch.get("conviction")
    lines.append(f"Rating       {icon} <b>{escape(rating)}</b>"
                 + (f"  <i>({escape(str(conviction))} conviction)</i>" if conviction else ""))

    street = street_view(data)
    if street:
        lines += ["", "<b>Street</b>"]
        lines += [f"{escape(who)}: {escape(what)}" for who, what in street]

    if pitch.writer and pitch.writer != config.MODEL:
        lines += [f"✍️ Written by backup model <code>{escape(pitch.writer)}</code>"]

    v = pitch.valuation
    if v is not None and v.implied_cagr is not None and v.model_cagr is not None:
        lines += ["", f"Price assumes <b>{v.implied_cagr:.1%}</b> revenue growth a year to "
                      f"{escape(v.rows[-1].label)}; the model has <b>{v.model_cagr:.1%}</b>"]
    if pitch.history:
        lines += ["", "<b>Earlier runs</b>"]
        lines += [escape(l) for l in history.describe(pitch.history,
                                                      pitch.get("intrinsic_price"))]

    if pitch.checks:
        lines += ["", f"⚠ <b>{len(pitch.checks)} open issue"
                      f"{'s' if len(pitch.checks) != 1 else ''}</b> — listed at the end of the PDF"]
    if pitch.removed or pitch.unverified:
        lines += [f"✂ {len(pitch.removed)} sentence(s) removed, {pitch.unverified} figure(s) "
                  "marked unverified — see the PDF"]

    if pitch.review:
        n = len(pitch.review)
        what = (f"reviewer point{'s' if n != 1 else ''}</b> the revision was asked to address"
                if pitch.review_addressed else
                f"open reviewer issue{'s' if n != 1 else ''}</b>")
        lines += [f"\U0001F50D <b>{n} {what} — see Reviewer notes in the PDF"]

    if pitch.get("one_liner"):
        lines += ["", f"<i>{escape(pitch.get('one_liner'))}</i>"]

    pillars = pitch.get("thesis_pillars") or []
    if pillars:
        lines += ["", "<b>Thesis</b>"]
        lines += [f"{n}. {escape(str(p))}" for n, p in enumerate(pillars, 1)]

    risks = pitch.get("key_risks") or []
    if risks:
        lines += ["", "<b>Key risks</b>"]
        lines += [f"• {escape(str(r))}" for r in risks]

    lines += ["", "\U0001F4C4 Full thesis attached below."]
    return "\n".join(lines)


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split long text on paragraph, then line, then hard boundaries."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks

