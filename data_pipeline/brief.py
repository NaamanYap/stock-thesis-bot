"""What the analyst reads: a selection of the fact database, grouped, and the written notes.

The database holds thousands of facts; the analyst is shown the ones an analyst works
with (results, trends, segments, balance sheet, guidance, valuation and its history),
a few hundred lines, each with its id. Anything it cites must be one of these ids, so
the selection is also the boundary of what the report can state.
"""

from __future__ import annotations

import re

import config
from .facts_db import Fact, FactDB
from .periods import Period, fiscal_period

ANNUAL = ["revenue", "revenue_growth", "gross_margin", "operating_income", "operating_margin",
          "operating_income_growth", "ebitda", "ebitda_margin", "net_income", "net_margin",
          "diluted_eps", "diluted_eps_growth", "operating_cash_flow", "capex",
          "capex_to_revenue", "d_and_a", "d_and_a_to_revenue", "fcf", "fcf_margin",
          "fcf_conversion", "sbc_to_revenue", "rd_to_revenue", "tax_rate", "buybacks",
          "dividends_paid", "shareholder_returns", "acquisitions", "diluted_shares", "roic",
          "roic_ex_goodwill", "interest_expense"]
CAGRS = [f"{m}_cagr_{n}y" for m in ("revenue", "operating_income", "net_income", "diluted_eps",
                                     "ebitda", "fcf") for n in (3, 5)]
QUARTER = ["revenue", "revenue_growth", "operating_income", "operating_margin",
           "operating_income_growth", "net_income", "diluted_eps", "diluted_eps_growth",
           "ebitda", "ebitda_margin", "fcf", "operating_cash_flow", "capex"]
BALANCE = ["cash", "short_term_investments", "total_debt", "net_debt", "equity", "goodwill",
           "intangibles", "minority_interest", "redeemable_minority_interest",
           "invested_capital", "shares_outstanding", "rpo", "inventory", "receivables"]
MARKET = ["share_price", "market_cap", "market_cap_reporting", "fx_rate", "enterprise_value",
          "ev_to_ebitda", "ev_to_revenue", "pe", "fcf_yield", "net_debt_to_ebitda",
          "interest_cover", "street_target_mean", "street_upside", "street_analysts", "beta",
          "risk_free_rate"]
HISTORY = [f"{m}_hist_{s}" for m in ("ev_to_ebitda", "ev_to_revenue", "pe")
           for s in ("low", "median", "high")]
# Other figures worth an analyst's eye, by the words in their name.
NOTABLE = re.compile(r"organic|adjusted|backlog|bookings|order|subscriber|users|customer|"
                     r"retention|recurring|arr\b|annual recurring|remaining performance|"
                     r"same store|comparable|average revenue per|arpu|units|deliveries|"
                     r"employees|headcount|free cash flow|guidance|pro forma revenue", re.I)
MAX_NOTABLE = 45
NOTES_CHARS = 48000
NOTE_CHARS = {"MD&A": 12000, "release": 7000, "10-K": 8000, "annual report": 8000,
              "deal terms": 3000, "events": 1500, "insiders": 2000, "search": 3000,
              "news": 3000}
NOTE_ORDER = ["MD&A", "release", "10-K", "annual report", "deal terms", "events", "insiders",
              "search", "news"]


def _ids(db: FactDB, facts: list[Fact | None]) -> list[str]:
    return [f.id for f in facts if f is not None and f.usable]


def base_year(db: FactDB) -> Period | None:
    """The last reported fiscal year: the base the forecast grows from."""
    f = db.latest("revenue", "12M")
    return f.period if f else None


def forecast_years(db: FactDB) -> list[str]:
    base = base_year(db)
    if base is None:
        return []
    fy = base.fiscal_year()
    return [fiscal_period("12M", fy + i, 4, db.fy_end).label
            for i in range(1, config.FORECAST_YEARS + 1)]


