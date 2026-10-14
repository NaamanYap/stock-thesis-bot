"""The figure sheet: the figures an analyst writes with, named, computed and sourced by code.

The fact database tags every cell of every table it reads: SPGI's held 812, as
`F233`-style ids, and its facts-mode rewrites cited 11 of them while typing 297 figures.
An analyst does not write "the three months revenue cell of the segment table"; it writes
"Ratings revenue grew 17% in 2Q26, to 32% of the group". This sheet gives the writer
those figures, about 50 to 90 of them, under names that say what they are
(`{{ratings_revenue_growth_2Q26}}`), with the growth rates, margins, shares, free cash
flow and leverage computed here from the reported figures. Each is a Fact under its
name, so the filler prints it with its source like any other.

Sources, in the order they are trusted: the company's tagged SEC figures (XBRL), the
tables of its latest earnings release and 10-Q/10-K, and the market data block.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

import facts
from facts import Fact
from provenance import figures

# Names the model's own placeholders use: a sheet figure never takes one.
RESERVED = {"price", "intrinsic", "upside", "rating", "wacc", "wacc_build", "beta", "ev",
            "equity", "pv_fcf", "tv", "pv_tv", "tv_share", "cash", "debt", "shares", "g",
            "exit_multiple", "ev_ebitda", "downside", "implied_cagr", "model_cagr",
            # Returns on capital and leverage, computed with the valuation (bridge debt).
            "net_debt", "net_cash", "net_debt_to_ebitda", "goodwill", "interest_expense",
            "interest_coverage", "cost_of_debt", "earnings_yield", "terminal_roic",
            "terminal_reinvestment", "buyback_amount", "buyback_eps_change",
            "minority_interest", "new_capital_return",
            "terminal_new_capital_return"}

# A metric by the row labels it goes by: the XBRL reader's first, then Yahoo's.
ANNUAL = {
    "revenue": ["revenue", "total revenue", "revenues", "net revenues", "total revenues"],
    "operating_income": ["operating income", "operating profit", "income from operations"],
    "net_income": ["net income", "net income common stockholders"],
    "diluted_eps": ["diluted eps"],
    "ebitda": ["ebitda"],
    # Without these on the sheet, ESLT's writer of 2026-10-07 cited D&A as a share of
    # revenue (2.2%) for its R&D, which was 7.6%.
    "research_and_development": ["research and development", "research and development expense",
                                 "research and development expenses", "research and development, net",
                                 "research and development expenses, net"],
    "sga": ["selling, general and administrative", "selling general and administration",
            "selling general and administrative"],
    "d_and_a": ["depreciation and amortization", "depreciation amortization depletion",
                "depreciation and amortisation"],
    "operating_cash_flow": ["operating cash flow"],
    "capex": ["capital expenditure", "capital expenditures"],
    "buybacks": ["share repurchases", "repurchase of capital stock"],
    "dividends": ["dividends paid", "cash dividends paid"],
    "cash_and_equivalents": ["cash and equivalents", "cash and cash equivalents"],
    "long_term_debt": ["long-term debt", "long term debt"],
}
LABELS = {
    "revenue": "revenue", "operating_income": "operating income", "net_income": "net income",
    "diluted_eps": "diluted EPS", "ebitda": "EBITDA", "d_and_a": "D&A",
    "research_and_development": "R&D expense", "sga": "SG&A expense",
    "rd_pct_revenue": "R&D as a share of revenue", "sga_pct_revenue": "SG&A as a share of revenue",
    "operating_cash_flow": "operating cash flow", "capex": "capex", "buybacks": "share repurchases",
    "dividends": "dividends paid", "fcf": "free cash flow (operating cash flow less capex)",
    "revenue_growth": "revenue growth on the year before", "operating_margin": "operating margin",
    "net_margin": "net margin", "fcf_margin": "free cash flow margin",
    "capex_pct_revenue": "capex as a share of revenue", "da_pct_revenue": "D&A as a share of revenue",
    "shareholder_returns": "buybacks plus dividends", "ebitda_margin": "EBITDA margin",
    "fcf_conversion": "free cash flow as a share of net income",
}
OLDER_YEARS = {"revenue", "operating_income", "net_income", "diluted_eps", "fcf",
               "operating_margin", "fcf_margin", "revenue_growth"}
# The income statement in a filing or the XBRL quarters: a quarter's results, not a
# segment table's or a reconciliation's.
STATEMENT_TABLE = re.compile(r"statements? of (?:income|operations|earnings)|income statement|"
                             r"XBRL facts, quarterly", re.I)
GUIDANCE_TABLE = re.compile(r"outlook|guidance", re.I)
SEGMENT_TABLE = re.compile(r"\bsegment", re.I)
NOT_A_SEGMENT = re.compile(r"intersegment|elimination|corporate|unallocated|%|change|total|"
                           r"other income|expenses?\b", re.I)
FOOTNOTE = re.compile(r"\s*\((?:[a-z]|\d{1,2})\)\s*$")


@dataclass
class Sheet:
    figures: dict[str, Fact] = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)   # heading -> names, in order

    def render(self) -> str:
        lines = [SHEET_INTRO]
        for heading, names in self.groups.items():
            if not names:
                continue
            lines.append(f"\n## {heading}\n")
            for name in names:
                f = self.figures[name]
                lines.append(f"- `{{{{{name}}}}}` {f.metric}, {f.period}: {f.text} ({f.source})")
        return "\n".join(lines)


SHEET_INTRO = """# Figure sheet

