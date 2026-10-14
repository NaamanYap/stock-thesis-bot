"""The company's own tagged figures from its SEC filings (XBRL company facts).

Every figure in a 10-K, 10-Q or 20-F is tagged by the company with what it is and the
period it covers, and the SEC serves them as JSON. Read here they need no reading at all:
the tag names the metric, its dates the period, its form the filing. In facts mode they go
into the research notes as a block of tables in the same row format as filing_tables.py,
so facts.py labels them the same way.

The market data block has statements too, from Yahoo; on Render, Yahoo's statements are
sometimes refused (HANDOFF.md). These come from the filer.
"""

from __future__ import annotations

import logging
from datetime import date

import config

log = logging.getLogger(__name__)

COMPANY_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
# (label, unit kind, [tags in order of preference]); us-gaap first, then IFRS for 20-F filers.
METRICS = [
    ("Revenue", "money", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                          "SalesRevenueNet", "Revenue"]),
    ("Cost of revenue", "money", ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfSales"]),
    ("Gross profit", "money", ["GrossProfit"]),
    ("Research and development", "money", ["ResearchAndDevelopmentExpense"]),
    ("Selling, general and administrative", "money", ["SellingGeneralAndAdministrativeExpense"]),
    ("Operating income", "money", ["OperatingIncomeLoss", "ProfitLossFromOperatingActivities"]),
    ("Interest expense", "money", ["InterestExpense", "InterestExpenseNonoperating"]),
    ("Income tax expense", "money", ["IncomeTaxExpenseBenefit", "IncomeTaxExpenseContinuingOperations"]),
    ("Net income", "money", ["NetIncomeLoss", "ProfitLoss"]),
    ("Diluted EPS", "per share", ["EarningsPerShareDiluted", "DilutedEarningsLossPerShare"]),
    ("Depreciation and amortization", "money", ["DepreciationDepletionAndAmortization",
                                                "DepreciationAndAmortization"]),
    ("Operating cash flow", "money", ["NetCashProvidedByUsedInOperatingActivities",
                                      "CashFlowsFromUsedInOperatingActivities"]),
    ("Capital expenditure", "money", ["PaymentsToAcquirePropertyPlantAndEquipment",
                                      "PaymentsToAcquireProductiveAssets",
                                      "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"]),
    ("Share repurchases", "money", ["PaymentsForRepurchaseOfCommonStock"]),
    ("Dividends paid", "money", ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock",
                                 "DividendsPaidClassifiedAsFinancingActivities"]),
    ("Cash and equivalents", "money", ["CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalents"]),
    ("Long-term debt", "money", ["LongTermDebt", "LongTermDebtNoncurrent", "NoncurrentPortionOfNoncurrentBorrowings"]),
    ("Total assets", "money", ["Assets"]),
    ("Shareholders' equity", "money", ["StockholdersEquity", "EquityAttributableToOwnersOfParent"]),
]


def cik_for(symbol: str) -> int | None:
    """Canonical SEC issuer lookup shared by XBRL, filings, and market-data currency."""
    import research_tools
    return research_tools.cik_for(symbol)
ANNUAL_YEARS = 3
QUARTERS = 4


def _days(entry: dict) -> int | None:
    if "start" not in entry:
        return None
    return (date.fromisoformat(entry["end"]) - date.fromisoformat(entry["start"])).days


def _pick(entries: list[dict], span: str, count: int) -> list[dict]:
    """The latest `count` periods of one length ("year", "quarter" or "instant"), each from
    the most recent filing that reports it."""
    by_end: dict[str, dict] = {}
    for e in entries:
        if e.get("form") not in ("10-K", "10-Q", "20-F", "40-F", "10-K/A", "10-Q/A"):
            continue
        days = _days(e)
        kind = ("instant" if days is None else "year" if 350 <= days <= 380
                else "quarter" if 80 <= days <= 100 else None)
        if kind != span:
            continue
        if e["end"] not in by_end or e.get("filed", "") > by_end[e["end"]].get("filed", ""):
            by_end[e["end"]] = e
    return [by_end[k] for k in sorted(by_end, reverse=True)[:count]]


