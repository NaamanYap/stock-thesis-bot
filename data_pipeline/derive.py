"""Calculated facts: everything an analyst works out from the reported figures.

Growth rates, margins, free cash flow, trailing-twelve-month sums, segment shares, net
debt, returns on capital and valuation multiples are computed here, each with its formula
and the ids of the facts it came from, so a reader can check it and the writer never
types one. A calculation is only made from inputs of the same period, and is named by
that period: a quarter's margin is never an annual one.
"""

from __future__ import annotations

import statistics
from datetime import date

import pandas as pd

import config
from .facts_db import Fact, FactDB, format_value
from .periods import Period

FLOWS = ["revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income",
         "profit_incl_minority", "pretax_income", "income_tax", "interest_expense", "d_and_a",
         "depreciation", "amortization_intangibles",
         "sbc", "operating_cash_flow", "capex", "buybacks", "dividends_paid", "acquisitions",
         "rd_expense", "sga_expense", "diluted_eps"]
GROWTH = ["revenue", "operating_income", "net_income", "diluted_eps", "ebitda", "fcf",
          "operating_cash_flow"]
MARGINS = [("gross_margin", "Gross margin", "gross_profit"),
           ("operating_margin", "Operating margin", "operating_income"),
           ("net_margin", "Net margin", "net_income"),
           ("ebitda_margin", "EBITDA margin", "ebitda"),
           ("fcf_margin", "Free cash flow margin", "fcf"),
           ("capex_to_revenue", "Capex as a share of revenue", "capex"),
           ("d_and_a_to_revenue", "D&A as a share of revenue", "d_and_a"),
           ("sbc_to_revenue", "Stock-based compensation as a share of revenue", "sbc"),
           ("rd_to_revenue", "R&D as a share of revenue", "rd_expense")]
LABELS = {"ebitda": "EBITDA (operating income plus D&A)",
          "fcf": "Free cash flow (operating cash flow less capex)"}
TAX_RANGE = (0.0, 0.40)
# Flows that cannot be negative, so a year can never be smaller than a part of it.
EARNINGS = {"gross_profit", "operating_income", "pretax_income", "net_income",
            "profit_incl_minority", "operating_cash_flow"}
NEVER_NEGATIVE = {"revenue", "cost_of_revenue", "rd_expense", "sga_expense", "d_and_a",
                  "depreciation", "amortization_intangibles", "sbc", "capex"}


class Calc:
    """Adds calculated facts to a database, each tracing to its inputs."""

    def __init__(self, db: FactDB):
        self.db = db

    def add(self, metric: str, label: str, value: float, unit: str, period: Period,
            formula: str, inputs: list[Fact], segment: str = "", axis: str = "",
            fact_id: str | None = None) -> Fact | None:
        if value is None or any(f is None for f in inputs):
            return None
        worst = max(inputs, key=lambda f: f.authority) if inputs else None
        kind = worst.source_kind if worst else "market"
        fid = fact_id or self.db.make_id(metric, period, segment)
        if fid in self.db.facts:
            return self.db.facts[fid]
        return self.db.add(Fact(
            id=fid, metric=metric, label=label, value=value, unit=unit, period=period,
            data_type="calculated", source="calculated", source_kind=kind,
            source_date=max((f.source_date for f in inputs), default=""),
            formula=formula, inputs=[f.id for f in inputs], segment=segment,
            segment_axis=axis))


def _fiscal(f: Fact) -> tuple[int, int]:
    return f.period.fiscal_year(), f.period.quarter()


