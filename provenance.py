"""Every figure in the prose must come from something the writer was shown.

consistency.py looks for wrong figures one phrasing at a time ("FCF margin of N% by
FY2030"), and every pitch found a phrasing it did not know. This check runs the other way
round: a figure is accepted only when it can be traced to a source — a computed value, a
model input, the market data, or the research notes the writer read. Anything else was
typed from memory or worked out in the writer's head, and the reader cannot check it.
NEE's pitch of 2026-09-26 called the stock "fully valued at 19x EV/EBITDA" at 15.0x,
said consensus implied "~18x", and priced a 50bp ROE cut at "~$425mn" — none of it from
anywhere.

A figure matches a source when it has the same kind (a multiple, a percentage, an amount
of money, a physical quantity) and the same value to the precision it was written with.
Research notes are long and full of numbers, so a figure matched only there must also
share a word with the note it came from: "12%" is not sourced by any 12% in 80,000
characters, only by one that sits near "margin" when the prose is about a margin.

A figure the writer presents as its own estimate ("I assume", "my estimate") needs no
source, except a multiple: every multiple a pitch needs is computed.
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass

from valuation import _norm_year, value_at, with_unit as _with_unit

# ---------------------------------------------------------------------------- figures

CURRENCY = (r"(?:US\$|[A-Z]{2}\$|\$|€|£|¥|"
            r"\b(?:USD|EUR|GBP|JPY|TWD|HKD|CNY|RMB|SGD|KRW|INR|CHF|CAD|AUD|SEK|DKK|NOK)\s?)")
SCALES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "mm": 1e6, "million": 1e6,
          "b": 1e9, "bn": 1e9, "billion": 1e9, "tn": 1e12, "trillion": 1e12,
          # Capital T only, as JPM's "~$4.0T": a lowercase "5t" is tonnes.
          "t": 1e12}
PHYSICAL = {"gw": ("W", 1e9), "mw": ("W", 1e6), "kw": ("W", 1e3),
            "twh": ("Wh", 1e12), "gwh": ("Wh", 1e9), "mwh": ("Wh", 1e6)}
# Spelled out, as the notes often are: NEE's "110 gigawatts (GW) of generation".
PHYSICAL_WORDS = {"gigawatt": "gw", "megawatt": "mw", "kilowatt": "kw",
                  "terawatt-hour": "twh", "gigawatt-hour": "gwh", "megawatt-hour": "mwh"}
NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
UNIT = (r"%|\s?(?:percent|per cent)\b|x(?![A-Za-z])|\s?times\b|\s?(?:bps|bp|pp|pts)\b|"
        r"\s?(?:trillion|billion|million|thousand|tn|bn|mn|mm|k|m|b)\b|(?-i:T)\b|"
        r"\s?(?:GW|MW|kW|TWh|GWh|MWh)\b|"
        r"\s?(?:(?:giga|mega|kilo)watt|(?:tera|giga|mega)watt[- ]hour)s?\b")
FIGURE = re.compile(
    r"(?P<cur>" + CURRENCY + r")?"
    # "c.52%" is a figure, though a dot comes right before it.
    r"(?:(?<=\bc\.)|(?<![\w.,/]))(?P<num>" + NUMBER + r")"
    r"(?P<unit>" + UNIT + r")?"
    # Not a name: "10-K", "765-kV", "2nd". "$1.5-2bn" is a range, not "$1".
    r"(?!-?[A-Za-z]|\.?\d)", re.I)
# "18-20%", "$12-13bn", "10.5 to 11.0%": the first end takes the unit of the second.
# Not "and": in "CAGR through 2032 and 9%+", 2032 is a year.
RANGE_JOIN = re.compile(r"^\s?(?:-|–|—|to)\s?$", re.I)
APPROX = re.compile(r"(?:~|≈|\bc\.|\bcirca|\babout|\baround|\broughly|\bapproximately|"
                    r"\bnearly|\balmost|\bover|\bmore than|\bless than|\bunder|\bup to|"
                    r"\bat least|\bat most|>|<|\bsome|\bexceed\w*|\babove|\bbelow)\s*\W*$",
                    re.I)
YEAR = re.compile(r"^(?:19|20)\d{2}$")
# Names written like figures: the US 340B drug-pricing programme is not 340 billion (NVO's
# of 2026-10-11 flagged it eleven times as a typed figure).
NAMED = re.compile(r"^(?:340B)$")
# Pool figures carry their own rounding: 8,661 printed as 8.7bn in the prose is exact.
POOL_TOLERANCE = 0.005
RATE_SLACK_PP = 0.15
# "~" against the one figure it names: ESLT's "FCF margins of ~5.5% in FY2026" for a
# model at 5.9% is a rounding, not a contradiction.
APPROX_SHARE = 0.03
MARGIN_PP = 1.0


@dataclass
class Figure:
    kind: str        # "x", "%", "pp", "money", "count", "W", "Wh", "bare"
    value: float     # absolute; money in currency units, counts in units, W or Wh
    slack: float     # half a unit in the last digit written, in the same units
    text: str        # as written
    start: int
    end: int
    approx: bool = False


def _kind(cur: str | None, unit: str) -> tuple[str, float]:
    """The kind of a figure and what one written unit is worth."""
    u = unit.strip().lower().replace(" ", "-")
    u = PHYSICAL_WORDS.get(u[:-1] if u.endswith("s") else u, u)
    if u in ("%", "percent", "per-cent"):
        return "%", 1.0
    if u in ("x", "times"):
        return "x", 1.0
    if u in ("bps", "bp"):
        return "pp", 0.01
    if u in ("pp", "pts"):
        return "pp", 1.0
    if u in SCALES:
        # "$2.25bn" is money; "6 million accounts" and "+90k/quarter" are counts.
        return ("money" if cur else "count"), SCALES[u]
    if u in PHYSICAL:
        base, factor = PHYSICAL[u]
        return base, factor
    if cur:
        return "money", 1.0
    return "bare", 1.0


def figures(text: str) -> list[Figure]:
    """Every figure in `text` with a kind; bare whole numbers (years, counts, list
    markers, names like S&P 500) are left out unless written with thousands commas."""
    out: list[Figure] = []
    matches = list(FIGURE.finditer(text))
    for i, m in enumerate(matches):
        cur, num, unit = m.group("cur"), m.group("num"), m.group("unit") or ""
        # A range's first end takes the unit and currency of its second.
        if not unit and i + 1 < len(matches) and not YEAR.match(num):
            nxt = matches[i + 1]
            if nxt.group("unit") and RANGE_JOIN.match(text[m.end():nxt.start(
                    "cur" if nxt.group("cur") else "num")]):
                unit = nxt.group("unit")
                cur = cur or nxt.group("cur")
        if not cur and NAMED.match(m.group(0).strip()):
            continue
        kind, factor = _kind(cur, unit)
        if kind == "bare" and "." not in num and "," not in num:
            continue
        if kind == "bare" and YEAR.match(num.replace(",", "")):
            continue
        value = float(num.replace(",", ""))
        decimals = len(num.split(".")[1]) if "." in num else 0
        slack = 0.5 * 10 ** -decimals * factor
        approx = bool(APPROX.search(text[max(0, m.start() - 14):m.start()]))
        out.append(Figure(kind, value * factor, slack, m.group(0).strip(), m.start(), m.end(),
                          approx))
    return out


# ---------------------------------------------------------------------------- the pool

STOP = set("""a an the and or of to in on for by with at from as is are was were be been
being it its this that these those than then which who whom whose what when where while
into over under about above below up down out off per vs via our my we i you they their
has have had will would could should may might can not no nor but so if also more most
less least very such each other some any all both only same new year years quarter
quarters fy total company company's share shares""".split())
WORD = re.compile(r"[A-Za-z][A-Za-z&'-]+")
# One name for each thing the prose abbreviates and the statements spell out: NEE's
# "FY2025 FCF was $3.2bn" was flagged against a "Free Cash Flow | 3,211" row.
SYNONYMS = [(re.compile(p, re.I), w) for p, w in (
    (r"free[- ]cash[- ]flows?", "fcf"), (r"capital (?:expenditures?|spending)", "capex"),
    (r"earnings per share", "eps"), (r"operating (?:income|profit)", "ebit"),
    (r"(?:total )?revenues?|\bsales\b|\bturnover\b", "revenue"),
    (r"net (?:income|profit)|\bearnings\b", "netincome"),
    (r"dividends? per share|\bDPS\b", "dividend"),
    (r"price[- ]to[- ]earnings|\bP/E\b", "peratio"), (r"enterprise value|\bEV\b", "entvalue"),
)]


YEAR_TOKEN = re.compile(r"\b(?:FY\s?'?(\d{4}|\d{2})|((?:19|20)\d{2}))(?!\d|,\d)(?:[EFA]\b)?",
                        re.I)


def _year_of(m: re.Match) -> str:
    digits = m.group(1) or m.group(2)
    return digits if len(digits) == 4 else "20" + digits


def _year_token(year: str) -> str:
    return f"y{year}"


def _years(text: str) -> set[str]:
    return {_year_token(_year_of(m)) for m in YEAR_TOKEN.finditer(text)}


def _words(text: str) -> set[str]:
    for pattern, word in SYNONYMS:
        text = pattern.sub(f" {word} ", text)
    out = set()
    for w in WORD.findall(text):
        w = w.lower().strip("'-")
        if len(w) < 3 or w in STOP:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


