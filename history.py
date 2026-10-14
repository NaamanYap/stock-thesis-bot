"""What earlier runs valued each ticker at, so a pitch can show how far its runs differ.

AVGO came out at USD 253.66 (SELL) on 2026-09-24 and USD 346.54 (HOLD) on 2026-10-01 with
the price within 1% both times: the forecast is chosen afresh each run, and a reader of one
PDF had no way to see that. Each pitch now lists the earlier runs of its ticker.

Runs live in three places, merged: `history_seed.json` in the repo (the runs before this
file existed, read off their PDFs), a file in out/ for runs on this machine, and a document
pinned in the Telegram chat (bot.py), because Render's disk is wiped on every deploy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date

import config

log = logging.getLogger("stockbot.history")

SEED = config.BASE_DIR / "history_seed.json"
FILENAME = "pitch_history.json"
SHOWN = 6           # earlier runs listed on a pitch


@dataclass
class Run:
    symbol: str
    date: str               # ISO date of the run
    currency: str
    price: float
    value: float            # the implied share price the pitch shipped with
    upside: float           # percent
    rating: str
    wacc: float | None = None


def from_pitch(result, data) -> Run | None:
    v = result.valuation
    if v is None:
        return None
    return Run(symbol=data.symbol, date=date.today().isoformat(), currency=data.currency,
               price=round(data.price, 2), value=round(v.per_share, 2),
               upside=round(v.upside_pct, 1), rating=v.rating, wacc=round(v.wacc, 4))


def parse(raw: bytes | str | None) -> list[Run]:
    """Runs from a JSON list; anything unreadable is skipped, never raised."""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return []
    runs = []
    for item in items if isinstance(items, list) else []:
        try:
            runs.append(Run(**{k: item.get(k) for k in Run.__dataclass_fields__}))
        except (TypeError, AttributeError):
            continue
    return [r for r in runs if r.symbol and r.date and r.value]


def dump(runs: list[Run]) -> bytes:
    return json.dumps([asdict(r) for r in runs], indent=1).encode("utf-8")


def merge(*lists: list[Run]) -> list[Run]:
    """All runs, one per ticker, date and value, oldest first."""
    seen, out = set(), []
    for run in (r for runs in lists for r in runs):
        key = (run.symbol.upper(), run.date, round(run.value, 2))
        if key not in seen:
            seen.add(key)
            out.append(run)
    return sorted(out, key=lambda r: (r.date, r.symbol))


def load_local() -> list[Run]:
    runs = []
    for path in (SEED, config.OUT_DIR / FILENAME):
        try:
            runs.append(parse(path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return merge(*runs)


def save_local(runs: list[Run]) -> None:
    try:
        config.OUT_DIR.mkdir(parents=True, exist_ok=True)
        (config.OUT_DIR / FILENAME).write_bytes(dump(runs))
    except OSError as exc:
        log.warning("could not save the run history: %s", exc)


def earlier(runs: list[Run], symbol: str) -> list[Run]:
    """The latest runs of `symbol` before this one, oldest first."""
    mine = [r for r in runs if r.symbol.upper() == symbol.upper()]
    return mine[-SHOWN:]


def describe(runs: list[Run], value: float | None) -> list[str]:
    """One line per earlier run, then how far apart the runs are."""
    lines = []
    for r in runs:
        try:
            when = date.fromisoformat(r.date).strftime("%d %b %Y")
        except ValueError:
            when = r.date
        lines.append(f"{when}: {r.currency} {r.value:,.2f} {r.rating} at a price of "
                     f"{r.price:,.2f}")
    gap = spread(runs, value) if value else None
    if gap:
        low, high = (f"{x:+.0f}%" for x in gap)
        lines.append((f"The earlier run's value is {low}" if len(runs) == 1 else
                      f"Earlier runs' values are {low}" if low == high else
                      f"Earlier runs' values are {low} to {high}")
                     + " against this one's: each run chooses its forecast afresh")
    return lines


def spread(runs: list[Run], value: float) -> tuple[float, float] | None:
    """Lowest and highest value of the earlier runs, as % against this one's. Not this
    run's own: ESLT's of 2026-10-07 read "from +0% to +9%" when both earlier runs were
    8-9% above it."""
    values = [r.value for r in runs]
    if not values or not value:
        return None
    return (min(values) / value - 1) * 100, (max(values) / value - 1) * 100
