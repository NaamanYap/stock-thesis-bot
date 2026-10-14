"""The fact database: every number a report may state, as a structured record.

Before the analyst sees anything, each figure exists here with its value, unit, period,
source and whether it was reported or calculated. The analyst and the writer cite facts
by id ("revenue_FY2025"); the code prints the value and the source. Nothing downstream
may type a number that is not a fact here or a calculation over facts.

Code fills every field. A field the code cannot determine stays empty rather than being
guessed. When two sources give the same figure (same metric, segment and period) and
disagree, the more authoritative one wins and the conflict is recorded; when they are
equally authoritative the figure is excluded, so a disputed number can never carry the
thesis.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date

import config
from .periods import Period

# Lower is more trusted. A calculation takes the least trusted of its inputs.
AUTHORITY = {
    "sec_inline": 0,        # the latest 10-Q/10-K's own tagged figures
    "sec_company_facts": 0,  # the SEC's company facts API: every tagged figure ever filed
    "filing_table": 1,      # a table in a filing that is not tagged (the earnings release)
    "market": 2,            # Yahoo Finance
    "third_party": 3,
}
DATA_TYPES = ("reported", "calculated", "guidance", "market")
CURRENCY = re.compile(r"^[A-Z]{3}$")


def slug(text: str) -> str:
    text = text.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


@dataclass
class Fact:
    id: str
    metric: str                 # canonical key ("revenue") or the tag's own words
    label: str                  # what it measures, for a reader: "Revenue"
    value: float
    unit: str                   # "USD", "USD/share", "%", "x", "shares", "count", "pure"
    period: Period
    data_type: str              # reported, calculated, guidance, market
    source: str                 # "10-Q filed 2026-07-28", "Yahoo Finance", "calculated"
    source_kind: str            # a key of AUTHORITY
    source_date: str = ""       # ISO date the source was filed or read
    source_url: str = ""
    source_location: str = ""   # the tag and context, or the table, row and column
    entity: str = ""            # the company the figure is about
    segment: str = ""           # a business segment, product line or region; "" for the group
    segment_axis: str = ""      # "segment", "product", "geography"
    value_high: float | None = None     # the top of a range (guidance)
    formula: str = ""           # calculations: how, in words over input ids
    inputs: list[str] = field(default_factory=list)
    concept: str = ""           # the XBRL tag, when there is one
    start: str = ""             # ISO first day of a reported duration; "" for a balance
    form: str = ""              # the filing it came from: "10-K", "10-Q", "8-K"
    corroborated_by: list[str] = field(default_factory=list)   # other sources that agree
    excluded: str = ""          # why the figure may not be used, when it may not

    @property
    def authority(self) -> int:
        return AUTHORITY.get(self.source_kind, 3)

    @property
    def usable(self) -> bool:
        return not self.excluded

    def text(self) -> str:
        """The figure as a report prints it."""
        low = format_value(self.value, self.unit)
        if self.value_high is not None and self.value_high != self.value:
            return f"{low} to {format_value(self.value_high, self.unit)}"
        return low

    def citation(self) -> str:
        where = f", {self.source_location}" if self.source_location and \
            self.source_kind == "filing_table" else ""
        return f"{self.source}{where}"

    def to_json(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if v not in ("", [], None) or k == "value"}
        d["period"] = self.period.to_json()
        d["text"] = self.text()
        return d

    @staticmethod
    def from_json(d: dict, fy_end: int) -> "Fact":
        d = dict(d)
        p = d.pop("period")
        d.pop("text", None)
        d["period"] = Period(p["span"], date.fromisoformat(p["end"]) if p.get("end") else None,
                             fy_end)
        return Fact(**d)


def format_value(value: float, unit: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n.a."
    sign = "-" if value < 0 else ""
    a = abs(value)
    if CURRENCY.match(unit):
        if a >= 1e9:
            return f"{sign}{unit} {a / 1e9:,.2f}bn"
        if a >= 1e6:
            return f"{sign}{unit} {a / 1e6:,.0f}mn" if a >= 1e7 else f"{sign}{unit} {a / 1e6:,.1f}mn"
        return f"{sign}{unit} {a:,.0f}"
    if unit.endswith("/share"):
        return f"{sign}{unit.split('/')[0]} {a:,.2f}"
    if unit == "%":
        return f"{sign}{a:.1f}%"
    if unit == "bps":
        return f"{sign}{a:,.0f} bps"
    if unit == "x":
        return f"{sign}{a:.1f}x"
    if unit == "shares":
        return f"{sign}{a / 1e6:,.1f}mn shares"
    if unit == "years":
        return f"{sign}{a:.1f} years"
    if a >= 1e6:
        return f"{sign}{a / 1e6:,.1f}mn"
    return f"{sign}{a:,.4g}"


def close(a: float, b: float, tolerance: float | None = None) -> bool:
    """The same figure, allowing for rounding: two sources state $4,146mn and $4.15bn."""
    tolerance = config.CONFLICT_TOLERANCE if tolerance is None else tolerance
    if a == b:
        return True
    scale = max(abs(a), abs(b))
    return abs(a - b) <= tolerance * scale


@dataclass
class Conflict:
    fact_id: str
    kept: str               # the source whose value stands, or "" when the figure was excluded
    values: list[tuple[str, float]]     # (source, value) for every source
    note: str

    def to_json(self) -> dict:
        return {"fact_id": self.fact_id, "kept": self.kept,
                "values": [[s, v] for s, v in self.values], "note": self.note}


@dataclass
class FactDB:
    symbol: str
    entity: str = ""
    currency: str = "USD"           # the reporting currency
    trading_currency: str = "USD"
    fy_end: int = 12
    built: str = field(default_factory=lambda: date.today().isoformat())
    facts: dict[str, Fact] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)       # what could not be read, and why

    # ---------------------------------------------------------------- adding
    def make_id(self, metric: str, period: Period, segment: str = "") -> str:
        parts = [slug(segment)] if segment else []
        parts.append(metric)
        if period.span != "latest":
            parts.append(period.slug)
        return "_".join(p for p in parts if p)

    def add(self, fact: Fact) -> Fact:
        """Add a fact, settling a clash with one already held under the same id."""
        if not fact.entity:
            fact.entity = self.entity
        held = self.facts.get(fact.id)
        if held is None:
            self.facts[fact.id] = fact
            return fact
        if held.period.key != fact.period.key or held.unit != fact.unit:
            # The same id for a different figure: keep both, the newcomer renamed.
            n = 2
            while f"{fact.id}_{n}" in self.facts:
                n += 1
            fact.id = f"{fact.id}_{n}"
            self.facts[fact.id] = fact
            return fact
        if close(held.value, fact.value) and (held.value_high is None or fact.value_high is None
                                              or close(held.value_high, fact.value_high)):
            winner, other = (held, fact) if held.authority <= fact.authority else (fact, held)
            winner.corroborated_by = list(dict.fromkeys(
                winner.corroborated_by + [other.source] + other.corroborated_by))
            if winner is fact:
                fact.id = held.id
                self.facts[fact.id] = fact
            return winner
        values = [(held.source, held.value), (fact.source, fact.value)]
        existing = next((c for c in self.conflicts if c.fact_id == fact.id), None)
        if held.authority == fact.authority and held.source_date == fact.source_date:
            held.excluded = (f"sources disagree: {held.source} says {held.text()}, "
                             f"{fact.source} says {fact.text()}")
            note = "equally authoritative sources disagree; the figure is excluded"
            kept = ""
        else:
            if held.authority != fact.authority:
                held_wins = held.authority < fact.authority
                why = "is more authoritative than"
            else:
                # The same kind of source, filed later: a restatement or a recast.
                held_wins = held.source_date > fact.source_date
                why = "was filed later than (a restatement or recast of)"
            winner, loser = (held, fact) if held_wins else (fact, held)
            if winner is fact:
                fact.id = held.id
                self.facts[fact.id] = fact
            note = f"{winner.source} ({winner.text()}) {why} {loser.source} ({loser.text()})"
            kept = winner.source
        if existing:
            existing.values += [v for v in values if v not in existing.values]
            existing.note, existing.kept = note, kept
        else:
            self.conflicts.append(Conflict(fact.id, kept, values, note))
        return self.facts[fact.id]

    # ---------------------------------------------------------------- reading
    def get(self, fact_id: str) -> Fact | None:
        f = self.facts.get(fact_id)
        return f if f is not None and f.usable else None

    def find(self, metric: str, period: Period | None = None, segment: str = "",
             span: str | None = None) -> list[Fact]:
        """Usable facts of a metric, newest period first."""
        out = [f for f in self.facts.values()
               if f.usable and f.metric == metric and f.segment == segment
               and (period is None or f.period.key == period.key)
               and (span is None or f.period.span == span)]
        return sorted(out, key=lambda f: (f.period.end or date.max, -f.authority), reverse=True)

    def one(self, metric: str, period: Period, segment: str = "") -> Fact | None:
        found = self.find(metric, period, segment)
        return found[0] if found else None

    def series(self, metric: str, span: str, segment: str = "") -> list[Fact]:
        """One fact per period of `span`, oldest first."""
        seen: dict[tuple, Fact] = {}
        for f in self.find(metric, segment=segment, span=span):
            seen.setdefault(f.period.key, f)
        return sorted(seen.values(), key=lambda f: f.period.end)

    def latest(self, metric: str, span: str, segment: str = "") -> Fact | None:
        s = self.series(metric, span, segment)
        return s[-1] if s else None

    def segments(self, metric: str = "revenue") -> dict[str, list[str]]:
        """Segment names by axis, for the metric."""
        out: dict[str, list[str]] = {}
        for f in self.facts.values():
            if f.usable and f.metric == metric and f.segment:
                names = out.setdefault(f.segment_axis or "segment", [])
                if f.segment not in names:
                    names.append(f.segment)
        return out

    # ---------------------------------------------------------------- saving
    def to_json(self) -> dict:
        return {"symbol": self.symbol, "entity": self.entity, "currency": self.currency,
                "trading_currency": self.trading_currency, "fy_end": self.fy_end,
                "built": self.built, "facts": [f.to_json() for f in self.facts.values()],
                "conflicts": [c.to_json() for c in self.conflicts], "gaps": self.gaps}

    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=1, default=str)

    @staticmethod
    def from_json(d: dict) -> "FactDB":
        db = FactDB(d["symbol"], d.get("entity", ""), d.get("currency", "USD"),
                    d.get("trading_currency", "USD"), d.get("fy_end", 12), d.get("built", ""))
        for fd in d.get("facts", []):
            f = Fact.from_json(fd, db.fy_end)
            db.facts[f.id] = f
        db.conflicts = [Conflict(c["fact_id"], c["kept"], [tuple(v) for v in c["values"]],
                                 c["note"]) for c in d.get("conflicts", [])]
        db.gaps = list(d.get("gaps", []))
        return db

    # ---------------------------------------------------------------- display
    def table(self, ids: list[str] | None = None) -> str:
        """One line per fact: id, what it is, period, value, source. What the analyst reads."""
        rows = [self.facts[i] for i in ids] if ids else list(self.facts.values())
        lines = []
        for f in rows:
            if not f.usable:
                continue
            kind = "" if f.data_type == "reported" else f" [{f.data_type}]"
            seg = f" — {f.segment}" if f.segment else ""
            lines.append(f"{f.id} | {f.label}{seg} | {f.period.label} | {f.text()} | "
                         f"{f.citation()}{kind}")
        return "\n".join(lines)