class Pool:
    """Figures a pitch may cite. `add` takes trusted values; `add_notes` research text."""

    def __init__(self) -> None:
        self.values: dict[str, list[float]] = {}
        self.notes: dict[str, list[tuple[float, frozenset]]] = {}
        self.forecast: set[str] = set()      # the model's forecast years, "2026"...
        # The same, by metric: ("fcf_margin", "2030") -> kind -> values. The bad case's
        # figures sit apart, accepted only in a paragraph about it.
        self.metric_years: dict[tuple[str, str], dict[str, list[float]]] = {}
        self.downside_years: dict[tuple[str, str], dict[str, list[float]]] = {}
        # Segment and driver names the prose may use as a metric: (pattern, key).
        self.names: list[tuple[re.Pattern, str]] = []
        # Multiples the code computed, by what they divide: "ev_ebitda", "pe", "fcf"...
        self.multiples: dict[str, list[float]] = {}
        self.company_words: set[str] = set()    # "Broadcom's revenue" is the total
        self.terminal_multiples: dict[str, float] = {}
        # Growth rates by metric over any span of years, and each metric's CAGR over the
        # whole forecast, for stated growth rates ("a 3.5% CAGR").
        self.growth: dict[str, list[float]] = {}
        self.downside_growth: dict[str, list[float]] = {}
        self.cagr: dict[str, float] = {}
        self._sorted = True

    def add_metric(self, metric: str, year: str, kind: str, value: float | None,
                   downside: bool = False) -> None:
        if value is None or not math.isfinite(value):
            return
        target = self.downside_years if downside else self.metric_years
        target.setdefault((metric, year), {}).setdefault(kind, []).append(abs(value))

    def matches_growth(self, f: Figure, metric: str, downside: bool) -> bool:
        """Any growth rate the model gives this metric, over any span of its years."""
        lo, hi = self.bounds(f)
        lo, hi = lo - RATE_SLACK_PP, hi + RATE_SLACK_PP
        sources = [self.growth] + ([self.downside_growth] if downside else [])
        return any(lo <= x <= hi for source in sources for x in source.get(metric, ()))

    def describe_growth(self, metric: str) -> str:
        rates = sorted(set(round(x, 1) for x in self.growth.get(metric, ())))
        name = metric.split(":", 1)[-1].replace("_", " ")
        cagr = self.cagr.get(metric)
        return (f"the model's {name} grows {cagr:.1f}% a year over the forecast"
                if cagr is not None else f"the model's {name} growth rates are "
                + ", ".join(f"{x}%" for x in rates[:6]))

    def describe_metric(self, metric: str, year: str) -> str:
        """The model's figure for a metric and year, for the rewrite request."""
        values = self.metric_years.get((metric, year), {})
        name = metric.split(":", 1)[-1].replace("_", " ")
        if metric.endswith("_margin") and values.get("%"):
            return f"the model has {name} {values['%'][0]:.1f}%"
        if values.get("money"):
            return f"the model has {name} {values['money'][0] / 1e6:,.0f}mn"
        if values.get("count"):
            return f"the model has {name} {values['count'][0]:,.4g}"
        return ""

    def add_multiple(self, metric: str, value: float | None) -> None:
        if value is None or not math.isfinite(value) or value <= 0:
            return
        self.multiples.setdefault(metric, []).append(value)
        self.add("x", value)

    def matches_metric(self, f: Figure, metric: str, year: str, downside: bool,
                       bound: str = "") -> bool:
        """The model's figure for this metric and year (or the bad case's, in its paragraph).
        `bound` "min" or "max" for ">$300bn" or "under 20%": the model's figure need only
        be on that side of it."""
        lo, hi = self.bounds(f)
        if bound == "min":
            hi = math.inf
        elif bound == "max":
            lo = -math.inf
        if f.kind in ("%", "pp"):
            # As consistency.py always allowed: "15% FY2030" for a model at 15.9% rounds.
            lo, hi = min(lo, f.value - MARGIN_PP), max(hi, f.value + MARGIN_PP)
        elif f.approx:
            lo, hi = min(lo, f.value * (1 - APPROX_SHARE)), max(hi, f.value * (1 + APPROX_SHARE))
        sources = [self.metric_years] + ([self.downside_years] if downside else [])
        # "USD 14.0bn revenue" in a sentence about ARM's chip business is the segment's.
        keys = [metric] + ([k for k, y in self.metric_years if y == year
                            and k.startswith(("segment:", "driver:"))]
                           if metric == "revenue" and f.kind in ("money", "count") else [])
        for source, key in ((src, k) for src in sources for k in keys):
            values = source.get((key, year), {})
            for kind, factor in self._candidates(f, notes=False):
                if any(lo / factor <= x <= hi / factor for x in values.get(kind, ())):
                    return True
        return False

    def add(self, kind: str, value: float) -> None:
        if value is None or not math.isfinite(value):
            return
        self.values.setdefault(kind, []).append(abs(value))
        self._sorted = False

    def add_year(self, year: str, kind: str, value: float | None) -> None:
        """A figure the model gives for a forecast year, trusted anywhere in the prose; the
        check by year and metric uses `metric_years`."""
        if value is not None and math.isfinite(value):
            self.add(kind, value)

    def add_text(self, text: str) -> None:
        """Trusted text whose figures carry their units (placeholder values, the computed
        summary): every figure, no context needed."""
        for f in figures(text):
            self.add(f.kind, f.value)

    def add_model(self, obj) -> None:
        """The JSON header's numbers: amounts are in millions, rates are fractions."""
        if isinstance(obj, dict):
            for x in obj.values():
                self.add_model(x)
        elif isinstance(obj, list):
            for x in obj:
                self.add_model(x)
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            self.add("millions", obj)
            self.add("bare", obj)
            self.add("x", obj)
            if abs(obj) <= 1.5:
                self.add("%", obj * 100)

    def add_notes(self, text: str, window: int = 90) -> None:
        """Research notes and the market data block: a figure here sources the prose only
        near a word the prose uses around it, since 80,000 characters of notes hold
        almost any number somewhere."""
        def context(start, end):
            around = text[max(0, start - window):end + window]
            return frozenset(_words(around) | _years(around))
        for f in figures(text):
            self.notes.setdefault(f.kind, []).append((abs(f.value), context(f.start, f.end)))
        for m in re.finditer(r"(?<![\w.$])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])", text):
            self.notes.setdefault("plain", []).append(
                (float(m.group(0).replace(",", "")), context(m.start(), m.end())))

    def _sort(self) -> None:
        if not self._sorted:
            for vals in self.values.values():
                vals.sort()
            for vals in self.notes.values():
                vals.sort(key=lambda t: t[0])
            self._sorted = True

    @staticmethod
    def _near(sorted_vals: list[float], lo: float, hi: float) -> bool:
        i = bisect.bisect_left(sorted_vals, lo)
        return i < len(sorted_vals) and sorted_vals[i] <= hi

    @staticmethod
    def _candidates(f: Figure, notes: bool) -> list[tuple[str, float]]:
        """(pool kind, factor) pairs a figure may match: pool value x factor ~ figure.

        Numbers without a unit match only where their unit is known: the JSON header's
        amounts are millions, and a comma'd figure in the notes is a table entry in
        units, thousands or millions. Anything looser, and the hundreds of per-share
        prices and ratios in the pool read as millions matched almost any amount.
        """
        if f.kind == "%":
            return [("%", 1.0), ("pp", 1.0)]
        if f.kind == "pp":
            return [("pp", 1.0), ("%", 1.0)]
        if f.kind == "x":
            # The market data block prints ratios bare ("| EV/EBITDA | 18.98 |"); the
            # context rule keeps a stray 19.0 elsewhere from sourcing "19x".
            return [("x", 1.0), ("bare", 1.0)] if notes else [("x", 1.0)]
        if f.kind in ("money", "count"):
            out = [("money", 1.0), ("count", 1.0)]
            if notes:
                out += [("plain", s) for s in ((1.0, 1e3, 1e6) if f.kind == "count"
                                               else (1e3, 1e6))]
            else:
                out.append(("millions", 1e6))
            return out
        if f.kind in ("W", "Wh"):
            return [(f.kind, 1.0), ("plain", 1.0)] if notes else [(f.kind, 1.0)]
        # A bare decimal or a comma'd count: the same value written with a unit
        # ("2,086.0mn shares" printed as "2,086.0"), or a bare figure.
        out = [(k, 1.0) for k in ("bare", "plain", "%", "x", "pp")]
        out += [(k, 1 / s) for k in ("money", "count") for s in (1.0, 1e3, 1e6, 1e9)]
        return out

    @staticmethod
    def bounds(f: Figure) -> tuple[float, float]:
        # "~" does not widen this: the digits written already carry the rounding, and a
        # 5% window let NEE's "~$9.2bn" FY2030 EBITDA match FY2022's $9,207mn.
        tol = max(f.slack, f.value * POOL_TOLERANCE)
        return f.value - tol, f.value + tol

    def trusted(self, f: Figure) -> bool:
        self._sort()
        lo, hi = self.bounds(f)
        for kind, factor in self._candidates(f, notes=False):
            vals = self.values.get(kind)
            if vals and self._near(vals, lo / factor, hi / factor):
                return True
        return False

    def in_notes(self, f: Figure, context: set[str], need: str | None = None) -> bool:
        """A note gives this figure near a word of `context` (and the token `need`)."""
        self._sort()
        lo, hi = self.bounds(f)
        words = context - {need} if need else context
        for kind, factor in self._candidates(f, notes=True):
            vals = self.notes.get(kind)
            if not vals:
                continue
            i = bisect.bisect_left(vals, (lo / factor,))
            while i < len(vals) and vals[i][0] <= hi / factor:
                near = vals[i][1]
                if near & words and (need is None or need in near):
                    return True
                i += 1
        return False


# ---------------------------------------------------------------------------- the check

# The writer's own numbers, declared as such: the prompt asks for exactly this wording.
# Only verbs that declare an input or an estimate: the pitch is in the first person
# throughout, and "I believe FPL added 90,000 customers" is still a claim about the world.
OWN_ESTIMATE = re.compile(
    r"\b(?:I|I'm|I've|I'd|my|we|our)\b[^.;]{0,60}?\b(?:assum|model|estimat|project|forecast|"
    r"haircut|appl|use|set|pencil|build|built|grow|hold|cut|lift|keep|put)\w*|"
    r"\bmy (?:estimate|assumption|forecast|projection|calculation|base case|downside case)\b|"
    r"\(estimate\)|"
    # "Forecast assumes 7 GW/year", "assumed $50-60/MWh", "vs my ~2.5 GW": NEE, 2026-09-27.
    r"\bassum\w*|\bmy\s*(?:~|c\.\s*)?\$?\d", re.I)
# "Each 1 GW delay", "per 100bp": the size of an illustration, not a claim.
PER_UNIT = re.compile(r"\b(?:each|every|per)\s+(?:additional\s+|extra\s+)?\W*$", re.I)
# Abbreviations whose full stop ends no sentence. Every sentence split in the code uses
# this: ESLT's "cost overruns directly hit margins (Land segment 9% margin in FY2025 vs.
# Aerospace 7.4%)" was split at "vs." on 2026-10-07, and "Aerospace 7.4%)." deleted alone.
ABBREVIATIONS = ("vs.", "v.", "e.g.", "i.e.", "cf.", "approx.", "incl.", "excl.", "est.",
                 "U.S.", "U.K.", "Mr.", "Ms.", "Dr.", "St.", "Jan.", "Feb.", "Mar.", "Apr.",
                 "Aug.", "Sep.", "Sept.", "Oct.", "Nov.", "Dec.")