def fill_quarters(db: FactDB, c: Calc) -> None:
    """The quarters a 10-Q does not state on its own. Cash flow statements give the year
    to date only, and the fourth quarter is never filed: Q2 = 1H - Q1, Q3 = 9M - 1H,
    Q4 = FY - 9M, within one fiscal year. A missing year-to-date figure is built from the
    one before plus the quarter (9M = 1H + Q3), so the next quarter can be found."""
    ytd_span = {1: "3M", 2: "6M", 3: "9M", 4: "12M"}
    for metric in FLOWS:
        if metric == "diluted_eps":
            continue        # per-share figures do not subtract (the share count moves)
        segments = [("", "")] + [(s, axis) for axis, names in db.segments(metric).items()
                                 for s in names]
        for seg, axis in segments:
            by: dict[tuple, Fact] = {}
            for f in db.find(metric, segment=seg):
                if f.period.span in ("3M", "6M", "9M", "12M"):
                    by.setdefault((f.period.span, *_fiscal(f)), f)
            for fy in sorted({k[1] for k in by}):
                q = {n: by.get(("3M", fy, n)) for n in (1, 2, 3, 4)}
                ytd = {n: by.get((ytd_span[n], fy, n)) for n in (2, 3, 4)}
                ytd[1] = q[1]
                for n in (2, 3, 4):
                    before, total = ytd[n - 1], ytd[n]
                    if before is None:
                        break
                    if q[n] is None and total is not None:
                        q[n] = c.add(metric, total.label, total.value - before.value,
                                     total.unit, Period("3M", total.period.end, db.fy_end),
                                     f"{total.id} - {before.id}", [total, before], seg, axis)
                    if total is None and q[n] is not None and n < 4:
                        ytd[n] = c.add(metric, q[n].label, before.value + q[n].value,
                                       q[n].unit, Period(ytd_span[n], q[n].period.end,
                                                         db.fy_end),
                                       f"{before.id} + {q[n].id}", [before, q[n]], seg, axis)


def _off(total: float, parts: float) -> bool:
    return abs(parts - total) > config.RECONCILE_TOLERANCE * max(abs(total), 1.0)


def _exclude(db: FactDB, f: Fact, why: str) -> None:
    f.excluded = why
    db.gaps.append(f"reconciliation: {f.id} excluded, {why}")


def _some_add_to(parts: list[Fact], total: float, most: int = 14) -> bool:
    """Whether some of the parts add to the total. A breakdown can nest (AAPL's Products
    and Services, then iPhone, Mac...) or list only part of the company; it contradicts
    the total only when no combination of its members gives it. Not judged (True) past
    `most` members."""
    from itertools import combinations
    if len(parts) > most:
        return True
    return any(not _off(total, sum(f.value for f in combo))
               for n in range(1, len(parts) + 1) for combo in combinations(parts, n))