def select(db: FactDB) -> dict[str, list[str]]:
    """The facts the analyst sees, by group heading."""
    groups: dict[str, list[str]] = {}
    groups["Market and valuation today"] = [m for m in MARKET if db.get(m)]
    years = [f.period for f in db.series("revenue", "12M")][-4:]
    hist = [m for m in HISTORY if db.get(m)]
    for p in years:
        hist += _ids(db, [db.one(f"{m}_hist", p) for m in ("ev_to_ebitda", "ev_to_revenue", "pe")])
    groups["The stock's own valuation history (fiscal year ends)"] = hist
    annual = []
    for p in years:
        annual += _ids(db, [db.one(m, p) for m in ANNUAL])
    annual += [f.id for f in db.facts.values() if f.metric in CAGRS and f.usable]
    groups["Annual results (fiscal years)"] = list(dict.fromkeys(annual))
    ttm = [db.latest(m, "TTM") for m in ANNUAL]
    groups["Trailing twelve months"] = _ids(db, ttm)
    quarters = db.series("revenue", "3M")
    q_ids: list[str] = []
    if quarters:
        latest = quarters[-1].period
        for p in (latest, latest.prior_year()):
            q_ids += _ids(db, [db.one(m, p) for m in QUARTER])
        # The trend: each of the last four quarters' revenue growth and margin.
        for f in quarters[-5:-1]:
            q_ids += _ids(db, [db.one("revenue_growth", f.period),
                               db.one("operating_margin", f.period)])
        ytd = [db.latest(m, s) for s in ("6M", "9M") for m in ("revenue", "revenue_growth")]
        q_ids += [f.id for f in ytd if f and f.period.end == latest.end]
    groups["Latest quarters"] = list(dict.fromkeys(q_ids))
    seg_ids: list[str] = []
    for axis, names in db.segments("revenue").items():
        for name in names:
            for span in ("3M", "12M"):
                latest = db.latest("revenue", span, name)
                if latest is None:
                    continue
                # Segments no longer reported (sold, spun off) are history, not the business.
                group_latest = db.latest("revenue", span)
                if group_latest and latest.period.end < group_latest.period.end:
                    continue
                seg_ids += _ids(db, [latest, db.one("revenue_growth", latest.period, name),
                                     db.one("revenue_share", latest.period, name),
                                     db.one("operating_income", latest.period, name)])
    groups["Segments, products and regions"] = seg_ids
    balance = [f for f in (db.latest(m, "at") for m in BALANCE) if f]
    groups["Balance sheet (latest)"] = _ids(db, balance)
    groups["Management guidance"] = [f.id for f in db.facts.values()
                                     if f.usable and f.data_type == "guidance"]
    shown = {i for ids in groups.values() for i in ids}
    notable = []
    latest_q = db.latest("revenue", "3M")
    newest = latest_q.period.key[1:] if latest_q else None
    for f in db.facts.values():
        if (f.usable and f.id not in shown and not f.segment and f.period.end
                and f.period.key[1:] == newest and f.period.span in ("3M", "at")
                and f.source_kind in ("sec_inline", "filing_table") and NOTABLE.search(f.label)):
            notable.append(f.id)
    # A row name the table repeats (one per segment, unnamed) cannot be told apart.
    repeated = {re.sub(r"_\d+$", "", i) for i in notable if re.search(r"_\d+$", i)
                and db.facts[i].source_kind == "filing_table"}
    notable = [i for i in notable if re.sub(r"_\d+$", "", i) not in repeated]
    groups["Other figures from the latest filing and release"] = notable[:MAX_NOTABLE]
    return {k: v for k, v in groups.items() if v}


def facts_text(db: FactDB, groups: dict[str, list[str]]) -> str:
    out = ["Each line: id | what it is | period | value | source. Cite a fact only by its id."]
    for heading, ids in groups.items():
        out.append(f"\n## {heading}\n")
        out.append(db.table(ids))
    if db.conflicts:
        out.append("\n## Sources that disagree (the figure shown above is the one kept)\n")
        out += [f"- {c.fact_id}: {c.note}" for c in db.conflicts[:20]]
    excluded = [f for f in db.facts.values() if not f.usable]
    if excluded:
        out.append("\n## Excluded figures (sources disagree; do not rely on them)\n")
        out += [f"- {f.id}: {f.excluded}" for f in excluded[:20]]
    if db.gaps:
        out.append("\n## Gaps in the data\n")
        out += [f"- {g}" for g in db.gaps]
    return "\n".join(out)


def notes_text(notes: list) -> str:
    """The written notes, company sources first, each clipped, within NOTES_CHARS."""
    ordered = sorted(notes, key=lambda n: NOTE_ORDER.index(n.kind) if n.kind in NOTE_ORDER
                     else len(NOTE_ORDER))
    out, total = [], 0
    for n in ordered:
        limit = NOTE_CHARS.get(n.kind, 3000)
        text = n.text[:limit]
        tag = "company source" if n.primary else "second-hand: not a fact source"
        block = f"### [{tag}] {n.title}\n{n.url}\n\n{text}".rstrip()
        if total + len(block) > NOTES_CHARS:
            continue
        out.append(block)
        total += len(block)
    return "\n\n".join(out)


def header(db: FactDB) -> str:
    base = base_year(db)
    years = forecast_years(db)
    lines = [f"Company: {db.entity} ({db.symbol}). Reporting currency: {db.currency}"
             f"{f'; shares trade in {db.trading_currency}' if db.trading_currency != db.currency else ''}."
             f" Fiscal year ends in month {db.fy_end}."]
    if base:
        rev = db.one("revenue", base)
        lines.append(f"Base year: {base.label} (revenue {rev.id if rev else 'n.a.'}). Forecast "
                     f"years: {', '.join(years)}. Scenario arrays have {len(years)} values, "
                     f"one per forecast year, in that order.")
        ttm = db.latest("revenue", "TTM")
        if ttm and ttm.period.end > base.end:
            lines.append(f"Already reported of {years[0]}: up to {ttm.period.label} "
                         f"({ttm.id}). Your {years[0]} growth should start from what is "
                         f"already reported.")
    return "\n".join(lines)