NOT_AN_END = "".join(rf"(?<!\b{re.escape(a)})" for a in ABBREVIATIONS)
SENTENCE = re.compile(rf"(?<=[.;!?]){NOT_AN_END}\s+(?=[A-Z(*])|\n+")
ARITHMETIC_TOLERANCE = 0.01


def _scalar(f: Figure) -> float:
    return f.value / 100 if f.kind in ("%", "pp") else f.value


def _worked_out(f: Figure, others: list[Figure]) -> bool:
    """Whether `f` is a sum, difference, product or ratio of two other figures in the
    same sentence, so the reader can check it there: "$110bn at 5% = $5.5bn a year"."""
    target = _scalar(f)
    slack = max(f.slack / 100 if f.kind in ("%", "pp") else f.slack,
                abs(target) * ARITHMETIC_TOLERANCE)
    values = [_scalar(o) for o in others]
    for i, a in enumerate(values):
        for b in values[i + 1:]:
            candidates = [a * b]
            if b:
                candidates.append(a / b)
            if a:
                candidates.append(b / a)
            candidates += [a + b, abs(a - b)]
            if any(abs(c - target) <= slack for c in candidates):
                return True
    return False


def _table_free(body: str) -> str:
    # Keep offsets: blank the generated tables rather than removing them.
    return "\n".join(" " * len(line) if line.lstrip().startswith("|") else line
                     for line in body.split("\n"))


# ------------------------------------------------------------- metrics and forecast years

# The model's own metrics, as the prose names them. A figure is held to the model's value
# for a year only when it is a figure *of one of these*: positional pairing alone ("$5-6bn
# by 2030") flagged NFLX's advertising revenue, net additions and penetration, ESLT's gross
# margin and VST's gigawatts against the model, about 50 false alarms in 60 across the
# saved pitches, while the margin NFLX typed against its own model ("FY2030 FCF margin
# reaches my estimate of ~18%", model 27.8%) sat too far from its year to be paired.
MODEL_METRICS = [(re.compile(p, re.I), key) for p, key in (
    (r"\bROIC\b|\breturns? on (?:invested )?capital\b", "roic"),
    (r"(?:free[- ]cash[- ]flow|\bFCF)\s+margins?", "fcf_margin"),
    (r"(?:\bEBIT|\boperating)\s+margins?", "ebit_margin"),
    (r"\bEBITDA\s+margins?", "ebitda_margin"),
    (r"free[- ]cash[- ]flows?|\bFCF\b", "fcf"),
    (r"\bEBITDA\b", "ebitda"),
    (r"\bEBIT\b|\boperating (?:income|profit)s?\b", "ebit"),
    (r"\bNOPAT\b", "nopat"),
    (r"\bcapex\b|\bcapital (?:expenditures?|spending)\b", "capex"),
    (r"\bD&A\b|\bdepreciation(?: and amorti[sz]ation)?\b", "da"),
    (r"\brevenues?\b|\bsales\b|\btop[- ]line\b", "revenue"),
)]
# Metrics the model does not have. Named nearer a figure than any model metric, they make
# it a claim about something else.
OTHER_METRIC = re.compile(
    r"\bgross (?:margins?|profits?)\b|\bnet (?:income|margins?|profits?)\b|\bEPS\b|"
    r"\bearnings\b|\bARR\b|\bbookings?\b|\bbacklog\b|\bpenetration\b|\bshare\b|\bTAM\b|"
    r"\baddressable\b|\bmarket\b|\bcapacity\b|\busers?\b|\bsubscri\w+|\bmembers\w*|"
    r"\bnet add\w*|\bMAUs?\b|\bsegment margins?\b|\badjusted\b|\bnon-GAAP\b|\bopex\b|"
    r"\bR&D\b|\bSG&A\b|\btax\w*|\binterest\b|\bdividends?\b|\bbuybacks?\b|\bdebt\b|"
    r"\bprices?\b|\bpricing\b|\bASPs?\b|\bARPU\b|\bvolumes?\b|\bdemand\b|\bload\b|"
    r"\bshipments?\b|\bunits?\b|\bheadcount\b|\bcustomers?\b|\bbills?\b|\bmix\b|"
    r"\bGW\b|\bMW\b|\bbps\b|\bpp\b|\bspend\w*|\bcosts?\b|\borders?\b|\bwafers?\b|\bGM\b|"
    # What the valuation is, rather than a forecast year's figure: "discounted at 11.0%",
    # "equity value = USD 499.39", "88% is terminal value", "3% growth" on the terminal.
    r"\bdiscount\w*|\bWACC\b|\brisk-free\b|\bcost of (?:equity|debt|capital)\b|\bERP\b|"
    r"\bbeta\b|\bterminal(?: value| growth)?\b|\bperpetu\w*|\benterprise value\b|"
    r"\bequity value\b|\bEV\b|\bvalues?\b|\bworth\b|\bgives\b|\byields\b|\breturns\b|"
    r"\bupside\b|\bmultiples?\b|\bcontribution\b|\bportion\b|\bproportion\b",
    re.I)
# "10% of revenue": revenue is what the figure is a share of, not what it is.
DENOMINATOR = re.compile(r"(?:(?:%|percent|share|proportion|fraction|portion)\s+of|contribution\s+to)"
                         r"\s+(?:total\s+|projected\s+|its\s+|group\s+|consolidated\s+)?$|"
                         r"\bas\s+(?:a\s+)?%\s+of\s+$", re.I)
# Figures that are someone else's: the Street's, management's, the peers'.
OTHERS_CLAIM = re.compile(r"\b(?:consensus|street|sell-side|analysts?|guid\w*|management|"
                          r"targets?|targeting|peers?|competitors?|industry|market expects|"
                          r"trailing|TTM|LTM|four quarters|twelve months)\b",
                          re.I)
# A hypothetical: "Even if ads reach $5-6bn by 2030", "Scores revenue in FY30 would be".
HYPOTHETICAL = re.compile(r"^\W*(?:even\s+)?(?:if|were|should|unless|suppose)\b|\bwould\b|\bimpact\b|"
                          r"\bcould\b|\bif\b", re.I)
# Words that may stand before "revenue" and leave it the company's total: anything else
# ("advertising revenue", "AI revenue") is a part of it.
TOTAL_REVENUE_WORDS = {"total", "consolidated", "group", "net", "annual", "projected",
                       "forecast", "reported", "overall", "company", "its", "their", "the",
                       "a", "of", "in", "to", "from", "and", "with", "fy", "my", "our",
                       "base", "case", "year", "full", "full-year", "implied", "modelled",
                       "modeled", "trailing", "ttm", "ltm", "that", "this", "on", "at",
                       "by", "for", "e", "f", "s", "rising", "falling", "grows", "growth"}
# The unit of the figure before "revenue" ("$1.2bn revenue"), not a word about it.
UNIT_WORD = re.compile(r"(?:bn|mn|tn|mm|m|b|k|billion|million|trillion|thousand|x|FY\d*)", re.I)
QUARTER_WORD = re.compile(r"(?:[QH][1-4]|[1-4][QH]|YTD|quarterly|annual)", re.I)
# Between a figure and the year it is for: "$91bn in FY2030", "18% (FY2030)", "FY2030:
# $29.2bn", "FY2030 EBITDA of $12.9bn". Never "to", "from" or an arrow: in "from 11.5% in
# FY2026 to 13.5% in FY2030" and "FY2025 $17.7bn -> FY2030 $24.8bn" those join a range.
LINK_AFTER = {"in", "by", "for", "at", "during", "of", "fiscal", "year", "end", "the",
              "e", "f"}
LINK_BEFORE = {"fy", "e", "f", "a", "at", "of", "is", "was", "reaches", "reaching", "in",
               "be", "will", "would", "and", "estimated", "est"}
RANGE_MARK = re.compile(r"→|->|–|—|\bto\b|\bfrom\b|\bvs\.?\b|\bversus\b|(?<=\d)-|-(?=\s*(?:FY|20)\d)",
                        re.I)
YEAR_RANGE = re.compile(r"(?:FY\s?'?)?(?:19|20)?\d{2}\s?(?:-|–|to)\s?(?:FY\s?'?)?(?:19|20)?\d{2}\b",
                        re.I)
# "Q2 2026", "1H 2027", "quarter of 2026": a part of the year, not the year.
QUARTER_BEFORE = re.compile(r"(?:\b[QH][1-4]|\b[1-4][QH]|quarter(?: of)?|\b(?:nine|six|three)"
                            r"[- ]months?(?: of| ended)?|\b[369]M|\bYTD|\bfirst[- ]half(?: of)?|"
                            r"\bsecond[- ]half(?: of)?|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|"
                            r"Oct|Nov|Dec)[a-z]*\.?(?:\s+\d{1,2},?)?|\bquarters (?:to|ended)|"
                            r"\bmonths (?:to|ended))\s*'?$", re.I)
# A paragraph about the bad case quotes the bad case's figures.
DOWNSIDE_WORDS = re.compile(r"downside|bear case|bad case|stress case", re.I)


@dataclass
class Mention:
    start: int
    end: int
    metric: str | None      # a model metric, or None for something else


def _mentions(sentence: str, pool: "Pool") -> list[Mention]:
    """The metrics a sentence names, most specific first where they overlap."""
    found: list[Mention] = []

    def free(s: int, e: int) -> bool:
        return not any(m.start < e and s < m.end for m in found)

    for pattern, key in pool.names:
        for m in pattern.finditer(sentence):
            if free(m.start(), m.end()):
                found.append(Mention(m.start(), m.end(), key))
    for pattern, key in MODEL_METRICS:
        for m in pattern.finditer(sentence):
            if not free(m.start(), m.end()):
                continue
            metric = key
            if DENOMINATOR.search(sentence[max(0, m.start() - 28):m.start()]):
                continue
            if key == "revenue":
                # Only the word right before it: "I project FY2031 revenue" and "$1.2bn
                # revenue" are the total, "advertising revenue" is a part of it.
                before = re.findall(r"[\w$.,&'~-]+", sentence[max(0, m.start() - 30):m.start()])
                word = before[-1].lower().rstrip("'s") if before else ""
                if re.search(r"\d", word) or UNIT_WORD.fullmatch(word) or QUARTER_WORD.fullmatch(word):
                    word = ""
                if word and word not in TOTAL_REVENUE_WORDS and word not in pool.company_words:
                    # "ad revenue" for a segment named Advertising; any other part of the
                    # total ("services revenue" with no such segment) is not the model's.
                    metric = next((key for _, key in pool.names
                                   if key.startswith("segment:") and len(word) >= 2
                                   and key[8:].lower().startswith(word)), None)
            found.append(Mention(m.start(), m.end(), metric))
    for m in OTHER_METRIC.finditer(sentence):
        if free(m.start(), m.end()):
            found.append(Mention(m.start(), m.end(), None))
    return sorted(found, key=lambda m: m.start)