def reconcile(db: FactDB) -> None:
    """Reported figures that do not add up are excluded before anything is built on them.
    A fiscal year's revenue cannot be smaller than one of its quarters or its year to date
    (FIX's FY2025 arrived as USD 1.83bn, the size of 1Q25), four reported quarters must add
    to the year, and a breakdown of revenue must add to the company's total, each within
    RECONCILE_TOLERANCE."""
    for metric in FLOWS:
        if metric == "diluted_eps":
            continue
        for fy in db.series(metric, "12M"):
            if fy.data_type != "reported":
                continue
            parts = [f for span in ("3M", "6M", "9M") for f in db.series(metric, span)
                     if f.data_type == "reported"
                     and f.period.fiscal_year() == fy.period.fiscal_year()]
            if metric in NEVER_NEGATIVE:
                bigger = [f for f in parts
                          if abs(f.value) > abs(fy.value) * (1 + config.RECONCILE_TOLERANCE)]
                if bigger:
                    top = max(bigger, key=lambda f: abs(f.value))
                    _exclude(db, fy, f"smaller than {top.id} ({top.text()}) of the same year")
                    continue
            elif metric in EARNINGS:
                # A profit can fall in the fourth quarter, so only the mark of a mislabelled
                # period counts: a "year" equal to one of its own quarters (FIX's FY2025
                # operating income arrived as 1Q25's USD 209mn). Lumpy flows (deals,
                # buybacks) can truly stop for a quarter, so they are not judged this way.
                same = [f for f in parts if fy.value and f.value
                        and abs(f.value / fy.value - 1) < 0.005]
                if same:
                    _exclude(db, fy, f"equal to {same[0].id} ({same[0].text()}): a partial "
                             "period filed as the year")
                    continue
            # Quarters and year under one tag: two tags can measure different things
            # (MSFT's D&A).
            quarters = {f.period.quarter(): f for f in parts
                        if f.period.span == "3M" and f.concept == fy.concept}
            if len(quarters) == 4 and _off(fy.value, sum(q.value for q in quarters.values())):
                _exclude(db, fy, f"its four reported quarters add to "
                         f"{format_value(sum(q.value for q in quarters.values()), fy.unit)}")
    for axis, names in db.segments("revenue").items():
        by_period: dict[tuple, list[Fact]] = {}
        for name in names:
            for f in db.find("revenue", segment=name):
                if f.data_type == "reported" and f.segment_axis == axis:
                    by_period.setdefault(f.period.key, []).append(f)
        for parts in by_period.values():
            total = db.one("revenue", parts[0].period)
            if len(parts) < 2 or total is None:
                continue
            added = sum(f.value for f in parts)
            if _off(total.value, added) and not _some_add_to(parts, total.value):
                for f in parts:
                    _exclude(db, f, f"the {axis} breakdown adds to "
                             f"{format_value(added, f.unit)} against {total.id} "
                             f"({total.text()})")


def _sum(c: Calc, metric: str, label: str, parts: list[Fact], period: Period, seg="",
         axis="") -> Fact | None:
    return c.add(metric, label, sum(p.value for p in parts), parts[0].unit, period,
                 " + ".join(p.id for p in parts), parts, seg, axis)


def trailing(db: FactDB, c: Calc) -> None:
    """Trailing twelve months, where a quarter newer than the last fiscal year is filed:
    the year, plus this year to date, less the same span a year before. Four quarters
    summed only where a year-to-date pair is missing."""
    for metric in FLOWS:
        annual = db.latest(metric, "12M")
        if metric != "diluted_eps" and annual is not None:
            ytd = [f for span in ("3M", "6M", "9M") for f in db.series(metric, span)
                   if f.period.fiscal_year() == annual.period.fiscal_year() + 1
                   and f.period.quarter() == {"3M": 1, "6M": 2, "9M": 3}[span]]
            if ytd:
                now = max(ytd, key=lambda f: f.period.end)
                before = db.one(metric, now.period.prior_year())
                if before is not None:
                    c.add(metric, f"{now.period.label}, trailing twelve months",
                          annual.value + now.value - before.value, now.unit,
                          Period("TTM", now.period.end, db.fy_end),
                          f"{annual.id} + {now.id} - {before.id}", [annual, now, before])
                    continue
        quarters = db.series(metric, "3M")
        if len(quarters) < 4:
            continue
        last4 = quarters[-4:]
        latest = last4[-1]
        if annual is not None and annual.period.end >= latest.period.end:
            continue
        # Four consecutive quarters, no gaps.
        if (last4[-1].period.end - last4[0].period.end).days > 300:
            continue
        _sum(c, metric, f"{latest.label}, trailing twelve months", last4,
             Period("TTM", latest.period.end, db.fy_end))


def _periods(db: FactDB, metric: str) -> list[Period]:
    """Every period the metric is held for: years, quarters, year-to-date and TTM."""
    seen = {}
    for f in db.find(metric):
        if f.period.span in ("3M", "6M", "9M", "12M", "TTM"):
            seen.setdefault(f.period.key, f.period)
    return list(seen.values())


