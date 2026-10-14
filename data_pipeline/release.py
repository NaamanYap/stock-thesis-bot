"""The earnings release's tables, read into the fact database.

A release (the 8-K's exhibit 99) is not tagged, so its figures are read from the table
they sit in: the row names the figure, the column heading and the table's title the
period (periods.from_heading). A cell whose period cannot be read is left out rather than
guessed. Releases matter for two things the 10-Q does not give: management's guidance,
and a quarter reported before its 10-Q is filed. Everything else they state is kept too
(adjusted and organic figures), as table facts below the tagged figures in authority.
"""

from __future__ import annotations

import re

from . import filing_tables
from .facts_db import Fact, FactDB, slug
from .periods import Period, from_heading, full_year_in

NUM = r"~?(?:[A-Z]{3}\s)?-?\d[\d,]*(?:\.\d+)?"
CELL = re.compile(rf"^(?P<approx>~)?(?P<low>{NUM})(?:\s*(?:mn|k|bn))?%?(?:\s*(?:to|-|–)\s*"
                  rf"(?P<high>{NUM}))?\s*(?P<suffix>mn|k|bn|million|billion|thousand|%|bps|"
                  rf"basis points|shares|mn shares|k shares|bn shares)?$", re.I)
SCALE = {"k": 1e3, "thousand": 1e3, "mn": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}

# Statement rows that are a canonical metric, matched on the row's own label.
ROWS = {
    "revenue": re.compile(r"^(?:total )?(?:net )?(?:revenues?|sales)$", re.I),
    "operating_income": re.compile(r"^(?:operating (?:income|profit)|income from operations)$",
                                   re.I),
    "net_income": re.compile(r"^net income attributable to (?!.*(?:non-?controlling|minority))"
                             r".*$", re.I),
    "diluted_eps": re.compile(r"(?:^|: )diluted(?: eps| earnings per share)?$|^diluted eps$",
                              re.I),
    "operating_cash_flow": re.compile(r"^(?:net )?cash provided by (?:\(used in\) )?operating "
                                      r"activities$", re.I),
    "capex": re.compile(r"^capital expenditures?$", re.I),
}
STATEMENT = re.compile(r"statements? of (?:income|operations|earnings)|income statement", re.I)
CASH_FLOW = re.compile(r"cash flows?", re.I)
SEGMENT = re.compile(r"\bsegment", re.I)
GUIDANCE = re.compile(r"outlook|guidance", re.I)
RECONCILIATION = re.compile(r"reconcil", re.I)
NOT_A_SEGMENT = re.compile(r"intersegment|elimination|corporate|unallocated|total|transfers|"
                           r"allocation|recast|pro forma|\(", re.I)


def parse_cell(text: str, money: str) -> tuple[float, float | None, str] | None:
    """(low, high or None, unit) of a cell as filing_tables printed it: "USD 4,146mn",
    "10%", "USD 4.12", "5.9% to 7.9%", "USD 205 to USD 215 million", "335 to 360 bps"."""
    t = text.strip()
    m = CELL.match(t)
    if not m:
        return None
    def num(s: str) -> float:
        return float(re.sub(r"[^\d.\-]", "", s.replace("~", "")))
    low = num(m.group("low"))
    high = num(m.group("high")) if m.group("high") else None
    suffix = (m.group("suffix") or "").lower()
    code = re.match(r"\s*-?([A-Z]{3})\s", t)
    has_currency = bool(code or re.search(r"[A-Z]{3}\s", t))
    # The cell's own currency: a USD figure in a EUR filer's table is not EUR.
    money = code.group(1) if code else money
    if suffix == "%" or t.endswith("%"):
        return low, high, "%"
    if suffix in ("bps", "basis points"):
        return low, high, "bps"
    if "shares" in suffix:
        scale = SCALE.get(suffix.split()[0], 1.0) if " " in suffix else 1.0
        return low * scale, None if high is None else high * scale, "shares"
    if suffix in SCALE or re.search(r"\d(?:mn|k|bn)\b", t):
        unit_suffix = suffix or re.search(r"\d(mn|k|bn)\b", t).group(1)
        scale = SCALE[unit_suffix]
        return low * scale, None if high is None else high * scale, money if has_currency else "count"
    if has_currency:
        return low, high, f"{money}/share"
    return low, high, "count"


def _period_label_ok(period: Period | None) -> bool:
    return period is not None and period.end is not None