# A capitalised name that is neither the company nor a period: "Solar", "Other Bets".
SUBJECT = re.compile(r"(?<![.:]\s)\b(?!FY|Q[1-4]|H[12]|USD|EUR|I\b)[A-Z][a-z]{2,}")


def _closed_paren(sentence: str, start: int, end: int) -> bool:
    """Whether a parenthesis opens and closes between start and end, or whether start sits
    inside one that closes before end: "ARPU $13.9 (derived from $45.2B revenue / ...) ->
    USD 16.6" is ARPU's figure, not revenue's."""
    depth = 0
    for ch in sentence[start:end]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return True
    return False


def _metric_of(f: Figure, sentence: str, mentions: list[Mention],
               company: set[str] = frozenset()) -> Mention | None:
    """The metric a figure is a figure of: one named right after it ("41% EBIT margin",
    "$29.2bn revenue", "85% of enterprise value"), else the nearest one named before it in
    the clause, else one named shortly after it ("$91bn of FY2030 revenue")."""
    def subject_in(gap: str) -> bool:
        return any(m.group(0).lower() not in company for m in SUBJECT.finditer(gap))

    right = next((m for m in mentions if m.start >= f.end
                  and re.match(r"^\s*(?:(?:of|is|in)\s+)?$", sentence[f.end:m.start])), None)
    if right is not None:
        # "I model Other Bets at USD 2.8bn revenue": the revenue of Other Bets.
        lead = re.split(r"[;,(]|\.\s", sentence[max(0, f.start - 40):f.start])[-1]
        if right.metric == "revenue" and subject_in(lead):
            return Mention(right.start, right.end, None)
        return right
    before = [m for m in mentions if m.end <= f.start and f.start - m.end <= 70
              and not re.search(r";|\.\s", sentence[m.end:f.start])
              and not _closed_paren(sentence, m.start, f.start)]
    if before:
        m = before[-1]
        # "My revenue forecast assumes Solar reaching ~$3.5B": the figure is Solar's.
        if subject_in(sentence[m.end:f.start]):
            return Mention(m.start, m.end, None)
        return m
    after = [m for m in mentions if m.start >= f.end and m.start - f.end <= 22
             and not re.search(r"[,;:(]|\d", sentence[f.end:m.start])]
    return after[0] if after else None


def _linked(gap: str, allowed: set[str]) -> bool:
    if RANGE_MARK.search(gap) or re.search(r"\d", gap):
        return False
    words = [w.lower() for w in re.findall(r"[A-Za-z]+", gap)]
    return len(words) <= 3 and all(w in allowed for w in words) and len(gap) <= 24


def _year_near(start: int, end: int, sentence: str, years: list[tuple[int, int, str]],
               extra: set[str] = frozenset()) -> str | None:
    """A forecast year written right after the span start:end ("USD 18.4bn in FY2026"), or
    else right before it ("FY2030: $29.2bn")."""
    after = [(ys - end, year) for ys, ye, year in years
             if ys >= end and _linked(sentence[end:ys], LINK_AFTER | extra)]
    if after:
        return min(after)[1]
    before = [(start - ye, year) for ys, ye, year in years
              if ye <= start and _linked(sentence[ye:start], LINK_BEFORE | extra)]
    return min(before)[1] if before else None


def _forecast_years(sentence: str, forecast: set[str]) -> list[tuple[int, int, str]]:
    """The model's forecast years named in a sentence, leaving out parts of a year
    ("Q2 2026") and the ends of a span ("FY2026-FY2030", "2026-27")."""
    spans = [m.span() for m in YEAR_RANGE.finditer(sentence)]
    out = []
    for m in YEAR_TOKEN.finditer(sentence):
        year = _year_of(m)
        if year not in forecast:
            continue
        if QUARTER_BEFORE.search(sentence[max(0, m.start() - 12):m.start()]):
            continue
        if any(s <= m.start() < e for s, e in spans):
            continue
        out.append((m.start(), m.end(), year))
    return out


# ------------------------------------------------------------------- citations and sums

# A figure credited to someone else is a claim about the world, whoever's estimate the
# sentence calls it: NFLX's second write turned "YouTube captures 25% of streaming watch
# time (Nielsen, 2025)" into "my estimate of 25%" to clear this check.
CITATION = re.compile(
    r"\((?![^()]*\b(?:my|I|our)\b)[^()]*?(?:\b[A-Z][A-Za-z&.]+[^()]*\b(?:19|20)\d{2}\b|"
    r"\b(?:10-K|10-Q|20-F|8-K|letter|release|transcript|filing|report|research|"
    r"estimates|data|disclosures?|guidance|survey|press)\b)[^()]*\)|"
    r"\b(?:per|according to|citing|cited by|source:)\s+(?!my\b|our\b|I\b)(?:the\s+)?[A-Z0-9]", re.I)
OWN_IN_CITED = re.compile(r"\bmy\s+(?:estimate|assumption)s?\s+(?:of\s+)?$", re.I)

# A chain of figures joined by operators and the figure it comes to: "325M / 650M = 50%",
# "USD 59.4bn - USD 24.0bn = USD 59.4bn", "$13.9 (derived from $45.2B revenue / 325M subs /
# 12 months)". Numbers here include bare whole numbers, which `figures` leaves out.
TERM = re.compile(r"(?P<cur>" + CURRENCY + r")?(?<![\w.,/])(?P<num>" + NUMBER + r")"
                  r"(?P<unit>%|x(?![A-Za-z])|\s?(?:trillion|billion|million|thousand|tn|bn|mn|"
                  r"mm|k|m|b|M|B|K)\b|(?-i:T)\b)?(?![A-Za-z])", re.I)
OPERATOR = re.compile(r"^\s*(?:[A-Za-z][\w&'-]*\s+){0,3}?(?P<op>[/×*÷+]|\s[-−]\s)\s*$")
EQUALS = re.compile(r"^\s*(?:[A-Za-z][\w&'-]*\s+){0,3}?(?:=|≈)\s*~?\s*$")
DERIVED = re.compile(r"\(\s*(?:[^()]*?,\s*)?(?:derived from|calculated as|computed as|"
                     r"i\.e\.|=)\s*$", re.I)
ARITHMETIC_SLACK = 0.03

# "a 17% discount to the peer median of 15.9x": CAT's 12.0x against 15.9x is 25%.
RELATION = re.compile(r"(?P<n>\d+(?:\.\d+)?)%\s+(?P<word>discount|premium)\s+(?:to|vs\.?|"
                      r"versus|over|against|relative to)\b", re.I)
RELATION_SLACK_PP = 1.5

# Multiples by what they divide, and where the prose names that.
MULTIPLE_METRIC = [(re.compile(p, re.I), key) for p, key in (
    (r"EV\s*/\s*EBITDA|\bEBITDA\b", "ev_ebitda"),
    (r"EV\s*/\s*EBIT\b|\bEBIT\b", "ev_ebit"),
    (r"\bP\s*/\s*E\b|\bPE\b|\bearnings\b", "pe"),
    (r"\bP\s*/\s*FCF\b|\bFCF\b|free[- ]cash[- ]flow", "fcf"),
    (r"EV\s*/\s*(?:sales|revenue)|\bsales\b|\brevenue\b", "ev_sales"),
)]
# A multiple in a sentence about this valuation is the code's, and wrong if it is not.
MODEL_CONTEXT = re.compile(r"\b(?:terminal|exit|DCF|my model|the model|implie[sd]|imply|"
                           r"perpetuity)\b", re.I)


def _term_value(m: re.Match) -> tuple[float, str]:
    num = float(m.group("num").replace(",", ""))
    unit = (m.group("unit") or "").strip().lower()
    if unit == "%":
        return num / 100, "%"
    if unit in SCALES:
        return num * SCALES[unit], "n"
    return num, "n"


def _evaluate(values: list[float], ops: list[str]) -> float | None:
    """Left to right, * and / before + and -."""
    try:
        stack, pending = [values[0]], []
        for op, x in zip(ops, values[1:]):
            op = op.strip()
            if op in ("×", "*"):
                stack[-1] *= x
            elif op in ("/", "÷"):
                stack[-1] /= x
            else:
                pending.append(op)
                stack.append(x)
        total = stack[0]
        for op, x in zip(pending, stack[1:]):
            total = total + x if op == "+" else total - x
        return total
    except ZeroDivisionError:
        return None


def _arithmetic(sentence: str) -> list[tuple[Figure, str]]:
    """Worked sums in the sentence that do not come to the figure they state."""
    terms = list(TERM.finditer(sentence))
    out = []
    i = 0
    while i < len(terms):
        values, ops, j = [_term_value(terms[i])[0]], [], i
        while j + 1 < len(terms):
            op = OPERATOR.match(sentence[terms[j].end():terms[j + 1].start()])
            if not op:
                break
            ops.append(op.group("op"))
            values.append(_term_value(terms[j + 1])[0])
            j += 1
        if ops:
            result = None
            if j + 1 < len(terms) and EQUALS.match(sentence[terms[j].end():terms[j + 1].start()]):
                result = terms[j + 1]
            elif i > 0 and DERIVED.search(sentence[terms[i - 1].end():terms[i].start()]):
                result = terms[i - 1]
            got = _evaluate(values, ops)
            if result is not None and got is not None:
                stated, _ = _term_value(result)
                written = result.group("num")
                decimals = len(written.split(".")[1]) if "." in written else 0
                unit_value = stated / float(written.replace(",", "")) if float(written.replace(",", "")) else 1
                slack = max(abs(stated) * ARITHMETIC_SLACK, 0.5 * 10 ** -decimals * abs(unit_value) * 1.5)
                if abs(got - stated) > slack:
                    f = Figure("arith", stated, slack, result.group(0).strip(), result.start(),
                               result.end())
                    out.append((f, f"{_fmt(got, result)}"))
            i = j + 1
        else:
            i += 1
    return out