Every figure you may state from the company's reports and the market data is below, under
a name. Cite it by that name in double braces, `{{revenue_FY2025}}`, and the code prints
the value with its source. A figure that is not here, not a placeholder of your model and
not one of your declared assumptions is not in the pitch: describe it in words instead.
For a share, ratio or change between two figures here that the sheet does not already
give, write `{{share:a/b}}`, `{{ratio:a/b}}` or `{{change:a/b}}` with their names."""


def _slug(text: str) -> str:
    text = FOOTNOTE.sub("", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower().replace("&", " and ")).strip("_")


def _label(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9&\- ]+", " ", FOOTNOTE.sub("", text.split(":")[-1]).lower()).split())


def period_label(key: tuple, fy_end: int) -> str:
    """("3M", "2026-06") -> "2Q26"; ("12M", "2025-12") -> "FY2025"; ("6M", ...) -> "1H26"."""
    span, end = key
    year, month = int(end[:4]), int(end[5:7])
    fiscal = year if month <= fy_end else year + 1
    if span == "12M":
        return f"FY{fiscal}"
    quarter = ((month - fy_end - 1) % 12) // 3 + 1
    if span == "3M":
        return f"{quarter}Q{fiscal % 100:02d}"
    if span == "6M":
        return f"{(quarter + 1) // 2}H{fiscal % 100:02d}"
    if span == "9M":
        return f"9M{fiscal % 100:02d}"
    return f"{span}{fiscal % 100:02d}"


def _money_text(value: float, currency: str) -> str:
    """An amount in currency units, as the reports write it: millions with commas."""
    mn = value / 1e6
    sign = "-" if mn < 0 else ""
    a = abs(mn)
    return f"{sign}{currency} {a:,.0f}mn" if a >= 10 else f"{sign}{currency} {a:,.1f}mn"


def _value(f: Fact, currency: str) -> float | None:
    """A fact's amount in currency units: the market block's statements are bare millions."""
    fig = f.figure
    if fig.kind == "money":
        return fig.value
    if fig.kind == "bare" and "millions" in f.table.lower():
        return fig.value * 1e6
    return None


class _Builder:
    def __init__(self, fx: facts.Facts, data):
        self.fx, self.data = fx, data
        self.currency = getattr(data, "reporting_currency", None) or getattr(data, "currency", "USD")
        self.sheet = Sheet()
        self.revenue_3m: dict[str, float] = {}     # quarter end -> revenue, from quarters()

    def cur(self, f: Fact) -> str:
        """The currency a fact's own source states ("USD 4,146mn"), else the reporting
        currency of the market data: a filing in dollars stays in dollars."""
        m = re.match(r"-?([A-Z]{3})\s", f.text)
        return m.group(1) if m else self.currency

    def add(self, group: str, name: str, metric: str, period: str, text: str, source: str,
            key: tuple | None, data_type: str = "reported") -> str | None:
        name = re.sub(r"_+", "_", name).strip("_")
        if name in RESERVED or name in self.sheet.figures:
            return None
        found = figures(text) or facts._bare(text)
        if not found:
            return None
        fact = Fact(name, found[0], source, True, f"{metric}, {period}", metric=metric,
                    period=period, table="figure sheet", period_key=key, data_type=data_type)
        fact.figure = found[0].__class__(found[0].kind, found[0].value, found[0].slack, text, 0,
                                          len(text), found[0].approx)
        self.sheet.figures[name] = fact
        self.sheet.groups.setdefault(group, []).append(name)
        return name

    def pct(self, group, name, metric, period, x, inputs: list[Fact], key, digits=1):
        if x is None:
            return
        sources = " and ".join(dict.fromkeys(f.source for f in inputs))
        self.add(group, name, metric, period, f"{x:.{digits}f}%", f"computed from {sources}", key,
                 "computed")

    # ---------------------------------------------------------------- annual
    def _annual_facts(self) -> dict[str, dict[str, Fact]]:
        """metric -> {year end: fact}, XBRL first."""
        out: dict[str, dict[str, Fact]] = defaultdict(dict)
        ranked = sorted(self.fx.by_id.values(), key=lambda f: 0 if "XBRL" in f.source else 1)
        for f in ranked:
            if not f.period_key or f.period_key[0] not in ("12M", "at") or not f.metric:
                continue
            label = _label(f.metric)
            for name, aliases in ANNUAL.items():
                if label in aliases and f.period_key[1] not in out[name]:
                    if _value(f, self.currency) is not None or name == "diluted_eps":
                        out[name][f.period_key[1]] = f
        return out

    def annual(self) -> None:
        found = self._annual_facts()
        years = sorted(found.get("revenue", {}), reverse=True)[:3]
        group = "Annual results (fiscal years)"
        fy_end = self.fx.fy_end_month
        for end in years:
            label = period_label(("12M", end), fy_end)
            key = ("12M", end)
            amounts: dict[str, float] = {}
            used: dict[str, Fact] = {}
            for name in ("revenue", "operating_income", "net_income", "diluted_eps", "ebitda",
                         "research_and_development", "sga",
                         "d_and_a", "operating_cash_flow", "capex", "buybacks", "dividends"):
                f = found.get(name, {}).get(end)
                if f is None:
                    continue
                used[name] = f
                if name == "diluted_eps":
                    text = f.text if f.text.startswith(self.currency) else f"{self.currency} {f.text}"
                    self.add(group, f"diluted_eps_{label}", "diluted EPS", label, text, f.source, key)
                    continue
                v = _value(f, self.currency)
                if name in ("capex", "buybacks", "dividends"):
                    v = abs(v)
                amounts[name] = v
                text = _money_text(v, self.cur(f))
                self.add(group, f"{name}_{label}", LABELS[name], label, text, f.source, key)
            rev = amounts.get("revenue")
            if "ebitda" not in amounts and "operating_income" in amounts and "d_and_a" in amounts:
                amounts["ebitda"] = amounts["operating_income"] + amounts["d_and_a"]
                self.add(group, f"ebitda_{label}", "EBITDA (operating income plus D&A)", label,
                         _money_text(amounts["ebitda"], self.cur(used["operating_income"])),
                         "computed from " + used["operating_income"].source, key, "computed")
            if "operating_cash_flow" in amounts and "capex" in amounts:
                amounts["fcf"] = amounts["operating_cash_flow"] - amounts["capex"]
                self.add(group, f"fcf_{label}", LABELS["fcf"], label,
                         _money_text(amounts["fcf"], self.cur(used["operating_cash_flow"])),
                         "computed from " + used["operating_cash_flow"].source, key, "computed")
            if "buybacks" in amounts and "dividends" in amounts:
                self.add(group, f"shareholder_returns_{label}", LABELS["shareholder_returns"], label,
                         _money_text(amounts["buybacks"] + amounts["dividends"], self.cur(used["buybacks"])),
                         "computed from " + used["buybacks"].source, key, "computed")
            if rev:
                inputs = [used["revenue"]]
                for name, part in (("operating_margin", "operating_income"),
                                   ("net_margin", "net_income"), ("ebitda_margin", "ebitda"),
                                   ("fcf_margin", "fcf"), ("capex_pct_revenue", "capex"),
                                   ("da_pct_revenue", "d_and_a"),
                                   ("rd_pct_revenue", "research_and_development"),
                                   ("sga_pct_revenue", "sga")):
                    if part in amounts:
                        self.pct(group, f"{name}_{label}", LABELS[name], label,
                                 amounts[part] / rev * 100, inputs + [used.get(part, used["revenue"])], key)
                prior = found["revenue"].get(f"{int(end[:4]) - 1}{end[4:]}")
                if prior is not None and _value(prior, self.currency):
                    self.pct(group, f"revenue_growth_{label}", LABELS["revenue_growth"], label,
                             (rev / _value(prior, self.currency) - 1) * 100, [used["revenue"], prior], key)
            if amounts.get("fcf") and amounts.get("net_income") and amounts["net_income"] > 0:
                self.pct(group, f"fcf_conversion_{label}", LABELS["fcf_conversion"], label,
                         amounts["fcf"] / amounts["net_income"] * 100, [used["net_income"]], key, 0)
        # Earlier years keep their headline lines; the full set is the latest year's.
        for end in years[1:]:
            label = period_label(("12M", end), fy_end)
            for name in list(self.sheet.groups.get(group, [])):
                if name.endswith("_" + label) and name[:-len(label) - 1] not in OLDER_YEARS:
                    self.sheet.groups[group].remove(name)
                    del self.sheet.figures[name]

    # ---------------------------------------------------------------- quarters
    def quarters(self) -> None:
        """The latest quarter's results against the same quarter a year before."""
        want = {"revenue": ANNUAL["revenue"], "operating_income": ANNUAL["operating_income"],
                "net_income": ANNUAL["net_income"] + ["net income attributable"],
                "diluted_eps": ["diluted eps", "diluted", "earnings per share diluted"]}
        # metric -> source -> period end -> fact: a quarter and the one a year before are
        # compared from one source, the filing first, then XBRL, then the market data.
        found: dict[str, dict[str, dict[str, Fact]]] = defaultdict(lambda: defaultdict(dict))
        for f in self.fx.by_id.values():
            if not (f.period_key and f.period_key[0] == "3M" and STATEMENT_TABLE.search(f.table)):
                continue
            label = _label(f.metric)
            for name, aliases in want.items():
                hit = label in aliases or (name == "net_income" and label.startswith("net income attributable"))
                if hit:
                    found[name][f.source].setdefault(f.period_key[1], f)
        if not found.get("revenue"):
            return
        # Every quarter's revenue, for the segment check.
        for per in found["revenue"].values():
            for end, f in per.items():
                v = _value(f, self.currency)
                if v:
                    self.revenue_3m.setdefault(end, v)
        latest = max(end for per in found["revenue"].values() for end in per)
        prior_end = f"{int(latest[:4]) - 1}{latest[4:]}"

        def rank(source: str) -> int:
            return 2 if source == "market data" else 1 if "XBRL" in source else 0

        by: dict[str, dict[str, Fact]] = {}
        for name, per_source in found.items():
            have = [src for src, ends in per_source.items() if latest in ends]
            both = [src for src in have if prior_end in per_source[src]]
            pick = sorted(both or have, key=rank)
            if pick:
                by[name] = per_source[pick[0]]
        label = period_label(("3M", latest), self.fx.fy_end_month)
        prior_label = period_label(("3M", prior_end), self.fx.fy_end_month)
        group = f"Latest quarter ({label}) against the year before ({prior_label})"
        key = ("3M", latest)
        amounts = {}
        for name in want:
            f = by.get(name, {}).get(latest)
            if f is None:
                continue
            if name == "diluted_eps":
                text = f.text if f.text.startswith(self.currency) else f"{self.currency} {f.text}"
                self.add(group, f"diluted_eps_{label}", "diluted EPS", label, text, f.source, key)
                continue
            v = _value(f, self.currency)
            if v is None:
                continue
            amounts[name] = (v, f)
            self.add(group, f"{name}_{label}", LABELS[name], label, _money_text(v, self.cur(f)),
                     f.source, key)
            p = by.get(name, {}).get(prior_end)
            pv = _value(p, self.currency) if p is not None else None
            if pv:
                self.add(group, f"{name}_{prior_label}", LABELS[name], prior_label,
                         _money_text(pv, self.cur(p)),
                         p.source, ("3M", prior_end))
                if name == "revenue" or pv > 0:
                    self.pct(group, f"{name}_growth_{label}", f"{LABELS[name]} growth on {prior_label}",
                             label, (v / pv - 1) * 100, [f, p], key)
        if "revenue" in amounts and "operating_income" in amounts:
            rev, rf = amounts["revenue"]
            op, of = amounts["operating_income"]
            self.pct(group, f"operating_margin_{label}", "operating margin", label, op / rev * 100,
                     [rf, of], key)

    # ---------------------------------------------------------------- segments
    def segments(self) -> None:
        """Each segment's revenue in the latest quarter, its growth and its share of the
        total, from the first segment table of the latest filing: the rows up to the total,
        before any row name repeats (SPGI's table goes on to operating profit under the
        same names, footnoted)."""
        tables: dict[str, list[Fact]] = defaultdict(list)
        for f in sorted(self.fx.by_id.values(), key=lambda f: int(f.id[1:]) if f.id[1:].isdigit() else 0):
            if f.period_key and f.period_key[0] == "3M" and SEGMENT_TABLE.search(f.table) \
                    and f.figure.kind == "money":
                tables[f.table].append(f)
        for table, cells in tables.items():
            latest = max(c.period_key[1] for c in cells)
            prior_end = f"{int(latest[:4]) - 1}{latest[4:]}"
            rows: dict[str, dict[str, Fact]] = {}
            order: list[str] = []
            total = None
            seen_base = set()
            for c in cells:
                base = _label(c.metric)
                if c.metric != FOOTNOTE.sub("", c.metric) and base in seen_base:
                    break       # the same names again, footnoted: the next block
                if "total" in base:
                    if total is None and c.period_key[1] == latest:
                        total = c
                    if order:
                        continue
                if NOT_A_SEGMENT.search(base) or not base:
                    continue
                if base not in rows:
                    if total is not None:
                        break   # past the total: the next block
                    rows[base] = {}
                    order.append(base)
                    seen_base.add(base)
                rows[base].setdefault(c.period_key[1], c)
            segs = [b for b in order if latest in rows[b] and prior_end in rows[b]]
            if len(segs) < 2:
                continue
            # The company's segments add up to its revenue for a quarter it reported. NKE's
            # first "segment" table was one region's sales channels, $5.1bn of an $11.2bn
            # quarter, dated by the fiscal year end instead of the quarter.
            parts = sum(rows[b][latest].figure.value for b in segs)
            revenue = self.revenue_3m.get(latest)
            if not revenue or not 0.9 <= parts / revenue <= 1.1:
                continue
            label = period_label(("3M", latest), self.fx.fy_end_month)
            group = f"Segments, {label} (from the segment table of the {cells[0].source.split(',')[0]})"
            total_v = total.figure.value if total is not None else sum(
                rows[b][latest].figure.value for b in segs)
            for b in segs:
                cur, old = rows[b][latest], rows[b][prior_end]
                name = _slug(b)
                display = FOOTNOTE.sub("", cur.metric.split(":")[-1]).strip()
                self.add(group, f"{name}_revenue_{label}", f"{display} revenue", label,
                         _money_text(cur.figure.value, self.cur(cur)),
                         cur.source, ("3M", latest))
                if old.figure.value:
                    self.pct(group, f"{name}_revenue_growth_{label}",
                             f"{display} revenue growth on a year before", label,
                             (cur.figure.value / old.figure.value - 1) * 100, [cur, old], ("3M", latest))
                if total_v:
                    self.pct(group, f"{name}_share_of_revenue_{label}",
                             f"{display} share of total revenue", label,
                             cur.figure.value / total_v * 100, [cur] + ([total] if total else []),
                             ("3M", latest), 0)
            return      # the first segment table is the one

    # ---------------------------------------------------------------- guidance
    def guidance(self) -> None:
        cells: dict[tuple, list[Fact]] = defaultdict(list)
        for f in sorted(self.fx.by_id.values(), key=lambda f: int(f.id[1:]) if f.id[1:].isdigit() else 0):
            # The outlook table itself: not a reconciliation exhibit's Low/High columns,
            # nor a 10-Q table the labeller took for guidance.
            if (f.data_type == "guidance" and f.metric and GUIDANCE_TABLE.search(f.table)
                    and not re.search(r"reconcil", f.table, re.I)
                    and not re.fullmatch(r"(?i)low|high", f.period or "")):
                cells[(f.table, f.metric, f.period)].append(f)
        seen_text: dict[str, str] = {}
        for (table, metric, basis), group_facts in cells.items():
            year = re.search(r"\b(20\d{2})\b", table)
            when = f"FY{year.group(1)}" if year else "outlook"
            text = (f"{group_facts[0].text} to {group_facts[1].text}" if len(group_facts) == 2
                    else group_facts[0].text)
            metric_label = FOOTNOTE.sub("", metric.split(":")[-1]).strip()
            base = f"guidance_{_slug(metric_label)}_{when}"
            # GAAP and adjusted the same: one figure. Different: the adjusted one named so.
            if base in seen_text:
                if seen_text[base] == text:
                    continue
                base += "_" + _slug(basis or "other")
            seen_text.setdefault(base, text)
            what = f"management's guidance for {metric_label.lower()}" + (
                f" ({basis})" if basis and basis.lower() not in ("value", "") else "")
            self.add("Management guidance", base, what, when, text, group_facts[0].source, None,
                     "guidance")

    # ---------------------------------------------------------------- market
    def market(self) -> None:
        d, cur = self.data, getattr(self.data, "currency", "USD")
        rep = self.currency
        group = "Market data and balance sheet (latest)"
        if getattr(d, "price", None):
            self.add(group, "share_price", "share price", "latest", f"{cur} {d.price:,.2f}",
                     "market data", None, "market")
        if getattr(d, "market_cap", None):
            self.add(group, "market_cap", "market capitalisation", "latest",
                     _money_text(d.market_cap, cur), "market data", None, "market")
        if getattr(d, "shares_outstanding", None):
            self.add(group, "shares_outstanding", "shares outstanding", "latest",
                     f"{d.shares_outstanding / 1e6:,.1f}mn shares", "market data", None, "market")
        cash, debt = getattr(d, "cash", None), getattr(d, "debt", None)
        if cash is not None and getattr(d, "cash_debt_known", True):
            self.add(group, "cash_balance", "cash and short-term investments", "latest",
                     _money_text(cash * 1e6, rep), "market data", None, "market")
        if debt is not None and getattr(d, "cash_debt_known", True):
            self.add(group, "total_debt", "total debt", "latest", _money_text(debt * 1e6, rep),
                     "market data", None, "market")
            if cash is not None:
                self.add(group, "net_debt", "net debt (total debt less cash)", "latest",
                         _money_text((debt - cash) * 1e6, rep), "computed from market data", None,
                         "computed")
        ttm_rev, ttm_ebitda = getattr(d, "ttm_revenue", None), getattr(d, "ttm_ebitda", None)
        ttm_ni = getattr(d, "ttm_net_income", None)
        if ttm_rev:
            self.add(group, "revenue_ttm", "revenue, last four quarters", "TTM",
                     _money_text(ttm_rev * 1e6, rep), "market data", None, "market")
        if ttm_ebitda:
            self.add(group, "ebitda_ttm", "EBITDA, last four quarters", "TTM",
                     _money_text(ttm_ebitda * 1e6, rep), "market data", None, "market")
            if ttm_rev:
                self.add(group, "ebitda_margin_ttm", "EBITDA margin, last four quarters", "TTM",
                         f"{ttm_ebitda / ttm_rev * 100:.1f}%", "computed from market data", None,
                         "computed")
            if debt is not None and cash is not None and ttm_ebitda > 0:
                self.add(group, "net_debt_to_ebitda", "net debt over EBITDA, last four quarters",
                         "TTM", f"{(debt - cash) / ttm_ebitda:.1f}x", "computed from market data",
                         None, "computed")
        fx_rate = getattr(d, "fx", 1.0) or 1.0
        if ttm_ni and ttm_ni > 0 and getattr(d, "market_cap", None):
            self.add(group, "pe_ttm", "price to earnings, last four quarters", "TTM",
                     f"{d.market_cap / (ttm_ni * 1e6 * fx_rate):.1f}x", "computed from market data",
                     None, "computed")