def _stale_before(newest: str) -> str:
    """Fifteen months before the newest period any metric reports."""
    d = date.fromisoformat(newest)
    months = d.year * 12 + d.month - 1 - 15
    return f"{months // 12:04d}-{months % 12 + 1:02d}-01"


def _fmt(value: float, kind: str, currency: str) -> str:
    if kind == "per share":
        return f"{currency} {value:,.2f}"
    return f"{currency} {value / 1e6:,.0f}mn"


def _series(facts: dict, tags: list[str]) -> tuple[list[dict], str] | None:
    """The entries of the tag the company reports most recently, with their currency.
    Companies change tags: SPGI's capital expenditure stopped under one in 2009."""
    best = None
    for taxonomy in ("us-gaap", "ifrs-full"):
        for tag in tags:
            units = facts.get(taxonomy, {}).get(tag, {}).get("units", {})
            for unit, entries in units.items():
                currency = unit.split("/")[0]
                if len(currency) == 3 and currency.isupper() and entries:
                    latest = max(e["end"] for e in entries)
                    if best is None or latest > best[2]:
                        best = (entries, currency, latest)
    return (best[0], best[1]) if best else None


def _latest(entries: list[dict]) -> str:
    return max((e["end"] for e in entries), default="")


def render(company_facts: dict) -> str:
    """The annual, quarterly and balance sheet tables, in filing_tables.py's row format."""
    facts = company_facts.get("facts", {})
    tables = {"year": [], "quarter": [], "instant": []}
    series = {label: (_series(facts, tags), kind) for label, kind, tags in METRICS}
    newest = max((_latest(s[0]) for s, _ in series.values() if s), default="")
    for label, (found, kind) in series.items():
        # A metric the company stopped reporting is history, not a current figure.
        if not found or (newest and _latest(found[0]) < _stale_before(newest)):
            continue
        entries, currency = found
        for span, count in (("year", ANNUAL_YEARS), ("quarter", QUARTERS),
                            ("instant", ANNUAL_YEARS)):
            picked = _pick(entries, span, count)
            if picked:
                cells = "; ".join(f"{e['end']}: {_fmt(e['val'], kind, currency)} "
                                  f"({e['form']} filed {e['filed']})" for e in picked)
                tables[span].append(f"- {label} — {cells}")
    titles = {"year": "SEC XBRL facts, annual (fiscal years ending on the date shown)",
              "quarter": "SEC XBRL facts, quarterly (three months ending on the date shown)",
              "instant": "SEC XBRL facts, balance sheet (at the date shown)"}
    return "\n\n".join(f"### {titles[span]}\n" + "\n".join(rows)
                       for span, rows in tables.items() if rows)


COMPANY_CONCEPT = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{tag}.json"


def reporting_currency(symbol: str) -> str | None:
    """The currency the company reports revenue in, from its own tagged filings: the unit
    most of its revenue figures are in. None for a company that does not file, or on failure.

    Yahoo's quote summary carries this as financialCurrency, and from Render it is refused
    (render-network-blocks): SPOT's pitch of 2026-10-07 took its EUR statements for USD at
    an exchange rate of 1.0. One concept per request, a few kB each, rather than the
    company facts (megabytes) that the research step reads later."""
    import research_tools

    if not config.SEC_USER_AGENT:
        return None
    try:
        cik = cik_for(symbol)
    except Exception as exc:
        log.warning("SEC CIK lookup failed for %s: %s", symbol, exc)
        return None
    if cik is None:
        return None
    tags = next(t for label, _, t in METRICS if label == "Revenue")
    for taxonomy in ("us-gaap", "ifrs-full"):
        for tag in tags:
            try:
                units = research_tools._sec_get(COMPANY_CONCEPT.format(
                    cik=cik, taxonomy=taxonomy, tag=tag)).json().get("units", {})
            except Exception:
                continue        # 404: the company does not use this tag
            # The unit with the most figures: TSM tags its TWD revenue 26 times and a USD
            # convenience translation of the latest years 9 times, to the same end date.
            found = [(len(entries), unit) for unit, entries in units.items()
                     if len(unit) == 3 and unit.isupper() and entries]
            if found:
                return max(found)[1]
    return None