def depreciation(db: FactDB, c: Calc) -> None:
    """D&A where the company states depreciation and amortization of intangibles apart."""
    for p in _periods(db, "depreciation"):
        dep, amort = db.one("depreciation", p), db.one("amortization_intangibles", p)
        if dep and amort and db.one("d_and_a", p) is None:
            c.add("d_and_a", "Depreciation and amortization", dep.value + amort.value,
                  dep.unit, p, f"{dep.id} + {amort.id}", [dep, amort])


def earnings_measures(db: FactDB, c: Calc) -> None:
    for p in _periods(db, "operating_income"):
        op, da = db.one("operating_income", p), db.one("d_and_a", p)
        if op and da:
            c.add("ebitda", LABELS["ebitda"], op.value + da.value, op.unit, p,
                  f"{op.id} + {da.id}", [op, da])
    for p in _periods(db, "operating_cash_flow"):
        ocf, capex = db.one("operating_cash_flow", p), db.one("capex", p)
        if ocf and capex:
            c.add("fcf", LABELS["fcf"], ocf.value - abs(capex.value), ocf.unit, p,
                  f"{ocf.id} - {capex.id}", [ocf, capex])
    for p in _periods(db, "revenue"):
        rev = db.one("revenue", p)
        if not rev or not rev.value:
            continue
        for name, label, part in MARGINS:
            f = db.one(part, p)
            if f:
                value = abs(f.value) if part == "capex" else f.value
                c.add(name, label, value / rev.value * 100, "%", p, f"{f.id} / {rev.id}",
                      [f, rev])
    for p in _periods(db, "net_income"):
        fcf, ni = db.one("fcf", p), db.one("net_income", p)
        if fcf and ni and ni.value > 0:
            c.add("fcf_conversion", "Free cash flow as a share of net income",
                  fcf.value / ni.value * 100, "%", p, f"{fcf.id} / {ni.id}", [fcf, ni])
        bb, dv = db.one("buybacks", p), db.one("dividends_paid", p)
        if bb and dv:
            c.add("shareholder_returns", "Buybacks plus dividends",
                  abs(bb.value) + abs(dv.value), bb.unit, p, f"{bb.id} + {dv.id}", [bb, dv])
    for p in _periods(db, "income_tax"):
        tax, pre = db.one("income_tax", p), db.one("pretax_income", p)
        if tax and pre and pre.value > 0:
            c.add("tax_rate", "Effective tax rate", tax.value / pre.value * 100, "%", p,
                  f"{tax.id} / {pre.id}", [tax, pre])


def growth(db: FactDB, c: Calc) -> None:
    """Growth on the same period a year before, for the company and its segments; and the
    compound annual growth of the fiscal years held."""
    for metric in GROWTH:
        segs = [("", "")] + [(s, axis) for axis, names in db.segments(metric).items()
                             for s in names]
        for seg, axis in segs:
            for f in db.find(metric, segment=seg):
                if f.period.span not in ("3M", "6M", "9M", "12M", "TTM"):
                    continue
                prev = db.one(metric, f.period.prior_year(), seg)
                if prev is None or prev.value <= 0:
                    continue
                label = f"{LABELS.get(metric, f.label)} growth on a year before"
                c.add(f"{metric}_growth", label, (f.value / prev.value - 1) * 100, "%",
                      f.period, f"{f.id} / {prev.id} - 1", [f, prev], seg, axis)
        years = db.series(metric, "12M")
        if len(years) >= 3:
            last = years[-1]
            for back in (3, 5):
                if len(years) > back:
                    first = years[-1 - back]
                    if first.value > 0 and last.value > 0:
                        c.add(f"{metric}_cagr_{back}y",
                              f"{last.label}, compound annual growth over {back} years",
                              ((last.value / first.value) ** (1 / back) - 1) * 100, "%",
                              last.period, f"({last.id} / {first.id}) ^ (1/{back}) - 1",
                              [first, last])


def segment_shares(db: FactDB, c: Calc) -> None:
    for axis, names in db.segments("revenue").items():
        for name in names:
            for f in db.find("revenue", segment=name):
                total = db.one("revenue", f.period)
                if total and total.value:
                    c.add("revenue_share", "Share of revenue",
                          f.value / total.value * 100, "%", f.period, f"{f.id} / {total.id}",
                          [f, total], name, axis)