def _fmt(value: float, like: re.Match) -> str:
    unit = (like.group("unit") or "").strip()
    cur = like.group("cur") or ""
    if unit == "%":
        return f"{value * 100:.1f}%"
    if unit.lower() in SCALES:
        return f"{cur}{value / SCALES[unit.lower()]:,.2f}{unit}"
    return f"{cur}{value:,.2f}"


def _relations(sentence: str, figs: list[Figure]) -> list[tuple[Figure, str]]:
    """"A 17% discount to 15.9x" where the figure discounted is 12.0x. In "12.0x is a 17%
    discount to 15.9x and a 51% discount to 24.4x" both are discounts of 12.0x."""
    out = []
    refs: set[int] = set()
    for m in RELATION.finditer(sentence):
        ref = next((f for f in figs if f.start >= m.end() and f.kind in ("x", "money", "%")),
                   None)
        if ref is None:
            continue
        subject = next((f for f in reversed(figs) if f.end <= m.start() and f.kind == ref.kind
                        and f.value and f.start not in refs), None)
        refs.add(ref.start)
        if subject is None or not ref.value:
            continue
        stated = float(m.group("n"))
        discount = m.group("word").lower() == "discount"
        actual = ((1 - subject.value / ref.value) * 100 if discount
                  else (subject.value / ref.value - 1) * 100)
        if abs(stated - actual) > RELATION_SLACK_PP:
            f = Figure("%", stated, 0.5, f"{m.group('n')}%", m.start(),
                       m.start() + len(m.group("n")) + 1)
            word = (("discount" if actual >= 0 else "premium") if discount
                    else ("premium" if actual >= 0 else "discount"))
            out.append((f, f"{subject.text} is a {abs(actual):.0f}% {word} to {ref.text}"))
    return out


def _multiple_metric(f: Figure, sentence: str) -> str | None:
    """What a multiple divides, named right after it ("12.0x EV/EBITDA", "29x FCF") or
    right before ("EV/EBITDA of 24.4x")."""
    after = sentence[f.end:f.end + 40]
    after = re.sub(r"^\s*(?:(?:trailing|forward|TTM|LTM|current|terminal|exit|today's|"
                   r"FY\s?'?\d{2,4}|20\d{2}|the|an?|its)\s+)*", "", after, flags=re.I)
    for pattern, key in MULTIPLE_METRIC:
        m = pattern.match(after)
        if m:
            return key
    before = sentence[max(0, f.start - 30):f.start]
    for pattern, key in MULTIPLE_METRIC:
        m = list(pattern.finditer(before))
        if m and re.match(r"^\s*(?:multiple\s+)?(?:of|at|is|=|:)?\s*(?:~|c\.\s*)?$",
                          before[m[-1].end():], re.I):
            return key
    return None


# ------------------------------------------------------------------------------- the audit

@dataclass
class Flag:
    """One figure the checks could not account for.

    `kind` is "year" (written against a forecast year and not the model's figure for it),
    "growth" (a stated growth rate of a model metric that the model does not have),
    "arithmetic" (a worked sum that does not come out), "relation" (a discount or premium
    that is not the gap between the figures it names), "multiple" (a multiple of this
    valuation that the code computed differently), or "untraced" (from no source at all).
    The first four are wrong; an untraced figure may be right, but no reader can check it.
    """
    figure: Figure
    sentence: str
    kind: str
    year: str | None = None
    detail: str = ""

    @property
    def wrong(self) -> bool:
        return self.kind != "untraced"


def audit(text: str, pool: "Pool") -> list[Flag]:
    """Every figure in `text` that is wrong against the model or comes from no source."""
    text = _table_free(text)
    flags: list[Flag] = []
    pos = 0
    paragraphs = [(m.start(), m.end()) for m in re.finditer(r"(?:[^\n]|\n(?!\s*\n))+", text)]
    for sentence in SENTENCE.split(text):
        if not sentence.strip():
            continue
        start = text.find(sentence, pos)
        pos = start + len(sentence)
        paragraph = next((text[s:e] for s, e in paragraphs if s <= start < e), sentence)
        about_downside = bool(DOWNSIDE_WORDS.search(paragraph))
        cited = bool(CITATION.search(sentence))
        own = bool(OWN_ESTIMATE.search(sentence))
        figs = figures(sentence)
        years = _forecast_years(sentence, pool.forecast)
        mentions = _mentions(sentence, pool)
        wrong_spans: list[tuple[int, int]] = []
        for f, expected in _arithmetic(sentence):
            flags.append(Flag(f, sentence, "arithmetic", detail=expected))
            wrong_spans.append((f.start, f.end))
        for f, expected in _relations(sentence, figs):
            flags.append(Flag(f, sentence, "relation", detail=expected))
            wrong_spans.append((f.start, f.end))

        sourced, pending = [], []
        for f in figs:
            if any(s <= f.start < e for s, e in wrong_spans):
                continue
            local = _words(sentence[max(0, f.start - 80):f.end + 80])
            per_unit = PER_UNIT.search(sentence[max(0, f.start - 20):f.start])
            if f.kind == "x":
                metric = _multiple_metric(f, sentence)
                computed = pool.multiples.get(metric) if metric else None
                lo, hi = pool.bounds(f)
                if computed and any(lo * (1 - MULTIPLE_SLACK) <= x <= hi * (1 + MULTIPLE_SLACK)
                                    for x in computed):
                    sourced.append(f)
                elif pool.in_notes(f, local):
                    sourced.append(f)
                elif computed and _model_multiple(sentence, f):
                    terminal = pool.terminal_multiples.get(metric)
                    nearest = (terminal if terminal and re.search(r"terminal|exit", sentence, re.I)
                               else min(computed, key=lambda x: abs(x - f.value)))
                    flags.append(Flag(f, sentence, "multiple",
                                      detail=f"the code's figure is {nearest:.1f}x"))
                elif not computed and pool.trusted(f):
                    sourced.append(f)
                else:
                    pending.append(f)
                continue
            mention = _metric_of(f, sentence, mentions, pool.company_words)
            growth = _growth_claim(f, sentence, mentions)
            if growth is not None:
                clause = re.split(r";|\.\s", sentence[:f.start])[-1]
                if OTHERS_CLAIM.search(clause) or HYPOTHETICAL.search(clause):
                    pass
                elif (pool.matches_growth(f, growth.metric, about_downside)
                      or pool.in_notes(f, local) or _stated_cagr(f, figs, sentence, pool)):
                    sourced.append(f)
                    continue
                else:
                    flags.append(Flag(f, sentence, "growth",
                                      detail=pool.describe_growth(growth.metric)))
                    continue
            clause = re.split(r";|\.\s", sentence[:f.start])[-1]
            exact_price = (f.kind == "money" and _significant(f.text) >= 4 and pool.trusted(f))
            if (mention is not None and mention.metric and not per_unit and not exact_price
                    and not OTHERS_CLAIM.search(clause) and not HYPOTHETICAL.search(clause)):
                year = _year_near(f.start, f.end, sentence, years)
                if year is None and not any(mention.end <= g.start < f.start for g in figs):
                    year = _year_near(mention.start, mention.end, sentence, years)
                if year and f.kind in ("%", "pp") and mention.metric.startswith(
                        ("segment:", "driver:")) and not _growth_or_share(sentence, f,
                                                                          mention.start):
                    year = None     # RKLB's "Launch Services 35%" was its gross margin
                if year:
                    metric = _metric_for_kind(mention.metric, f.kind)
                    if metric is None:
                        pass        # a count or quantity of a money metric: not this check
                    elif (pool.matches_metric(f, metric, year, about_downside,
                                              _bound(sentence, f))
                          or pool.in_notes(f, local | {_year_token(year)}, need=_year_token(year))
                          or _segment_in_paragraph(f, metric, year, paragraph, pool,
                                                   about_downside)):
                        sourced.append(f)
                        continue
                    else:
                        flags.append(Flag(f, sentence, "year", year,
                                          detail=pool.describe_metric(metric, year)))
                        continue
            own_here = own and not (cited and not OWN_IN_CITED.search(
                sentence[max(0, f.start - 30):f.start]) and _cited_near(sentence, f))
            # A multiple is computed, never estimated; a ratio that is not a valuation
            # multiple ("a book-to-bill of 1.0x by FY2030") may be the writer's own.
            own_ok = own_here and (f.kind != "x" or _multiple_metric(f, sentence) is None)
            if (pool.trusted(f) or own_ok or per_unit
                    or pool.in_notes(f, local)):
                sourced.append(f)
            else:
                pending.append(f)
        # A figure worked out from sourced ones in the same sentence can be checked there,
        # and may source the next step. Only from sourced ones: NEE's "14x FY2030 EBITDA
        # ($12.9bn) = $181bn EV" agreed with itself and with neither the model's $22.2bn
        # EBITDA nor its EV.
        progress = True
        while pending and progress:
            progress = False
            for f in list(pending):
                if _worked_out(f, sourced):
                    pending.remove(f)
                    sourced.append(f)
                    progress = True
        flags += [Flag(f, sentence, "untraced") for f in pending]
    return flags


MULTIPLE_SLACK = 0.05


def _significant(text: str) -> int:
    """Significant digits as written: "USD 499.39" has five, "$51.5bn" three."""
    number = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    return len(re.sub(r"\D", "", number.group(0)).lstrip("0")) if number else 0


def _model_multiple(sentence: str, f: Figure) -> bool:
    """A multiple of this valuation: named in the words just before it, and not the peers',
    the Street's or a forward market multiple."""
    window = sentence[max(0, f.start - 70):f.start]
    return bool(MODEL_CONTEXT.search(window)) and not re.search(
        r"peer|consensus|street|forward|market|analyst|\bvs\.?\s|versus", window, re.I)


# "a 3.5% CAGR", "(19% CAGR)", "a CAGR of 9.4%", "growing 8% a year".
CAGR_AFTER = re.compile(r"^\s*(?:CAGR|compound(?:ed)? annual|a year\b|annual(?:ly)?\b|per year)",
                        re.I)
CAGR_BEFORE = re.compile(r"\bCAGR(?: of)?\s*(?:~|c\.\s*)?$", re.I)