def from_release(db: FactDB, tables: list[filing_tables.Table], source: str, filed: str,
                 url: str = "") -> int:
    """Add the release's figures. `source` names it ("earnings release for 2Q26, filed
    2026-07-28")."""
    added = 0
    used_canonical: set[str] = set()     # "statement", "segment", "cash flow": first table only
    # Segment names the 10-Q tags: a release row is a segment only under one of these. Where
    # nothing is tagged, rows count until the table's total.
    tagged = {slug(s) for names in db.segments("revenue").values() for s in names}
    for n, table in enumerate(tables, 1):
        title = table.title
        guidance = bool(GUIDANCE.search(title))
        reconciliation = bool(RECONCILIATION.search(title))
        if guidance:
            added += _guidance(db, table, source, filed, url, reconciliation)
            continue
        kind = ("statement" if STATEMENT.search(title) else
                "cash flow" if CASH_FLOW.search(title) else
                "segment" if SEGMENT.search(title) else "")
        canonical = kind and kind not in used_canonical
        took_canonical = False
        # The title's last words name a per-share table; earlier words can belong to the
        # table before it ("Diluted earnings per share" as its last row).
        # "(in € millions, except share and per share data)" names the exception, not the
        # table: SPOT's 6-K statement of operations read as per share, revenue EUR 4,777.
        last = re.sub(r"except\b[^)]*", "", " ".join(title.split()[-8:]), flags=re.I)
        per_share = bool(re.search(r"per share|\bEPS\b", last, re.I))
        # A row name met again in the same column is the table's next block (SPGI's
        # segment table repeats the names for operating profit): only the first is the
        # canonical figure, and every occurrence gets its own id.
        first: set[tuple] = set()
        occurrences: dict[str, int] = {}
        past_total: set[str] = set()
        # "Net income" is the shareholders' only where no row says "attributable to".
        attributable = any(ROWS["net_income"].search(c.row.split(": ")[-1]) for c in table.cells)
        for cell in table.cells:
            period = from_heading(cell.column, title, db.fy_end)
            if not _period_label_ok(period):
                continue
            parsed = parse_cell(cell.text, db.currency)
            if parsed is None:
                continue
            low, high, unit = parsed
            if per_share and unit == db.currency:
                # A per-share table "in millions" prints USD 4.80 as "USD 4.80mn".
                scale = next((v for k, v in SCALE.items() if cell.text.endswith(k)), 1.0)
                if abs(low) / scale < 10000:
                    low, high = low / scale, None if high is None else high / scale
                    unit = f"{db.currency}/share"
            row = re.sub(r"\s*\((?:[a-z]|\d{1,2})\)\s*$", "", cell.row).strip()
            own = row.split(": ")[-1]
            metric, label, segment = "", own, ""
            seen_key = (own.lower(), cell.column)
            repeat = seen_key in first
            first.add(seen_key)
            if re.match(r"(?i)total", own):
                past_total.add(cell.column)
            if canonical and not repeat:
                if kind == "segment":
                    if re.search(r"revenue", cell.column, re.I) and unit == db.currency \
                            and not NOT_A_SEGMENT.search(own) \
                            and (slug(own) in tagged if tagged else cell.column not in past_total):
                        metric, segment = "revenue", own
                else:
                    metric = next((m for m, rx in ROWS.items() if rx.search(own)
                                   or (m == "diluted_eps" and rx.search(row))), "")
                    if not metric and kind == "statement" and not attributable                             and re.fullmatch(r"(?i)net income", own):
                        metric = "net_income"
                    if metric == "diluted_eps" and not unit.endswith("/share"):
                        metric = ""
                    if metric in ("revenue", "operating_income", "net_income",
                                  "operating_cash_flow", "capex") and unit != db.currency:
                        metric = ""
            location = f"table '{title[-70:]}', row '{cell.row}', column '{cell.column}'"
            if metric:
                took_canonical = True
                fid = db.make_id(metric, period, segment)
                label = {"revenue": "Revenue", "operating_income": "Operating income",
                         "net_income": "Net income attributable to shareholders",
                         "diluted_eps": "Diluted EPS", "operating_cash_flow": "Operating cash flow",
                         "capex": "Capital expenditure"}[metric]
                value = abs(low) if metric == "capex" else low
            else:
                metric = "table_" + slug(row)[:60]
                base = f"t{n}_{slug(row)[:60]}_{period.slug}"
                occurrences[base] = occurrences.get(base, 0) + 1
                fid = base if occurrences[base] == 1 else f"{base}_{occurrences[base]}"
                value = low
            db.add(Fact(
                id=fid, metric=metric, label=label, value=value, unit=unit, period=period,
                data_type="reported", source=source, source_kind="filing_table",
                source_date=filed, source_url=url, source_location=location, segment=segment,
                segment_axis="segment" if segment else "",
                value_high=high))
            added += 1
        if took_canonical:
            used_canonical.add(kind)
    return added


def _guidance(db: FactDB, table: filing_tables.Table, source: str, filed: str, url: str,
              reconciliation: bool) -> int:
    """Management's outlook: one fact per row and basis (GAAP, adjusted), the range kept.
    A reconciliation's Low/High columns give one range per row; only its per-share rows
    are read, since its other rows are per-share adjustments printed in the table's scale."""
    period = full_year_in(table.title, db.fy_end)
    if period is None:
        return 0
    added = 0
    rows: dict[str, dict[str, str]] = {}
    for cell in table.cells:
        rows.setdefault(cell.row, {})[cell.column] = cell.text
    seen: dict[str, str] = {}
    for row, cols in rows.items():
        own = re.sub(r"\s*\((?:[a-z]|\d{1,2})\)\s*$", "", row.split(": ")[-1]).strip()
        if reconciliation:
            if not re.search(r"\bEPS\b|per share", own, re.I) or "Low" not in cols:
                continue
            lo = parse_cell(cols["Low"], db.currency)
            hi = parse_cell(cols.get("High", cols["Low"]), db.currency)
            if not lo or not hi:
                continue
            ranges = [("", lo[0], hi[0], f"{db.currency}/share", "Low/High")]
        else:
            ranges = []
            for basis, text in cols.items():
                p = parse_cell(text, db.currency)
                if p:
                    ranges.append((basis, p[0], p[1], p[2], basis))
        for basis, low, high, unit, column in ranges:
            name = f"guidance_{slug(own)[:48]}"
            key = f"{low}|{high}|{unit}"
            if name in seen and seen[name] != key:
                name += "_" + slug(basis or "other")
            elif name in seen:
                continue
            seen.setdefault(name, key)
            label = f"Guidance: {own}" + (f" ({basis})" if basis and basis.lower() not in
                                          ("value", "gaap") else "")
            db.add(Fact(
                id=f"{name}_{period.slug}", metric=name, label=label, value=low,
                value_high=high, unit=unit, period=period, data_type="guidance",
                source=source, source_kind="filing_table", source_date=filed, source_url=url,
                source_location=f"table '{table.title[-70:]}', row '{row}', column '{column}'"))
            added += 1
    return added