def balance_sheet(db: FactDB, c: Calc) -> None:
    """Total debt, net debt and invested capital at each balance date."""
    dates = {f.period.key: f.period for f in db.facts.values()
             if f.usable and f.period.span == "at" and f.metric in ("cash", "equity")}
    for p in dates.values():
        cur, non, lt = db.one("debt_current", p), db.one("debt_noncurrent", p), \
            db.one("long_term_debt", p)
        debt = None
        if non is not None or cur is not None:
            # Either part alone is still the total: SPOT's notes all fell due within a
            # year at the end of FY2025, leaving no non-current debt.
            parts = [x for x in (non, cur) if x is not None]
            if lt is not None and non is None and lt.value > cur.value:
                parts = [lt]        # long-term debt including its current portion
            debt = c.add("total_debt", "Total debt (current plus non-current)",
                         sum(x.value for x in parts), parts[0].unit, p,
                         " + ".join(x.id for x in parts), parts)
        elif lt is not None:
            debt = c.add("total_debt", "Total debt (long-term debt including the current "
                         "portion)", lt.value, lt.unit, p, lt.id, [lt])
        cash, sti = db.one("cash", p), db.one("short_term_investments", p)
        if debt is not None and cash is not None:
            liquid = [cash] + ([sti] if sti else [])
            c.add("net_debt", "Net debt (total debt less cash and short-term investments)",
                  debt.value - sum(x.value for x in liquid), debt.unit, p,
                  f"{debt.id} - " + " - ".join(x.id for x in liquid), [debt, *liquid])
        eq = db.one("equity", p)
        if eq is not None and debt is not None and cash is not None:
            mi = [x for x in (db.one("minority_interest", p),
                              db.one("redeemable_minority_interest", p)) if x]
            liquid = [cash] + ([sti] if sti else [])
            value = eq.value + debt.value + sum(x.value for x in mi) - sum(x.value for x in liquid)
            c.add("invested_capital", "Invested capital (equity + debt + minority interests "
                  "- cash)", value, eq.unit, p,
                  " + ".join(x.id for x in [eq, debt, *mi]) + " - " + " - ".join(
                      x.id for x in liquid), [eq, debt, *mi, *liquid])
            gw = db.one("goodwill", p)
            if gw is not None:
                c.add("invested_capital_ex_goodwill", "Invested capital excluding goodwill",
                      value - gw.value, eq.unit, p, f"invested_capital - {gw.id}",
                      [db.one("invested_capital", p), gw])


def returns(db: FactDB, c: Calc) -> None:
    """Return on invested capital for each fiscal year: operating income after tax over
    the average of opening and closing invested capital."""
    for op in db.series("operating_income", "12M"):
        p = op.period
        tax = db.one("tax_rate", p)
        if tax is None or not TAX_RANGE[0] <= tax.value / 100 <= TAX_RANGE[1]:
            continue
        end = Period("at", p.end, db.fy_end)
        start = Period("at", p.prior_year().end, db.fy_end)
        for metric, label in (("invested_capital", "Return on invested capital"),
                              ("invested_capital_ex_goodwill",
                               "Return on invested capital excluding goodwill")):
            a, b = db.one(metric, start), db.one(metric, end)
            if a is None or b is None or a.value + b.value <= 0:
                continue
            avg = (a.value + b.value) / 2
            nopat = op.value * (1 - tax.value / 100)
            c.add("roic" if metric == "invested_capital" else "roic_ex_goodwill", label,
                  nopat / avg * 100, "%", p,
                  f"{op.id} * (1 - {tax.id}) / average({a.id}, {b.id})", [op, tax, a, b])