def build(fx: facts.Facts, data) -> Sheet:
    b = _Builder(fx, data)
    for step in (b.market, b.annual, b.quarters, b.segments, b.guidance):
        try:
            step()
        except (ValueError, KeyError, TypeError, ZeroDivisionError, IndexError):
            # One source read wrong leaves its group out, not the sheet.
            import logging
            logging.getLogger(__name__).exception("figure sheet: %s failed", step.__name__)
    return b.sheet


def attach(fx: facts.Facts, data) -> facts.Facts:
    """The sheet's figures join the fact database under their names; the writer is shown
    the sheet, not the database. The market data's statements take the filings' figures
    where they differ (`reconcile`), so the model is built on what the text cites."""
    sheet = build(fx, data)
    fx.by_id.update(sheet.figures)
    fx.sheet = sheet
    reconcile(sheet, data)
    return fx


# The market data's annual statements are Yahoo's, which standardises them: ESLT's FY2025
# operating income was USD 687mn there and USD 671mn in its 20-F, and on 2026-10-07 the
# model's base year printed an 8.7% margin beside a text citing the filing's 8.5%.
RECONCILED_ROWS = {"revenue": "Total Revenue", "operating_income": "Operating Income",
                   "ebitda": "EBITDA"}
RECONCILED_TITLE = "## Statements matched to the company's filings"
RECONCILE_TOLERANCE = 0.005


