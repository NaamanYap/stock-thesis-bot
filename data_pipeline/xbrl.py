"""The company's own tagged figures (XBRL), read into the fact database.

Two readers, both for figures the company tagged itself, so the code needs no reading of
prose or table layouts to know what a number is:

- `from_company_facts`: the SEC's company facts API, every figure the company has tagged
  in any 10-K or 10-Q. Gives the history (annual and quarterly) of the standard metrics.
- `from_inline`: the inline tags inside one filing's HTML. Gives everything in the latest
  10-Q or 10-K, including figures by business segment, product and region, which the
  company facts API leaves out.
"""

from __future__ import annotations

import logging
import re
import warnings
from datetime import date

from bs4 import BeautifulSoup, SoupStrainer, XMLParsedAsHTMLWarning

import config
from .facts_db import Fact, FactDB, slug
from .periods import Period, fy_end_from_annual_ends, span_of

log = logging.getLogger(__name__)

PERIODIC_FORMS = {"10-K", "10-Q", "20-F", "40-F", "10-K/A", "10-Q/A", "20-F/A",
                  "40-F/A", "10-KT"}
ANNUAL_FORMS = {"10-K", "20-F", "40-F", "10-K/A", "20-F/A", "40-F/A", "10-KT"}

# Canonical metric -> (label, unit kind, tags in order of preference). us-gaap first,
# IFRS names for 20-F filers. "flow" covers a span of time; "stock" is a balance at a date.
METRICS: dict[str, tuple[str, str, list[str]]] = {
    "revenue": ("Revenue", "flow", [
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax", "Revenue"]),
    "cost_of_revenue": ("Cost of revenue", "flow", [
        "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfSales"]),
    "gross_profit": ("Gross profit", "flow", ["GrossProfit"]),
    "rd_expense": ("Research and development", "flow", ["ResearchAndDevelopmentExpense"]),
    "sga_expense": ("Selling, general and administrative", "flow", [
        "SellingGeneralAndAdministrativeExpense"]),
    "operating_income": ("Operating income", "flow", [
        "OperatingIncomeLoss", "ProfitLossFromOperatingActivities"]),
    "interest_expense": ("Interest expense", "flow", [
        "InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt",
        "InterestIncomeExpenseNonoperatingNet"]),
    "pretax_income": ("Income before taxes", "flow", [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "ProfitLossBeforeTax"]),
    "income_tax": ("Income tax expense", "flow", [
        "IncomeTaxExpenseBenefit", "IncomeTaxExpenseContinuingOperations"]),
    "net_income": ("Net income attributable to shareholders", "flow", [
        "NetIncomeLoss", "ProfitLossAttributableToOwnersOfParent"]),
    "profit_incl_minority": ("Net income including minority interests", "flow", ["ProfitLoss"]),
    "diluted_eps": ("Diluted EPS", "per_share", [
        "EarningsPerShareDiluted", "DilutedEarningsLossPerShare"]),
    "diluted_shares": ("Diluted weighted average shares", "shares", [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingDiluted"]),
    "d_and_a": ("Depreciation and amortization", "flow", [
        "DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
        "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortisationExpense"]),
    "depreciation": ("Depreciation", "flow", ["Depreciation", "DepreciationNonproduction"]),
    "amortization_intangibles": ("Amortization of intangible assets", "flow", [
        "AmortizationOfIntangibleAssets"]),
    "sbc": ("Stock-based compensation", "flow", [
        "ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]),
    "operating_cash_flow": ("Operating cash flow", "flow", [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "CashFlowsFromUsedInOperatingActivities"]),
    "capex": ("Capital expenditure", "flow", [
        "PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"]),
    "acquisitions": ("Acquisitions, net of cash acquired", "flow", [
        "PaymentsToAcquireBusinessesNetOfCashAcquired"]),
    "buybacks": ("Share repurchases", "flow", ["PaymentsForRepurchaseOfCommonStock"]),
    "dividends_paid": ("Dividends paid", "flow", [
        "PaymentsOfDividends", "PaymentsOfDividendsCommonStock",
        "DividendsPaidClassifiedAsFinancingActivities"]),
    "cash": ("Cash and cash equivalents", "stock", [
        "CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalents",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
    "short_term_investments": ("Short-term investments", "stock", [
        "ShortTermInvestments", "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent", "CurrentFinancialAssetsAtFairValueThroughProfitOrLoss"]),
    "debt_current": ("Debt due within a year", "stock", [
        "LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings", "CommercialPaper",
        "LongTermDebtAndCapitalLeaseObligationsCurrent", "CurrentBorrowings"]),
    "debt_noncurrent": ("Long-term debt, non-current", "stock", [
        "LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
        "NoncurrentPortionOfNoncurrentBorrowings", "NoncurrentBorrowings"]),
    "long_term_debt": ("Long-term debt, including the current portion", "stock", [
        "LongTermDebt", "DebtLongtermAndShorttermCombinedAmount"]),
    "total_assets": ("Total assets", "stock", ["Assets"]),
    "total_liabilities": ("Total liabilities", "stock", ["Liabilities"]),
    "current_assets": ("Current assets", "stock", ["AssetsCurrent", "CurrentAssets"]),
    "current_liabilities": ("Current liabilities", "stock", [
        "LiabilitiesCurrent", "CurrentLiabilities"]),
    "equity": ("Shareholders' equity", "stock", [
        "StockholdersEquity", "EquityAttributableToOwnersOfParent"]),
    "minority_interest": ("Minority interests", "stock", [
        "MinorityInterest", "NoncontrollingInterests"]),
    "redeemable_minority_interest": ("Redeemable minority interests", "stock", [
        "RedeemableNoncontrollingInterestEquityCarryingAmount"]),
    "goodwill": ("Goodwill", "stock", ["Goodwill"]),
    "intangibles": ("Intangible assets other than goodwill", "stock", [
        "IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet",
        "IntangibleAssetsOtherThanGoodwill"]),
    "inventory": ("Inventory", "stock", ["InventoryNet", "Inventories"]),
    "receivables": ("Accounts receivable", "stock", [
        "AccountsReceivableNetCurrent", "TradeAndOtherCurrentReceivables"]),
    "payables": ("Accounts payable", "stock", [
        "AccountsPayableCurrent", "TradeAndOtherCurrentPayables"]),
    "rpo": ("Remaining performance obligations", "stock", [
        "RevenueRemainingPerformanceObligation"]),
    "shares_outstanding": ("Shares outstanding", "shares_stock", [
        "EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"]),
}
# Share counts and per-share figures move with stock splits, not only restatements.
SPLIT_KINDS = {"per_share", "shares", "shares_stock"}
TAG_TO_METRIC = {tag: m for m, (_, _, tags) in METRICS.items() for tag in tags}
TAG_RANK = {tag: i for _, (_, _, tags) in METRICS.items() for i, tag in enumerate(tags)}

# Axes whose members split a figure by business, product or region.
SEGMENT_AXES = {
    "StatementBusinessSegmentsAxis": "segment",
    "ProductOrServiceAxis": "product",
    "StatementGeographicalAxis": "geography",
    "SegmentsAxis": "segment",          # IFRS
}
# Consolidation members that leave a segment figure as the segment's own.
SEGMENT_TOTAL = {"OperatingSegmentsMember"}
NOT_SEGMENTS = re.compile(r"Elimination|Intersegment|ReconcilingItem|Adjustment", re.I)


def words(name: str) -> str:
    """A tag or member name as words: "spgi:RatingsMember" -> "Ratings";
    "SubscriptionAndCirculationMember" -> "Subscription and circulation"."""
    local = name.split(":")[-1]
    local = re.sub(r"(?:Segment)?(?:Member|Axis)$", "", local)
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", local) or [local]
    out = [parts[0]] + [p if p.isupper() and len(p) > 1 else p.lower() for p in parts[1:]]
    text = " ".join(out)
    return text[:1].upper() + text[1:]


def _unit_of(unit: str, kind: str, currency: str | None) -> str | None:
    """A company facts unit ("USD", "USD/shares", "shares", "pure") as the fact's unit."""
    if kind == "per_share":
        return f"{unit.split('/')[0]}/share" if "/" in unit else None
    if kind in ("shares", "shares_stock"):
        return "shares" if unit == "shares" else None
    if re.fullmatch(r"[A-Z]{3}", unit):
        return unit
    return None


# ---------------------------------------------------------------- company facts

def fiscal_year_end(company_facts: dict) -> int:
    """The month the fiscal year ends: where the annual periods of a 10-K end."""
    ends = []
    for taxonomy in ("us-gaap", "ifrs-full"):
        for tag in METRICS["revenue"][2] + METRICS["net_income"][2]:
            for entries in company_facts.get("facts", {}).get(taxonomy, {}).get(tag, {}).get(
                    "units", {}).values():
                for e in entries:
                    if e.get("form") in ("10-K", "20-F", "40-F") and "start" in e \
                            and span_of(date.fromisoformat(e["start"]),
                                        date.fromisoformat(e["end"])) == "12M":
                        ends.append(date.fromisoformat(e["end"]))
    return fy_end_from_annual_ends(ends)


def _entries(company_facts: dict, metric: str) -> list[tuple[str, str, dict]]:
    """(tag, unit, entry) for a metric, the tags ranked by how recently they were used and
    then by preference: companies change tags (SPGI's capex tag stopped in 2009)."""
    _, kind, tags = METRICS[metric]
    found = []
    for rank, tag in enumerate(tags):
        for taxonomy in ("us-gaap", "ifrs-full", "dei"):
            units = company_facts.get("facts", {}).get(taxonomy, {}).get(tag, {}).get("units", {})
            for unit, entries in units.items():
                if _unit_of(unit, kind, None) is None or not entries:
                    continue
                latest = max(e["end"] for e in entries)
                found.append((latest, -rank, f"{taxonomy}:{tag}", unit, entries))
    found.sort(reverse=True)
    out = []
    for _, _, tag, unit, entries in found:
        out += [(tag, unit, e) for e in entries]
    return out


def _changed(a: dict, b: dict) -> bool:
    """Two filings' values for one period differ by more than RESTATEMENT_LIMIT."""
    x, y = float(a["val"]), float(b["val"])
    if x == y:
        return False
    base = max(abs(x), abs(y))
    return base > 0 and abs(x - y) / base > config.RESTATEMENT_LIMIT


def _pick(company_facts: dict, fy_end: int) -> tuple[dict, dict]:
    """(metric -> period key -> (tag, unit, entry, period), restated), where restated maps
    (metric, period key) to the two filings of one tag that disagree on the period."""
    picked: dict[str, dict[tuple, tuple[str, str, dict, Period]]] = {}
    restated: dict[tuple, tuple[dict, dict]] = {}
    for metric, (label, kind, _) in METRICS.items():
        chosen: dict[tuple, tuple[str, str, dict, Period]] = {}
        ranked_tags: list[str] = []
        for tag, unit, e in _entries(company_facts, metric):
            if e.get("form") not in PERIODIC_FORMS and not (tag.startswith("dei:")):
                continue
            end = date.fromisoformat(e["end"])
            if "start" in e:
                if kind in ("stock", "shares_stock"):
                    continue
                span = span_of(date.fromisoformat(e["start"]), end)
                if span is None:
                    continue
                # Annual periods must come from an annual filing. The SEC company-facts
                # feed can attach a 12-month period to a comparative quarter in a later
                # 10-Q. Letting that later row win by filed-date can overwrite a valid 10-K
                # figure and make derived Q4/TTM revenue nonsensical.
                if span == "12M" and e.get("form") not in ANNUAL_FORMS:
                    continue
                period = Period(span, end, fy_end)
            else:
                if kind not in ("stock", "shares_stock"):
                    continue
                period = Period("at", end, fy_end)
            if tag not in ranked_tags:
                ranked_tags.append(tag)
            k = period.key
            held = chosen.get(k)
            if held is not None and held[0] == tag and kind not in SPLIT_KINDS                     and _changed(held[2], e):
                # One tag, one period, two filings far apart: a bad row in the feed or a
                # restatement. Either way not a figure to build a thesis on.
                pair = sorted((held[2], e), key=lambda x: x.get("filed", ""))
                restated[(metric, k)] = (pair[0], pair[-1])
            # The first-ranked tag that reports a period gives it; within a tag the latest
            # filing (a restatement) wins.
            if held is None or (held[0] == tag and e.get("filed", "") > held[2].get("filed", "")):
                chosen[k] = (tag, unit, e, period)
        if chosen:
            picked[metric] = chosen
    return picked, restated


def restatements(company_facts: dict) -> dict[tuple, tuple[dict, dict]]:
    """Periods whose figure changed by more than RESTATEMENT_LIMIT between filings."""
    return _pick(company_facts, fiscal_year_end(company_facts))[1]


def from_company_facts(db: FactDB, company_facts: dict, cik: int | None = None) -> int:
    """Add the standard metrics' annual, quarterly and year-to-date figures, and balances.
    A figure two filings disagree on is added excluded. Returns how many were added."""
    db.fy_end = fiscal_year_end(company_facts)
    if not db.entity:
        db.entity = company_facts.get("entityName", "")
    # The reporting currency: the unit of the revenue most recently reported.
    revenue = [(e["end"], unit) for _, unit, e in _entries(company_facts, "revenue")]
    if revenue:
        db.currency = max(revenue)[1]
    picked, restated = _pick(company_facts, db.fy_end)
    if not picked:
        return 0
    # Keep the recent history: the annual years, quarters and year-to-date periods a
    # derivation needs, and balances at the same dates. The window is the company's, not
    # the tag's: a tag dropped in 2015 (FIX's MinorityInterest) must not make its last
    # 2015 balance look current.
    newest = max(p.end for chosen in picked.values() for *_, p in chosen.values())
    keep_after = date(newest.year - config.ANNUAL_YEARS - 1, newest.month, 1)
    added = 0
    for metric, chosen in picked.items():
        label, kind, _ = METRICS[metric]
        for tag, unit, e, period in chosen.values():
            if period.end < keep_after:
                continue
            fact_unit = _unit_of(unit, kind, db.currency)
            if fact_unit not in (db.currency, f"{db.currency}/share", "shares"):
                continue
            accn = e.get("accn", "")
            url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/"
                   if cik and accn else "")
            form, filed = e.get("form", ""), e.get("filed", "")
            excluded = ""
            if (metric, period.key) in restated:
                old, new = restated[(metric, period.key)]
                excluded = (f"filings disagree: {old.get('form')} filed {old.get('filed')} gave "
                            f"{float(old['val']):,.0f}, {new.get('form')} filed "
                            f"{new.get('filed')} gave {float(new['val']):,.0f}; treated as a "
                            f"data error, not a restatement")
                db.gaps.append(f"{metric} {period.label}: {excluded}")
            db.add(Fact(
                id=db.make_id(metric, period), metric=metric, label=label, value=float(e["val"]),
                unit=fact_unit, period=period, data_type="reported",
                source=f"{form} filed {filed}" if form else "SEC company facts",
                source_kind="sec_company_facts", source_date=filed, source_url=url,
                source_location=tag, concept=tag, start=e.get("start", ""), form=form,
                excluded=excluded))
            added += 1
    return added


# ---------------------------------------------------------------- inline XBRL

def _number(text: str, fmt: str) -> float | None:
    t = text.strip()
    if "zero" in fmt or t in ("-", "—", "–", ""):
        return 0.0 if "zero" in fmt or t else None
    if "comma-decimal" in fmt or "numcommadecimal" in fmt:
        t = t.replace(".", "").replace(" ", "").replace(",", ".")
    else:
        t = t.replace(",", "").replace(" ", "")
    t = t.strip("()$€£")
    try:
        return float(t)
    except ValueError:
        return None


def _contexts(soup: BeautifulSoup) -> dict[str, tuple[date | None, date, dict[str, str]]]:
    out = {}
    for c in soup.find_all("xbrli:context"):
        start = c.find("xbrli:startdate")
        end = c.find("xbrli:enddate")
        instant = c.find("xbrli:instant")
        try:
            if instant is not None:
                s, e = None, date.fromisoformat(instant.get_text(strip=True))
            elif start is not None and end is not None:
                s = date.fromisoformat(start.get_text(strip=True))
                e = date.fromisoformat(end.get_text(strip=True))
            else:
                continue
        except ValueError:
            continue
        dims = {m.get("dimension", "").split(":")[-1]: m.get_text(strip=True)
                for m in c.find_all("xbrldi:explicitmember")}
        if c.find("xbrldi:typedmember") is not None:
            dims["_typed"] = "1"
        out[c.get("id")] = (s, e, dims)
    return out


def _units(soup: BeautifulSoup) -> dict[str, str]:
    out = {}
    for u in soup.find_all("xbrli:unit"):
        measures = [m.get_text(strip=True).split(":")[-1] for m in u.find_all("xbrli:measure")]
        if u.find("xbrli:divide") is not None and len(measures) == 2:
            out[u.get("id")] = f"{measures[0]}/{measures[1]}"
        elif measures:
            out[u.get("id")] = measures[0]
    return out


def _segment(dims: dict[str, str]) -> tuple[str, str] | None:
    """(segment name, axis kind) for a context split only by segment axes, ("", "") for
    the whole company, None for any other split."""
    if not dims:
        return "", ""
    if "_typed" in dims:
        return None
    names, kinds = [], []
    for axis, member in dims.items():
        if axis == "ConsolidationItemsAxis":
            if member.split(":")[-1] not in SEGMENT_TOTAL:
                return None
            continue
        if axis not in SEGMENT_AXES or NOT_SEGMENTS.search(member):
            return None
        names.append(words(member))
        kinds.append(SEGMENT_AXES[axis])
    if not names:
        # Split only by consolidation ("operating segments", before eliminations): not
        # the company's own total.
        return None
    return " — ".join(names), "+".join(kinds)


INLINE_TAGS = SoupStrainer(["ix:nonfraction", "xbrli:context", "xbrli:unit"])


def from_inline(db: FactDB, html: str, source: str, filed: str, url: str = "") -> int:
    """Add every numeric figure a filing tags for the company or one of its segments.
    `source` names the filing ("10-Q for 2Q26, filed 2026-07-28")."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        # Only the tags read below: a whole tree of BABA's 20-F took 150 MB and, with the
        # bot's other memory, pushed it past a free Render instance's 512 MB (2026-10-12).
        soup = BeautifulSoup(html, "lxml", parse_only=INLINE_TAGS)
    contexts, units = _contexts(soup), _units(soup)
    form = source.split()[0].rstrip(",") if source else ""  # "10-Q for 2Q26, filed ..."
    seen: set[tuple] = set()
    # One figure per metric, period and segment: where a filing tags two of a metric's
    # tags for the same context (LongTermDebt and DebtLongtermAndShorttermCombinedAmount),
    # the preferred tag gives it.
    found: dict[tuple, tuple[int, Fact]] = {}
    for tag in soup.find_all("ix:nonfraction"):
        concept = tag.get("name", "")
        ctx = contexts.get(tag.get("contextref", ""))
        unit = units.get(tag.get("unitref", ""), "")
        if not concept or ctx is None:
            continue
        start, end, dims = ctx
        seg = _segment(dims)
        if seg is None:
            continue
        if (concept, tag.get("contextref")) in seen:
            continue
        seen.add((concept, tag.get("contextref")))
        value = _number(tag.get_text(), tag.get("format", ""))
        if value is None:
            continue
        try:
            value *= 10 ** int(tag.get("scale") or 0)
        except ValueError:
            continue
        if tag.get("sign") == "-":
            value = -value
        if start is None:
            period = Period("at", end, db.fy_end)
        else:
            span = span_of(start, end)
            if span is None or (span == "12M" and form not in ANNUAL_FORMS):
                # A fiscal year only from an annual report, as in from_company_facts.
                continue
            period = Period(span, end, db.fy_end)
        local = concept.split(":")[-1]
        metric = TAG_TO_METRIC.get(local)
        if metric:
            label = METRICS[metric][0]
        else:
            metric, label = slug(words(local)), words(local)
        if re.fullmatch(r"[A-Z]{3}", unit):
            fact_unit = unit
        elif re.fullmatch(r"[A-Z]{3}/shares", unit):
            fact_unit = unit.split("/")[0] + "/share"
        elif unit == "shares":
            fact_unit = "shares"
        elif unit == "pure":
            # Tagged ratios are fractions: 0.5 is 50%.
            fact_unit, value = "%", value * 100
        else:
            fact_unit = unit or "count"
        segment, axis = seg
        fact = Fact(
            id=db.make_id(metric, period, segment), metric=metric, label=label, value=value,
            unit=fact_unit, period=period, data_type="reported", source=source,
            source_kind="sec_inline", source_date=filed, source_url=url,
            source_location=f"{concept}, context {tag.get('contextref')}", segment=segment,
            segment_axis=axis, concept=concept,
            start=start.isoformat() if start else "", form=form)
        k = (metric, period.key, segment, fact_unit)
        rank = TAG_RANK.get(local, 0)
        if k not in found or rank < found[k][0]:
            found[k] = (rank, fact)
    for _, fact in found.values():
        db.add(fact)
    return len(found)