def latest_flow(db: FactDB, metric: str) -> Fact | None:
    """The most recent twelve months of a flow: TTM where it is newer than the year."""
    ttm, fy = db.latest(metric, "TTM"), db.latest(metric, "12M")
    if ttm and (fy is None or ttm.period.end > fy.period.end):
        return ttm
    return fy


def latest_balance(db: FactDB, metric: str) -> Fact | None:
    s = db.series(metric, "at")
    s = [f for f in s if f.period.at_quarter_end()]
    return s[-1] if s else None


def multiples(db: FactDB, c: Calc) -> None:
    """Today's valuation on the latest twelve months: EV/EBITDA, EV/revenue, P/E, FCF
    yield. The market value is the share price times the latest share count, in the
    reporting currency."""
    mcap = db.get("market_cap_reporting") or db.get("market_cap")
    if mcap is None:
        return
    debt, net = latest_balance(db, "total_debt"), latest_balance(db, "net_debt")
    mi = [x for x in (latest_balance(db, "minority_interest"),
                      latest_balance(db, "redeemable_minority_interest")) if x]
    now = Period("latest", None, db.fy_end)
    ev = None
    if net is not None:
        ev = c.add("enterprise_value", "Enterprise value (market cap + net debt + minority "
                   "interests)", mcap.value + net.value + sum(x.value for x in mi), net.unit,
                   now, " + ".join(x.id for x in [mcap, net, *mi]), [mcap, net, *mi])
    for metric, name, label, base in (
            ("ebitda", "ev_to_ebitda", "EV / EBITDA", ev),
            ("revenue", "ev_to_revenue", "EV / revenue", ev),
            ("net_income", "pe", "Price / earnings (market cap / net income)", mcap)):
        flow = latest_flow(db, metric)
        if base is not None and flow is not None and flow.value > 0:
            c.add(name, f"{label}, {flow.period.label}", base.value / flow.value, "x", now,
                  f"{base.id} / {flow.id}", [base, flow])
    fcf = latest_flow(db, "fcf")
    if fcf is not None:
        c.add("fcf_yield", f"Free cash flow yield, {fcf.period.label}",
              fcf.value / mcap.value * 100, "%", now, f"{fcf.id} / {mcap.id}", [fcf, mcap])
    ebitda = latest_flow(db, "ebitda")
    if net is not None and ebitda is not None and ebitda.value > 0:
        c.add("net_debt_to_ebitda", f"Net debt / EBITDA, {ebitda.period.label}",
              net.value / ebitda.value, "x", now, f"{net.id} / {ebitda.id}", [net, ebitda])
    interest, op = latest_flow(db, "interest_expense"), latest_flow(db, "operating_income")
    if interest is not None and op is not None and interest.value > 0:
        c.add("interest_cover", f"Interest cover (operating income / interest), "
              f"{op.period.label}", op.value / interest.value, "x", now,
              f"{op.id} / {interest.id}", [op, interest])


def split_factor(splits: list[tuple[date, float]], basis: str) -> float:
    """How many of today's shares one share was, on the share basis of a filing made on
    `basis` (ISO date): the product of the splits after it. A figure filed before a split
    counts shares, and per-share amounts, on the old basis."""
    try:
        d = date.fromisoformat(basis[:10])
    except ValueError:
        return 1.0
    factor = 1.0
    for when, ratio in splits:
        if when > d and ratio > 0:
            factor *= ratio
    return factor