def _growth_claim(f: Figure, sentence: str, mentions: list[Mention]) -> Mention | None:
    """The model metric a stated compound growth rate is for, when it names one.

    NFLX's pitch of 2026-10-02 gave memberships "325m -> 400m, a 3.5% CAGR" (4.2%), and
    ad revenue "USD 8.9bn by FY2030 (19% CAGR)" (31%): a figure sat between each rate and
    its metric, so the forecast-year check never paired them.
    """
    if f.kind != "%" or not (CAGR_AFTER.match(sentence[f.end:f.end + 20])
                             or CAGR_BEFORE.search(sentence[max(0, f.start - 14):f.start])):
        return None
    before = [m for m in mentions if m.end <= f.start
              and not re.search(r";|\.\s", sentence[m.end:f.start])]
    for m in reversed(before):
        if m.metric is None:
            continue
        # "Ratings fee increases of 1.5-2.0% annually" is the price part of Ratings' growth,
        # not its revenue growth: SPGI's of 2026-10-02 lost two sound sentences to it.
        after = re.sub(r"^[\s\-–%\d.]*(?:annual(?:ly)?\s+)?", "", sentence[f.end:f.end + 40])
        if (PRICE_PART.search(sentence[max(m.end, f.start - 30):f.start])
                or PRICE_PART.match(after)):
            return None
        if not m.metric.endswith("_margin"):
            return m
        break
    return None


PRICE_PART = re.compile(r"(?:[\w/-]+\s+){0,2}?\b(?:fee|price|pricing|escalator|ARPU|yield|"
                        r"rate card)s?\b", re.I)
FISCAL_YEAR = re.compile(r"\b(?:FY)?((?:19|20)\d{2})\b")


def _stated_cagr(f: Figure, figs: list[Figure], sentence: str, pool: "Pool") -> bool:
    """A rate that is the CAGR between two amounts written in the same sentence: "Indices
    from $1.85bn to USD 2.7bn in FY2030, an 8.2% CAGR" runs from the reported year, not
    the model's first forecast year, and the reader can check it there. With one year
    named, the other end is the last reported year; with none, the whole forecast. Each
    amount may be anywhere its rounding allows: "USD 2.7bn" is 2.65-2.75bn."""
    years = {int(y) for y in FISCAL_YEAR.findall(sentence)}
    if pool.forecast and len(years) < 2:
        first = min(int(y) for y in pool.forecast)
        years |= {first - 1} | ({max(int(y) for y in pool.forecast)} if not years else set())
    years = sorted(years)
    span = years[-1] - years[0] if len(years) >= 2 else 0
    if span <= 0:
        return False
    money = [g for g in figs if g.kind == "money" and g.value > g.slack]
    lo, hi = f.value - max(f.slack, 0.1), f.value + max(f.slack, 0.1)
    for a in money:
        for b in money:
            if b.value <= a.value:
                continue
            low = ((b.value - b.slack) / (a.value + a.slack)) ** (1 / span) - 1
            high = ((b.value + b.slack) / (a.value - a.slack)) ** (1 / span) - 1
            if low * 100 <= hi and high * 100 >= lo:
                return True
    return False


LOWER_BOUND = re.compile(r"(?:>|≥|\bover|\bmore than|\bat least|\bexceed\w*|\babove)\s*\W*$", re.I)
UPPER_BOUND = re.compile(r"(?:<|≤|\bunder|\bless than|\bbelow|\bat most|\bup to)\s*\W*$", re.I)
GROWTH_OR_SHARE = re.compile(r"\b(?:grow\w*|growth|CAGR|declin\w*|ris\w*|fall\w*|YoY|"
                             r"annual\w*|a year|per year|of (?:total )?(?:revenue|sales)|share|"
                             r"mix|up|down)\b", re.I)


def _bound(sentence: str, f: Figure) -> str:
    before = sentence[max(0, f.start - 14):f.start]
    return "min" if LOWER_BOUND.search(before) else "max" if UPPER_BOUND.search(before) else ""


def _growth_or_share(sentence: str, f: Figure, since: int | None = None) -> bool:
    """A sign or a growth or share word beside a percentage: "+2% FY2027", "grows 8%",
    "35% of revenue". Without one, a segment's percentage may be its margin."""
    start = max(0, f.start - 30) if since is None else min(since, max(0, f.start - 30))
    around = sentence[start:f.end + 25]
    return bool(re.search(r"[+\-−]\s*$", sentence[max(0, f.start - 2):f.start])
                or GROWTH_OR_SHARE.search(around))


def _cited_near(sentence: str, f: Figure) -> bool:
    """A citation in the same clause as the figure."""
    for m in CITATION.finditer(sentence):
        between = sentence[min(f.end, m.start()):max(f.start, m.end())]
        if not re.search(r"[;]|\.\s", between) and abs(m.start() - f.end) <= 120:
            return True
    return False


def _segment_in_paragraph(f: Figure, metric: str, year: str, paragraph: str, pool: "Pool",
                          downside: bool) -> bool:
    """A revenue percentage that is the figure of a segment its paragraph names: MSFT's
    "Revenue path: $172bn (FY2027, +25%)" under its Intelligent Cloud heading was held to
    total revenue growth."""
    if metric != "revenue" or f.kind not in ("%", "pp"):
        return False
    return any(key.startswith("segment:") and pattern.search(paragraph)
               and pool.matches_metric(f, key, year, downside)
               for pattern, key in pool.names)


def _metric_for_kind(metric: str, kind: str) -> str | None:
    """The metric a figure of this kind is held to: a money figure beside "FCF margin" is
    the FCF, a percentage beside "revenue" its growth or share."""
    margin = metric.endswith("_margin")
    if kind in ("money", "count"):
        return metric[:-len("_margin")] if margin else metric
    if kind in ("%", "pp"):
        return metric
    if kind in ("W", "Wh", "bare"):
        return metric if metric.startswith(("segment:", "driver:")) else None
    return None


def unsourced(text: str, pool: "Pool") -> list[tuple[Figure, str, str | None]]:
    """The audit as (figure, its sentence, the forecast year it contradicts or None)."""
    return [(fl.figure, fl.sentence, fl.year) for fl in audit(text, pool)]


def outcomes(text: str, pool: "Pool") -> list[tuple[Figure, str, str]]:
    """Every figure in `text` with what the audit made of it: "sourced", "untraced", or the
    kind of a wrong one. The audit reports only the figures it flags, so a figure it passed
    in error never showed up anywhere: SPGI's "~90% recurring" and "retention above 110%"
    of 2026-10-02 matched unrelated model percentages and went out unmarked."""
    flagged = {(fl.sentence, fl.figure.start): fl.kind for fl in audit(text, pool)}
    out = []
    for sentence in SENTENCE.split(_table_free(text)):
        if sentence.strip():
            for f in figures(sentence):
                out.append((f, sentence, flagged.get((sentence, f.start), "sourced")))
    return out


def _quote(f: Figure, sentence: str, width: int = 70) -> str:
    """The figure in its sentence, cut to the words around it."""
    lo, hi = max(0, f.start - width), min(len(sentence), f.end + width)
    where = (("..." if lo else "") + sentence[lo:hi].strip()
             + ("..." if hi < len(sentence.rstrip()) else ""))
    where = where.replace("**", "").replace("__", "")     # quoted on the PDF cover
    return f'"{f.text}" in "{where}"'


def describe(found, limit: int = 12) -> list[str]:
    """The problem lines for a rewrite: the wrong figures, by kind. Untraced figures are
    not sent back: asked to source them, the writer relabelled them as its own estimates.
    They are marked in the PDF instead."""
    flags = [fl if isinstance(fl, Flag) else Flag(fl[0], fl[1], "year" if fl[2] else "untraced",
                                                  fl[2]) for fl in found]
    problems = []
    year = [fl for fl in flags if fl.kind in ("year", "growth")]
    if year:
        problems.append(
            "these figures are given for a forecast year but are not your model's figure "
            "for that year: " + "; ".join(
                f"{_quote(fl.figure, fl.sentence)} ("
                + (f"FY{fl.year}" if fl.year else "growth")
                + (f"; {fl.detail}" if fl.detail else "") + ")" for fl in year[:limit])
            + ". Cite the year's value through its placeholder ({{revenue:FY2030}}, "
            "{{fcf_margin:FY2030}}, a segment or driver), correct the figure, or change the "
            "model. Calling it your estimate does not reconcile it with your model")
    sums = [fl for fl in flags if fl.kind in ("arithmetic", "relation")]
    if sums:
        problems.append(
            "these worked figures do not come out: " + "; ".join(
                f"{_quote(fl.figure, fl.sentence)} (it comes to {fl.detail})" for fl in sums[:limit])
            + ". Correct the figure or the sentence")
    multiples = [fl for fl in flags if fl.kind == "multiple"]
    if multiples:
        problems.append(
            "these multiples of this valuation are not what the code computed: " + "; ".join(
                f"{_quote(fl.figure, fl.sentence)} ({fl.detail})" for fl in multiples[:limit])
            + ". Cite {{ev_ebitda}}, {{tv_ev_ebitda}} or the tables instead")
    return problems


# ---------------------------------------------------------------------------- building it

def _growth(pool: Pool, points: list[float]) -> None:
    """Year-on-year and from-the-start growth, simple and compound, of one series."""
    pts = [x for x in points if isinstance(x, (int, float)) and x > 0]
    for j in range(1, len(pts)):
        for i in {0, j - 1, 1 if j > 1 else 0}:
            if i < j:
                pool.add("%", (pts[j] / pts[i] - 1) * 100)
                pool.add("%", ((pts[j] / pts[i]) ** (1 / (j - i)) - 1) * 100)


STATEMENT_HEADER = re.compile(r"^\|\s*Line item\s*\|(.+)\|\s*$", re.M)