def reconcile(sheet: Sheet, data) -> list[tuple[str, str, float, float, str]]:
    """Put the company's filed revenue, operating income and EBITDA into the market data's
    annual income statement where Yahoo's differ by more than half a percent, and list
    each change in the block. Returns (line item, year, Yahoo's, filed, source)."""
    block = getattr(data, "prompt_block", "") or ""
    if RECONCILED_TITLE in block:
        return []
    import market_data
    dates, rows = market_data.statement_rows(block, "### Income statement")
    currency = getattr(data, "reporting_currency", "") or getattr(data, "currency", "")
    changes = []
    for name, row in RECONCILED_ROWS.items():
        for i, date_ in enumerate(dates):
            f = sheet.figures.get(f"{name}_FY{date_[:4]}")
            yahoo = rows.get(row, [None] * len(dates))[i] if i < len(rows.get(row, [])) else None
            if (f is None or yahoo is None or "market data" in f.source.lower()
                    or not f.text.lstrip("-").startswith(currency) or f.figure.kind != "money"):
                continue
            filed = f.figure.value / 1e6 * (-1 if f.text.startswith("-") else 1)
            if abs(filed - yahoo) <= max(abs(filed) * RECONCILE_TOLERANCE, 1.0):
                continue
            block = market_data.set_cell(block, "### Income statement", row, i, filed)
            changes.append((row, f"FY{date_[:4]}", yahoo, filed, f.source))
    if not changes:
        return []
    by_year = {(row, year): filed for row, year, _, filed, _ in changes}
    data.history = [(label, by_year.get(("Total Revenue", label), revenue),
                     by_year.get(("EBITDA", label), ebitda))
                    for label, revenue, ebitda in (getattr(data, "history", None) or [])]
    lines = [RECONCILED_TITLE, "",
             "Yahoo's figures were replaced in the statements above by the company's own, "
             "which the model and the figure sheet use.", "",
             f"| Line item | Year | Yahoo ({currency} mn) | Filed ({currency} mn) | Source |",
             "| --- | --- | --- | --- | --- |"]
    lines += [f"| {row} | {year} | {yahoo:,.0f} | {filed:,.0f} | {source} |"
              for row, year, yahoo, filed, source in changes]
    data.prompt_block = block + "\n\n" + "\n".join(lines)
    return changes