def historical_multiples(db: FactDB, c: Calc, closes: pd.Series | None, fx: float = 1.0,
                         source_date: str = "",
                         splits: list[tuple[date, float]] | None = None) -> None:
    """The stock's own valuation at each fiscal year end it has a price for: EV/EBITDA
    and P/E, from the month-end close and that year's reported figures. Gives the
    terminal multiple a history to be judged against. `closes` is monthly, in the trading
    currency and on today's share basis (adjusted for splits, not dividends); each price is
    put on the share basis of the filing whose share count or EPS it is set against. `fx`
    is trading currency per unit of reporting currency."""
    if closes is None or len(closes) == 0:
        return
    splits = splits or []
    found = {"ev_to_ebitda": [], "ev_to_revenue": [], "pe": []}
    for revenue in db.series("revenue", "12M"):
        p = revenue.period
        ebitda = db.one("ebitda", p)
        month = pd.Period(year=p.end.year, month=p.end.month, freq="M")
        if month not in closes.index:
            continue
        price = float(closes[month]) / (fx or 1.0)
        shares = db.one("diluted_shares", p)
        net = db.one("net_debt", Period("at", p.end, db.fy_end))
        eps = db.one("diluted_eps", p)
        price_fact = c.db.add(Fact(
            id=f"share_price_{p.slug}", metric="share_price_fy_end",
            label=f"Share price at the end of {p.label} (month-end close, on today's share "
                  f"basis)", value=price, unit=f"{db.currency}/share",
            period=Period("at", p.end, db.fy_end), data_type="market",
            source="Yahoo Finance monthly closes", source_kind="market",
            source_date=source_date, source_location=f"close for {month}"))
        if shares and net:
            k = split_factor(splits, shares.source_date)
            ev = price * k * shares.value + net.value
            adjust = f" * {k:g} (splits since filing)" if k != 1 else ""
            ev_formula = f"({price_fact.id}{adjust} * {shares.id} + {net.id})"
            # EV/EBITDA means nothing for a year of losses; EV/revenue still does.
            if ebitda is not None and ebitda.value > 0:
                f = c.add("ev_to_ebitda_hist", f"EV / EBITDA at the end of {p.label}",
                          ev / ebitda.value, "x", p, f"{ev_formula} / {ebitda.id}",
                          [price_fact, shares, net, ebitda])
                if f:
                    found["ev_to_ebitda"].append(f)
            if revenue.value > 0:
                f = c.add("ev_to_revenue_hist", f"EV / revenue at the end of {p.label}",
                          ev / revenue.value, "x", p, f"{ev_formula} / {revenue.id}",
                          [price_fact, shares, net, revenue])
                if f:
                    found["ev_to_revenue"].append(f)
        if eps and eps.value > 0:
            k = split_factor(splits, eps.source_date)
            adjust = f" * {k:g} (splits since filing)" if k != 1 else ""
            f = c.add("pe_hist", f"P/E at the end of {p.label}", price * k / eps.value, "x",
                      p, f"{price_fact.id}{adjust} / {eps.id}", [price_fact, eps])
            if f:
                found["pe"].append(f)
    now = Period("latest", None, db.fy_end)
    for name, facts in found.items():
        if len(facts) >= 3:
            values = sorted(f.value for f in facts)
            years = f"{facts[0].period.label}-{facts[-1].period.label}"
            label = {"ev_to_ebitda": "EV / EBITDA", "ev_to_revenue": "EV / revenue",
                     "pe": "P/E"}[name]
            for stat, v in (("low", values[0]), ("median", statistics.median(values)),
                            ("high", values[-1])):
                c.add(f"{name}_hist_{stat}", f"{label} at fiscal year ends {years}, {stat}",
                      v, "x", now, f"{stat}({', '.join(f.id for f in facts)})", facts)


def derive_all(db: FactDB, closes: pd.Series | None = None, fx: float = 1.0,
               splits: list[tuple[date, float]] | None = None) -> int:
    """Every calculation, in dependency order. Returns how many facts were added."""
    before = len(db.facts)
    c = Calc(db)
    reconcile(db)
    fill_quarters(db, c)
    trailing(db, c)
    depreciation(db, c)
    earnings_measures(db, c)
    balance_sheet(db, c)
    growth(db, c)
    segment_shares(db, c)
    returns(db, c)
    multiples(db, c)
    historical_multiples(db, c, closes, fx, db.built, splits)
    return len(db.facts) - before