def _statement_pool(pool: Pool, text: str) -> None:
    """The market data block's statements, row by row: each value with its line item and
    year, and each line as a share of revenue and its growth. ESLT's "FY2025 FCF was $553mn
    (7.0% margin)" and "GAAP operating margin of 8.7%" are both there, as 553 and 690 over
    7,939, but a three-digit table entry and a ratio were never matched."""
    for header in STATEMENT_HEADER.finditer(text):
        columns = [c.strip() for c in header.group(1).split("|")]
        years = [_years(c) for c in columns]
        rows: dict[str, list[float | None]] = {}
        for line in text[header.end():].split("\n")[2:]:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            values = []
            for c in cells[1:]:
                try:
                    values.append(float(c.replace(",", "")))
                except ValueError:
                    values.append(None)
            rows[cells[0]] = values
        revenue = next((v for k, v in rows.items() if k.lower() == "total revenue"), None)
        for item, values in rows.items():
            words = frozenset(_words(item))
            for i, x in enumerate(values):
                if x is None or i >= len(years):
                    continue
                context = words | years[i]
                pool.notes.setdefault("money", []).append((abs(x) * 1e6, context))
                pool.notes.setdefault("count", []).append((abs(x) * 1e6, context))
                if revenue and i < len(revenue) and revenue[i] and item.lower() != "total revenue":
                    share = abs(x) / revenue[i] * 100
                    pool.notes.setdefault("%", []).append((share, context | {"margin"}))
                prior = values[i + 1] if i + 1 < len(values) else None
                if prior and x and prior > 0 and x > 0:
                    pool.notes.setdefault("%", []).append(((x / prior - 1) * 100, context))
            pool._sorted = False


def _name_pattern(name: str) -> re.Pattern | None:
    """A segment or driver name as the prose writes it: "Paid subscribers (mn)" is
    "paid subscribers", "AI Semiconductor Revenue" is "AI semiconductor"."""
    core = re.sub(r"\([^)]*\)", " ", name)
    core = re.sub(r"\b(?:revenues?|sales|segment|USD|EUR|mn|bn|tn)\b", " ", core, flags=re.I)
    words = [w for w in re.findall(r"[A-Za-z0-9&][\w&'-]*", core)]
    if not words or all(len(w) < 3 and not w.isupper() for w in words):
        return None
    # MSFT's "Productivity & Business Processes" is the model's "... and Business Processes".
    body = r"\s+".join(r"(?:and|&)" if w.lower() in ("and", "&") else re.escape(w)
                       for w in words)
    return re.compile(rf"(?<![/-])\b{body}(?:'s)?(?:\s+(?:revenues?|sales|segment))?\b", re.I)


def _rates(points: list) -> list[float]:
    """Simple and compound growth between every pair of positive points."""
    pts = [x if isinstance(x, (int, float)) and x > 0 else None for x in points]
    out = []
    for i, a in enumerate(pts):
        for j in range(i + 1, len(pts)):
            b = pts[j]
            if a and b:
                out += [(b / a - 1) * 100, ((b / a) ** (1 / (j - i)) - 1) * 100]
    return out


def _growth_pool(pool: Pool, v) -> None:
    rows = v.rows
    for downside, case in ((False, rows), (True, getattr(v, "downside_rows", None) or [])):
        if not case:
            continue
        target = pool.downside_growth if downside else pool.growth
        series = {"revenue": [v.base_revenue] + [r.revenue for r in case],
                  "ebit": [r.ebit for r in case], "ebitda": [r.ebitda for r in case],
                  "fcf": [r.fcf for r in case], "nopat": [r.nopat for r in case],
                  "capex": [r.capex for r in case], "da": [r.da for r in case]}
        for metric, points in series.items():
            target.setdefault(metric, []).extend(_rates(points))
            if not downside and points[0] and points[0] > 0 and points[-1] > 0:
                pool.cagr[metric] = ((points[-1] / points[0]) ** (1 / (len(points) - 1)) - 1) * 100
    for name, values in v.segments.items():
        pool.growth.setdefault(f"segment:{name}", []).extend(_rates(values))
        # No base year in the model: "grows 7.3% CAGR to $1.32bn by FY30" from FICO's first
        # forecast year counts that year as the start, over one more year.
        if values[0] > 0 and values[-1] > 0:
            pool.growth[f"segment:{name}"].append(
                ((values[-1] / values[0]) ** (1 / len(values)) - 1) * 100)
        if values[0] > 0 and values[-1] > 0:
            pool.cagr[f"segment:{name}"] = ((values[-1] / values[0]) ** (1 / (len(values) - 1)) - 1) * 100
    for name, spec in v.drivers.items():
        if isinstance(spec, dict) and isinstance(spec.get("values"), list):
            base = spec.get("base")
            points = ([base] if isinstance(base, (int, float)) else []) + list(spec["values"])
            pool.growth.setdefault(f"driver:{name}", []).extend(_rates(points))
            if (isinstance(points[0], (int, float)) and isinstance(points[-1], (int, float))
                    and points[0] > 0 and points[-1] > 0 and len(points) > 1):
                pool.cagr[f"driver:{name}"] = ((points[-1] / points[0]) ** (1 / (len(points) - 1)) - 1) * 100


def _metric_pool(pool: Pool, v, data) -> None:
    """The model's figures for each metric and forecast year, base case and bad case."""
    mn = 1e6
    rows = v.rows
    for downside, case in ((False, rows), (True, getattr(v, "downside_rows", None) or [])):
        revenue = [v.base_revenue] + [r.revenue for r in case]
        series = {"revenue": revenue[1:], "ebit": [r.ebit for r in case],
                  "ebitda": [r.ebitda for r in case], "fcf": [r.fcf for r in case],
                  "nopat": [r.nopat for r in case], "capex": [r.capex for r in case],
                  "da": [r.da for r in case]}
        for i, r in enumerate(case):
            year = _norm_year(r.label)
            add = lambda metric, kind, value: pool.add_metric(metric, year, kind, value, downside)
            for metric, values in series.items():
                add(metric, "money", values[i] * mn)
                if r.revenue and metric != "revenue":
                    add(metric, "%", values[i] / r.revenue * 100)       # "capex at 4.5%"
                points = ([revenue[0]] if metric == "revenue" else []) + values
                now = i + 1 if metric == "revenue" else i
                for j in range(now):
                    start = points[j]
                    if start and start > 0 and values[i] > 0:
                        add(metric, "%", (values[i] / start - 1) * 100)
                        add(metric, "%", ((values[i] / start) ** (1 / (now - j)) - 1) * 100)
            for metric, value in (("ebit_margin", r.ebit_margin), ("fcf_margin", r.fcf_margin),
                                  ("ebitda_margin", r.ebitda / r.revenue if r.revenue else None)):
                if value is not None:
                    add(metric, "%", value * 100)
    returns = getattr(v, "returns", None)
    for y in (returns.years if returns else []):
        if y.roic is not None:
            pool.add_metric("roic", _norm_year(y.label), "%", y.roic * 100)
        if y.roic_ex_goodwill is not None:
            pool.add_metric("roic", _norm_year(y.label), "%", y.roic_ex_goodwill * 100)
    for name, values in v.segments.items():
        key = f"segment:{name}"
        pattern = _name_pattern(name)
        if pattern:
            pool.names.append((pattern, key))
        # From the base year when the model gives the segment's: MSFT's "$50bn (FY2027, -7%)"
        # was More Personal Computing's fall from its USD 54.05bn.
        base = (getattr(v, "segment_base", None) or {}).get(name)
        points = ([base] if isinstance(base, (int, float)) else []) + list(values)
        lead = len(points) - len(values)
        for i, (r, amount) in enumerate(zip(rows, values)):
            year = _norm_year(r.label)
            pool.add_metric(key, year, "money", amount * mn)
            if r.revenue:
                pool.add_metric(key, year, "%", amount / r.revenue * 100)
            now = i + lead
            for j in range(now):
                if points[j] > 0 and amount > 0:
                    pool.add_metric(key, year, "%", (amount / points[j] - 1) * 100)
                    pool.add_metric(key, year, "%", ((amount / points[j]) ** (1 / (now - j)) - 1) * 100)
    for name, spec in v.drivers.items():
        if not (isinstance(spec, dict) and isinstance(spec.get("values"), list)
                and len(spec["values"]) == len(rows)):
            continue
        key = f"driver:{name}"
        pattern = _name_pattern(name)
        if pattern:
            pool.names.append((pattern, key))
        base = spec.get("base") if isinstance(spec.get("base"), (int, float)) else None
        points = [base] + list(spec["values"])
        for i, (r, x) in enumerate(zip(rows, spec["values"])):
            if not isinstance(x, (int, float)):
                continue
            year = _norm_year(r.label)
            for f in figures(_with_unit(x, spec.get("unit"))):
                pool.add_metric(key, year, f.kind, f.value)
            for kind in ("money", "count", "bare"):
                pool.add_metric(key, year, kind, x)
            # A change in a driver: NFLX's "net additions of 20 million in FY2026".
            prior = points[i]
            if isinstance(prior, (int, float)):
                for f in figures(_with_unit(x - prior, spec.get("unit"))):
                    pool.add_metric(key, year, f.kind, f.value)
                    pool.add(f.kind, f.value)
            for j, start in enumerate(points[:i + 1]):
                if isinstance(start, (int, float)) and start > 0 and x > 0:
                    pool.add_metric(key, year, "%", (x / start - 1) * 100)
                    pool.add_metric(key, year, "%", ((x / start) ** (1 / (i + 1 - j)) - 1) * 100)
    pool.names.sort(key=lambda t: -len(t[0].pattern))
    _growth_pool(pool, v)

    # Multiples the code computed, by what they divide.
    fx, shares = v.fx or 1.0, v.shares
    minority = getattr(v, "minority", 0.0) or 0.0
    net_debt = v.bridge_debt - v.cash + minority
    last = rows[-1]
    for p in (v.price, v.per_share):
        cap = p * shares / fx / mn
        ev = cap + net_debt
        for ebitda in (data.ttm_ebitda, rows[0].ebitda, last.ebitda):
            if ebitda and ebitda > 0:
                pool.add_multiple("ev_ebitda", ev / ebitda)
        for ebit in (rows[0].ebit, last.ebit):
            if ebit > 0:
                pool.add_multiple("ev_ebit", ev / ebit)
        if data.ttm_net_income and data.ttm_net_income > 0:
            pool.add_multiple("pe", cap / data.ttm_net_income)
        for nopat in (rows[0].nopat, last.nopat):
            if nopat > 0:
                pool.add_multiple("pe", cap / nopat)
        for fcf in (rows[0].fcf, last.fcf):
            if fcf > 0:
                pool.add_multiple("fcf", cap / fcf)
        if data.ttm_revenue:
            pool.add_multiple("ev_sales", ev / data.ttm_revenue)
    pool.add_multiple("ev_ebitda", v.terminal_ev_ebitda)
    pool.add_multiple("ev_ebitda", getattr(data, "quoted_ev_ebitda", None))
    if last.ebit > 0:
        pool.add_multiple("ev_ebit", v.terminal_value / last.ebit)
    if last.fcf > 0:
        pool.add_multiple("fcf", v.terminal_value / last.fcf)
        pool.terminal_multiples["fcf"] = v.terminal_value / last.fcf
    if v.terminal_ev_ebitda:
        pool.terminal_multiples["ev_ebitda"] = v.terminal_ev_ebitda
    if last.ebit > 0:
        pool.terminal_multiples["ev_ebit"] = v.terminal_value / last.ebit
    if v.exit_multiple:
        pool.add_multiple("ev_ebitda" if v.exit_basis == "EBITDA" else "ev_ebit", v.exit_multiple)
    if v.peers:
        for p in v.peers.peers:
            pool.add_multiple("ev_ebitda", p.ev_ebitda)
            pool.add_multiple("pe", p.pe)
        pool.add_multiple("ev_ebitda", v.peers.median_ev_ebitda)
        pool.add_multiple("pe", getattr(v.peers, "median_pe", None))
    pool.company_words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", data.name or "")} | {
        (data.symbol or "").lower()}