def _concept_latest(cik: int, taxonomy: str, tag: str) -> tuple[str, float] | None:
    """The latest (end date, value) of one tagged concept, in the unit most of its figures
    are in; None when the company does not use the tag."""
    import research_tools

    try:
        units = research_tools._sec_get(COMPANY_CONCEPT.format(
            cik=cik, taxonomy=taxonomy, tag=tag)).json().get("units", {})
    except Exception:
        return None             # 404: the company does not use this tag
    found = [entries for unit, entries in units.items()
             if len(unit) == 3 and unit.isupper() and entries]
    if not found:
        return None
    latest = max(max(found, key=len), key=lambda e: e.get("end", ""))
    return latest.get("end", ""), float(latest["val"])


def operating_lease_share(symbol: str) -> float | None:
    """The share of the company's lease liabilities that are operating leases, from its own
    US GAAP filings. None for an IFRS filer (it tags no operating leases), a company that
    does not file, or on failure.

    Under US GAAP an operating lease's cost is an operating expense, inside operating
    income and EBITDA; under IFRS 16 every lease is a borrowing whose cost is depreciation
    and interest. Yahoo adds all lease liabilities to Total Debt either way."""
    import research_tools

    if not config.SEC_USER_AGENT:
        return None
    try:
        cik = cik_for(symbol)
    except Exception as exc:
        log.warning("SEC CIK lookup failed for %s: %s", symbol, exc)
        return None
    if cik is None:
        return None
    operating = _concept_latest(cik, "us-gaap", "OperatingLeaseLiability")
    if operating is None or operating[1] <= 0:
        return None
    finance = _concept_latest(cik, "us-gaap", "FinanceLeaseLiability")
    finance_value = finance[1] if finance and finance[0] == operating[0] else 0.0
    return operating[1] / (operating[1] + max(finance_value, 0.0))


CYCLE_YEARS = 10


def margin_history(symbol: str, years: int = CYCLE_YEARS) -> list[tuple[str, float, float]]:
    """Annual revenue and operating income, (fiscal year end, revenue, operating income) in
    the filing's currency units, newest first, for up to `years` years: a cycle is longer
    than the four years the market data provider gives. [] for a non-SEC filer or on failure."""
    import research_tools

    if not config.SEC_USER_AGENT:
        return []
    try:
        cik = cik_for(symbol)
        if cik is None:
            return []
        facts = research_tools._sec_get(COMPANY_FACTS.format(cik=cik)).json().get("facts", {})
    except Exception as exc:
        log.warning("SEC XBRL margin history unavailable for %s: %s", symbol, exc)
        return []
    tags = dict((label, tags) for label, _, tags in METRICS)
    revenue = _series(facts, tags["Revenue"])
    income = _series(facts, tags["Operating income"])
    if not revenue or not income:
        return []
    rev = {e["end"]: e["val"] for e in _pick(revenue[0], "year", years + 5)}
    op = {e["end"]: e["val"] for e in _pick(income[0], "year", years + 5)}
    ends = sorted(set(rev) & set(op), reverse=True)[:years]
    return [(end, rev[end], op[end]) for end in ends if rev[end]]


def read(symbol: str) -> str:
    """The XBRL block for a ticker, or "" for a company that does not file with the SEC,
    without SEC_USER_AGENT, or on any failure."""
    import research_tools

    if not config.SEC_USER_AGENT:
        return ""
    try:
        cik = cik_for(symbol)
        if cik is None:
            return ""
        return render(research_tools._sec_get(COMPANY_FACTS.format(cik=cik)).json())
    except Exception as exc:
        log.warning("SEC XBRL facts unavailable for %s: %s", symbol, exc)
        return ""