def build_pool(trusted_texts: list[str], notes_texts: list[str], v, data,
               model: dict) -> Pool:
    """What the writer may cite. `trusted_texts` carry their units (placeholder values,
    the computed summary); `notes_texts` are the research notes and the market data
    block, matched with their context; `v` is the valuation, `data` the market data and
    `model` the JSON header's model object."""
    pool = Pool()
    for text in trusted_texts:
        if text:
            pool.add_text(text)
    for text in notes_texts:
        if text:
            pool.add_notes(text)
            _statement_pool(pool, text)
    pool.add_model(model or {})
    for spec in v.drivers.values():
        if isinstance(spec, dict) and isinstance(spec.get("base"), (int, float)):
            for f in figures(_with_unit(spec["base"], spec.get("unit"))):
                pool.add(f.kind, f.value)

    mn = 1e6
    rows = v.rows
    fx, shares = v.fx or 1.0, v.shares
    pool.forecast = {_norm_year(r.label) for r in rows}
    base = [v.base_revenue] + [r.revenue for r in rows]
    for case in (rows, getattr(v, "downside_rows", None) or []):
        for i, r in enumerate(case):
            year = _norm_year(r.label)
            for amount in (r.revenue, r.ebit, r.nopat, r.da, r.capex, r.nwc_change, r.fcf,
                           r.ebitda, r.pv_fcf, r.ebitda - r.capex, r.capex - r.da):
                pool.add_year(year, "money", amount * mn)
            for share in (r.ebit_margin, r.fcf_margin, r.da / r.revenue if r.revenue else None,
                          r.capex / r.revenue if r.revenue else None,
                          r.ebitda / r.revenue if r.revenue else None,
                          r.nopat / r.revenue if r.revenue else None,
                          r.fcf / r.nopat if r.nopat else None):
                if share is not None:
                    pool.add_year(year, "%", share * 100)
            revenue = [base[0]] + [x.revenue for x in case]
            for start in {0, 1, i}:
                if start <= i and revenue[start] > 0 and r.revenue > 0:
                    span = i + 1 - start
                    pool.add_year(year, "%", (r.revenue / revenue[start] - 1) * 100)
                    pool.add_year(year, "%", ((r.revenue / revenue[start]) ** (1 / span) - 1) * 100)
    for name, series in v.segments.items():
        for i, (r, amount) in enumerate(zip(rows, series)):
            year = _norm_year(r.label)
            pool.add_year(year, "money", amount * mn)
            if r.revenue:
                pool.add_year(year, "%", amount / r.revenue * 100)
            for start in {0, i - 1}:
                if 0 <= start < i and series[start] > 0 and amount > 0:
                    pool.add_year(year, "%", (amount / series[start] - 1) * 100)
                    pool.add_year(year, "%", ((amount / series[start]) ** (1 / (i - start)) - 1) * 100)
    for spec in v.drivers.values():
        if not (isinstance(spec, dict) and isinstance(spec.get("values"), list)
                and len(spec["values"]) == len(rows)):
            continue
        first = spec.get("base") if isinstance(spec.get("base"), (int, float)) else None
        for i, (r, x) in enumerate(zip(rows, spec["values"])):
            if not isinstance(x, (int, float)):
                continue
            year = _norm_year(r.label)
            for f in figures(_with_unit(x, spec.get("unit"))):
                pool.add_year(year, f.kind, f.value)
                pool.add_year(year, "money", f.value)
            if first and first > 0 and x > 0:
                pool.add_year(year, "%", ((x / first) ** (1 / (i + 1)) - 1) * 100)
                pool.add_year(year, "%", (x / first - 1) * 100)
    _metric_pool(pool, v, data)
    last = rows[-1]
    for amount in (v.terminal_value, v.pv_terminal):
        pool.add_year(_norm_year(last.label), "money", amount * mn)
    # Rates that hold for every year: "4.5% perpetual growth on FY2030 FCF".
    tax = (model or {}).get("tax_rate")
    for rate in (v.wacc, v.growth, v.implied_growth, v.terminal_share,
                 *(tax if isinstance(tax, list) else [tax])):
        if isinstance(rate, (int, float)):
            for year in pool.forecast:
                pool.add_year(year, "%", rate * 100)
    # The sensitivity grid: "at a 8.2% WACC the value falls to 73.58".
    for w in (v.wacc + d for d in (-0.02, -0.01, 0.0, 0.01, 0.02)):
        if v.method == "perpetuity":
            cells = [value_at(v, w, growth=v.growth + d) for d in (-0.01, -0.005, 0, 0.005, 0.01)]
        else:
            cells = [value_at(v, w, multiple=v.exit_multiple + d) for d in (-4, -2, 0, 2, 4)
                     if v.exit_multiple + d > 0]
        for cell in cells:
            if cell is not None:
                pool.add("money", cell)
                pool.add("%", (cell / v.price - 1) * 100)
    for amount in (v.enterprise_value, v.equity_value, v.terminal_value, v.pv_terminal,
                   v.pv_fcf, v.debt - v.cash, v.cash, v.debt, v.finance_debt,
                   v.bridge_debt, v.bridge_debt - v.cash, getattr(v, "minority", 0.0)):
        pool.add("money", amount * mn)
    _growth(pool, [v.base_revenue] + [r.revenue for r in rows])
    for series in ([r.ebit for r in rows], [r.fcf for r in rows], [r.ebitda for r in rows]):
        _growth(pool, series)
    for i, r in enumerate(rows):
        total = sum(vals[i] for vals in v.segments.values()) or r.revenue
        for vals in v.segments.values():
            if total:
                pool.add("%", vals[i] / total * 100)
    for vals in v.segments.values():
        _growth(pool, vals)
    for spec in v.drivers.values():
        if isinstance(spec, dict) and isinstance(spec.get("values"), list):
            base = spec.get("base")
            _growth(pool, ([base] if isinstance(base, (int, float)) else []) + spec["values"])
    if data.revenue:
        _growth(pool, [data.revenue] + [r.revenue for r in rows])
    for label, revenue, ebitda in getattr(data, "history", None) or []:
        if revenue and ebitda:
            pool.add("%", ebitda / revenue * 100)
    history = [h[1] for h in getattr(data, "history", None) or [] if h[1]]
    _growth(pool, history)
    if data.ttm_revenue and data.revenue:
        pool.add("%", (data.ttm_revenue / data.revenue - 1) * 100)

    # What the market pays, and what each value in the pitch would pay, today and on
    # the first forecast year: "consensus implies ~18x" is fine when it does.
    net_debt = v.debt - v.cash + (getattr(v, "minority", 0.0) or 0.0)
    prices = [v.price, v.per_share, data.analyst_target, v.downside_per_share,
              v.cross_check_per_share]
    if v.peers:
        prices += [v.peers.value_ev_ebitda, v.peers.value_pe]
        for p in v.peers.peers:
            pool.add("x", p.ev_ebitda)
            pool.add("x", p.pe)
    for f in getattr(data, "firm_targets", None) or []:
        prices.append(f.target)
    prices = [p for p in prices if p]
    for p in prices:
        cap = p * shares / fx / mn
        ev = cap + net_debt
        pool.add("money", p)
        pool.add("%", (p / v.price - 1) * 100)
        if p in (v.price, v.per_share):     # at other prices these are rarely cited
            pool.add("money", cap * mn)
            pool.add("money", ev * mn)
        for ebitda in (data.ttm_ebitda, rows[0].ebitda, last.ebitda):
            if ebitda and ebitda > 0:
                pool.add("x", ev / ebitda)
        for ebit in (rows[0].ebit, last.ebit):
            if ebit > 0:
                pool.add("x", ev / ebit)
        if data.ttm_revenue:
            pool.add("x", ev / data.ttm_revenue)
        if data.ttm_net_income and data.ttm_net_income > 0:
            pool.add("x", cap / data.ttm_net_income)
            pool.add("money", p / (cap / data.ttm_net_income))       # EPS
        if rows[0].nopat > 0:
            pool.add("x", cap / rows[0].nopat)
        for q in prices:
            if q and q != p:
                pool.add("%", (p / q - 1) * 100)
        pool.add("%", cap / (cap + v.debt) * 100 if cap + v.debt else None)
        pool.add("%", v.debt / cap * 100 if cap else None)
    for ebitda in (data.ttm_ebitda, rows[0].ebitda):
        if ebitda and ebitda > 0:
            pool.add("x", net_debt / ebitda)
            pool.add("x", v.debt / ebitda)
    pool.add("x", v.terminal_value / last.ebit if last.ebit > 0 else None)
    pool.add("x", v.terminal_ev_ebitda)
    pool.add("x", getattr(data, "quoted_ev_ebitda", None))
    if v.implied_growth is not None:
        pool.add("%", v.implied_growth * 100)
    pool.add("%", v.terminal_share * 100)
    pool.add("%", (1 - v.terminal_share) * 100)
    pool.add("%", v.wacc * 100)
    if v.cost:
        c = v.cost
        for x in (c.risk_free, c.erp, c.cost_of_equity, c.cost_of_debt, c.debt_weight,
                  1 - c.debt_weight, c.premium, c.cost_of_debt / (1 - 0.21)):
            pool.add("%", x * 100)
        pool.add("plain", c.beta)
        pool.add("plain", c.beta_raw)
    # The recommendation rule and the checks' own bounds, quoted from the instructions.
    for x in (25, 10, 4, 1, 2):
        pool.add("%", x)
    return pool
