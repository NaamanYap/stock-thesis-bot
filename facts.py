"""Every number a pitch prints comes from something the code read, never from the writer.

provenance.py checks typed figures afterwards, by matching each one against 80,000
characters of notes, and a match is a guess. SPGI's pitch of 2026-10-02 had 27 figures
it could not place and 40 more it placed wrongly: "~90% recurring revenue" matched an
unrelated 89.7% in the model, "~33% of revenue" (30.8%) an unrelated 33% in the notes,
and figures were credited to a 10-K the writer never read. The prompt already forbade
all of it.

So the writer no longer types numbers. The code gives every figure in the research notes
and the market data block an id ("$4,146 [#F212]"), and the writer cites the id
("{{F212}}"). The code prints the figure exactly as the source wrote it, with the source
it was read from, so a figure can be wrong only by being the wrong fact. A share, ratio
or change between two figures is computed ("{{share:F1/F2}}"), and the writer's own
estimates are declared in the JSON header and printed as assumptions ("{{assume:id}}").
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse

from provenance import NOT_AN_END, SENTENCE as SENTENCE_END, Figure, figures

TAG = " [#{}]"
TAGGED = re.compile(r" \[#F\d+\]")
BLOCK = re.compile(r"^## \[(?P<kind>PRIMARY|SECONDARY|SNIPPETS)\] (?P<name>\w+): (?P<arg>.*)$", re.M)
FILING_LABEL = re.compile(r"^\[(?:latest )?(?P<label>[^\]\n]+)\]$", re.M)
URL = re.compile(r"https?://[^\s)\]|>]+")
# {{F12}}, {{share:F1/F2}}, {{ratio:F1/F2}}, {{change:F1/F2}}, {{assume:leverage_target}},
# and a figure sheet name in place of any id: {{revenue_FY2025}},
# {{share:ratings_revenue_2Q26/revenue_2Q26}}. A name that is neither (the model's
# {{wacc}}) is left for the model's filler.
NAME = r"[A-Za-z][A-Za-z0-9_]*"
REF = re.compile(r"\{\{\s*(?:(?P<op>share|ratio|change)\s*:\s*(?P<a>" + NAME + r")\s*/\s*(?P<b>"
                 + NAME + r")|(?P<id>" + NAME + r")|assume\s*:\s*(?P<assume>[\w.-]+))\s*\}\}", re.I)
FACT_ID = re.compile(r"F\d+", re.I)


def find(by_id: dict, name: str | None) -> "Fact | None":
    """The fact an id or a sheet name cites: "F12" in any case, a name as written or in
    another case ("Revenue_FY2025")."""
    if not name:
        return None
    hit = by_id.get(name) or by_id.get(name.upper())
    if hit is None and not FACT_ID.fullmatch(name):
        low = name.lower()
        hit = next((f for k, f in by_id.items() if k.lower() == low), None)
    return hit


def is_citation(name: str | None, by_id: dict) -> bool:
    """An id or a sheet name the database holds, or an id-shaped name it does not: the
    model's own placeholders are neither."""
    return bool(name) and (FACT_ID.fullmatch(name) is not None or find(by_id, name) is not None)
ANY_PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")
# A source marker left in the text while filling, turned into "(source)" once per
# sentence after the last figure from that source.
MARK = "\x00{}\x00"
MARKED = re.compile(r"\x00([^\x00]*)\x00")


@dataclass
class Fact:
    id: str
    figure: Figure
    source: str             # where the code read it: "10-Q, filed 2026-07-28", "bullfincher.io"
    primary: bool           # the company's own filings and releases, or the market data
    context: str            # the words around it, for the reviewer and the ledger
    # Read from the table the figure sits in, by code; blank for a figure in running text.
    metric: str = ""        # the row label: "Revenue", "Assets: Goodwill"
    period: str = ""        # the column heading as written: "Three Months 2026"
    table: str = ""         # the table's title
    period_key: tuple | None = None    # ("3M", "2026-06"): months covered, and the end
    data_type: str = ""     # "reported", "guidance", "third-party" or "market"

    @property
    def text(self) -> str:
        return self.figure.text


@dataclass
class Assumption:
    id: str
    figure: Figure          # the first figure; a range has two
    basis: str
    text: str = ""          # as declared: "2.0x", "2-3%"


@dataclass
class Facts:
    """The fact database the writer cites from, the notes it reads for context, and what
    each id is."""
    by_id: dict[str, Fact]
    digest: str             # the research notes without the tables: context, no ids
    database: str           # the market data and the filing and XBRL tables, every figure tagged
    fy_end_month: int = 12  # the month the company's fiscal year ends, for "2Q26"
    sheet: object = None    # figure_sheet.Sheet: the named figures the writer is shown
    # Figures in the sentences of the company's own filings and releases (S-ids), cited
    # only when the writer states one as the company does (adopt_statements).
    statements: list[Fact] = field(default_factory=list)


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _block_source(kind: str, name: str, arg: str, body: str) -> str:
    if name == "fetch_page":
        label = FILING_LABEL.search(body[:300])
        if label:
            return label.group("label")
        if _domain(arg) == "sec.gov":
            # "6-K filed with the SEC", not "SEC filing tm264427d1_6k.htm" (NOK, 2026-10-04).
            form = re.search(r"\bFORM\s+(10-K|10-Q|8-K|6-K|20-F|40-F)\b", body[:600], re.I)
            if form:
                return f"{form.group(1).upper()} filed with the SEC"
            return "SEC filing " + urlparse(arg).path.rsplit("/", 1)[-1]
        return _domain(arg)
    if name == "filing_section":
        return arg.strip()
    if name == "filing_tables":
        return re.sub(r"^latest ", "", arg.strip()) + ", tables"
    if name == "sec_xbrl":
        return "SEC XBRL company facts"
    if name == "derived_facts":
        return "calculated from reconciled filing facts"
    return "web search"


def _context(text: str, f: Figure, width: int = 110) -> str:
    lo, hi = max(0, f.start - width), min(len(text), f.end + width)
    return " ".join(text[lo:hi].split())


def _tag(text: str, facts: dict[str, Fact], source_at, primary_at, fy_end: int,
         data_type_at) -> str:
    """`text` with " [#Fn]" after every figure, recording each in `facts` with the label
    of the table cell it sits in. Exhibit, note and page numbers are not figures."""
    out, last = [], 0
    for f in figures(text):
        if NOT_A_FIGURE.search(text[max(0, f.start - 14):f.start]) or _in_url(text, f.start):
            continue
        fact_id = f"F{len(facts) + 1}"
        metric, period, table = _label_at(text, f.start)
        fact = Fact(fact_id, f, source_at(f.start), primary_at(f.start), _context(text, f),
                    metric, period, table, period_key(period, table, fy_end) if period else None)
        fact.data_type = ("guidance" if GUIDANCE.search(f"{table} {metric}")
                          or (not period and GUIDANCE.search(_clause(text, f)))
                          else data_type_at(f.start))
        facts[fact_id] = fact
        out.append(text[last:f.end] + TAG.format(fact_id))
        last = f.end
    out.append(text[last:])
    return "".join(out)


NOT_A_FIGURE = re.compile(r"\b(?:exhibit|note|item|page|section|rule|form|schedule|ex)\s*$",
                          re.I)
GUIDANCE = re.compile(r"\b(?:guid\w*|outlook|expect\w*|forecast\w*|target\w*|anticipat\w*|"
                      r"project\w*|plan\w*|aim\w*|intend\w*|intent\w*|commit\w*)\b", re.I)


def _in_url(text: str, pos: int) -> bool:
    start = text.rfind(" ", 0, pos) + 1
    return "://" in text[start:pos] or text[start:pos].startswith("www.")


def _clause(text: str, f: Figure) -> str:
    """The sentence a figure sits in."""
    lo = max(text.rfind(". ", 0, f.start), text.rfind("\n", 0, f.start)) + 1
    ends = [i for i in (text.find(". ", f.end), text.find("\n", f.end)) if i >= 0]
    return text[lo:min(ends) if ends else len(text)]


def _label_at(text: str, pos: int) -> tuple[str, str, str]:
    """(row label, column heading, table title) of the table cell at `pos`: a row the
    filing table reader wrote ("- Revenue — Three Months 2026: USD 4,146mn; ..."), or a
    markdown table row under a header row. Blank for running text."""
    lo = text.rfind("\n", 0, pos) + 1
    hi = text.find("\n", pos)
    line = text[lo:hi if hi >= 0 else len(text)]
    at = pos - lo
    title = _title_before(text, lo)
    row = TABLE_ROW.match(line)
    if row:
        if at < row.start("cells"):
            return "", "", ""
        offset = row.start("cells")
        for cell in row.group("cells").split("; "):
            if offset <= at < offset + len(cell):
                column = cell.split(": ", 1)[0] if ": " in cell else ""
                return row.group("row").strip(), column.strip(), title
            offset += len(cell) + 2
        return row.group("row").strip(), "", title
    if line.lstrip().startswith("|"):
        cells = line.split("|")
        index = sum(1 for _ in re.finditer(r"\|", line[:at])) - 1
        header = _header_above(text, lo)
        label = cells[1].strip() if len(cells) > 2 else ""
        if index <= 0 or header is None:
            return "", "", ""
        heads = header.split("|")
        column = heads[index + 1].strip() if index + 1 < len(heads) else ""
        return label, column, title
    return "", "", ""


TABLE_ROW = re.compile(r"^- (?P<row>.+?) — (?P<cells>.+)$")
SEPARATOR = re.compile(r"^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$")


def _title_before(text: str, lo: int) -> str:
    """The nearest heading above a line."""
    m = None
    for m in re.finditer(r"^#{2,4} (.+)$", text[:lo], re.M):
        pass
    return m.group(1).strip() if m else ""


def _header_above(text: str, lo: int) -> str | None:
    """The header row of the markdown table a line belongs to."""
    lines = text[:lo].rstrip("\n").split("\n")
    for i in range(len(lines) - 1, 0, -1):
        if SEPARATOR.match(lines[i]):
            return lines[i - 1]
        if not lines[i].lstrip().startswith("|"):
            return None
    return None


MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
SPAN_WORDS = {"three": 3, "six": 6, "nine": 9, "twelve": 12}


def period_key(period: str, table: str, fy_end: int) -> tuple | None:
    """("3M", "2026-06") for a quarter to June 2026, ("12M", "2025-12") for a year,
    ("at", "2026-06") for a balance at a date; None when the heading does not say."""
    both = f"{period} {table}"
    iso = re.search(r"\b((?:19|20)\d{2})-(\d{2})-\d{2}\b", period)
    if iso:
        span = ("3M" if re.search(r"quarter", table, re.I)
                else "12M" if re.search(r"annual|income statement|cash flow", table, re.I)
                else "at" if re.search(r"balance", table, re.I) else None)
        return (span, f"{iso.group(1)}-{iso.group(2)}") if span else None
    q = QUARTER.search(period)
    if q:
        return _quarter_key(*_quarter_parts(q), fy_end)
    # "H1 2026", as NVO's 6-K heads its half-year columns: read as sentences read it, or
    # "DKK 23,986mn in H1 2026" never meets its cell (2026-10-11).
    h = HALF.search(period)
    if h:
        half = int(h.group("h1")) if h.group("h1") else (
            1 if h.group("word").lower() == "first" else 2)
        return _quarter_key(2 * half, _year(h.group("y1") or h.group("y2")), fy_end, 6)
    year = re.search(r"\b((?:19|20)\d{2})\b", period)
    if not year and re.search(r"change|y/y|growth", period, re.I):
        # "Three Months % Change" is the latest year's change: the first year in the title.
        year = re.search(r"\b((?:19|20)\d{2})\b", table)
    if not year:
        return None
    span = re.search(r"\b(three|six|nine|twelve)\s+months\b", both, re.I)
    month = re.search(r"\b(" + "|".join(MONTHS) + r")\s+\d{1,2},", both, re.I)
    end_month = MONTHS[month.group(1).lower()] if month else fy_end
    if span:
        return f"{SPAN_WORDS[span.group(1).lower()]}M", f"{year.group(1)}-{end_month:02d}"
    if re.search(r"\b(?:year|fiscal|annual)\b", both, re.I) and not month:
        return "12M", f"{year.group(1)}-{fy_end:02d}"
    if month and not span:
        return "at", f"{year.group(1)}-{end_month:02d}"
    return None


# "2Q26", "2Q '26", "Q2 2026", "Q2 FY26", "second quarter of 2026"
QUARTER = re.compile(r"\b(?:(?P<q1>[1-4])Q\s?'?(?P<y1>\d{2}|\d{4})|Q(?P<q2>[1-4])\s?(?:FY)?\s?'?"
                     r"(?P<y2>\d{4}|\d{2})|(?P<word>first|second|third|fourth)[- ]quarter"
                     r"(?: of)?(?: fiscal)?(?: year)? (?P<y3>\d{4}))\b", re.I)
HALF = re.compile(r"\b(?:H(?P<h1>[12])\s?'?(?P<y1>\d{4}|\d{2})|(?P<word>first|second) half"
                  r"(?: of)?(?: fiscal)?(?: year)? (?P<y2>\d{4}))\b", re.I)
FULL_YEAR = re.compile(r"\b(?:FY\s?'?(?P<y1>\d{4}|\d{2})|fiscal (?:year )?(?P<y2>\d{4})|"
                       r"full[- ]year (?P<y3>\d{4}))\b", re.I)
ORDINAL = {"first": 1, "second": 2, "third": 3, "fourth": 4}


def _year(text: str) -> int:
    y = int(text)
    return y + 2000 if y < 100 else y


def _quarter_parts(m: re.Match) -> tuple[int, int]:
    if m.group("q1"):
        return int(m.group("q1")), _year(m.group("y1"))
    if m.group("q2"):
        return int(m.group("q2")), _year(m.group("y2"))
    return ORDINAL[m.group("word").lower()], _year(m.group("y3"))


def _quarter_key(q: int, fy: int, fy_end: int, months: int = 3) -> tuple:
    """The quarter `q` (or the half ending with it) of fiscal year `fy`, as (span, end)."""
    end = fy_end - 3 * (4 - q)
    year = fy
    while end <= 0:
        end += 12
        year -= 1
    return f"{months}M", f"{year}-{end:02d}"


def mentioned_periods(sentence: str, fy_end: int) -> list[tuple]:
    """The flow periods a sentence names: quarters, halves and fiscal years."""
    out = [_quarter_key(*_quarter_parts(m), fy_end) for m in QUARTER.finditer(sentence)]
    for m in HALF.finditer(sentence):
        h = int(m.group("h1")) if m.group("h1") else (1 if m.group("word").lower() == "first" else 2)
        out.append(_quarter_key(2 * h, _year(m.group("y1") or m.group("y2")), fy_end, 6))
    for m in FULL_YEAR.finditer(sentence):
        out.append(("12M", f"{_year(m.group('y1') or m.group('y2') or m.group('y3'))}-{fy_end:02d}"))
    return out


# What a figure measures, by the words that name it. A citation measures one of these
# (its metric's), and the words in front of it say which one the reader takes it for:
# ESLT's of 2026-10-07 wrote "increase R&D to above {{da_pct_revenue_FY2025}} of revenue",
# which printed D&A's 2.2% as R&D, which was 7.6%.
METRIC_TERMS = [
    ("r&d", r"\bR&D\b|research and development|\bresearch spend"),
    ("sg&a", r"\bSG&A\b|selling,? general"),
    ("d&a", r"\bD&A\b|depreciation|amorti[sz]ation"),
    ("capex", r"\bcapex\b|capital expenditure|capital spending"),
    ("ebitda", r"\bEBITDA\b"),
    ("gross", r"\bgross (?:margin|profit)"),
    ("operating", r"\boperating (?:margin|income|profit)|\bEBIT\b(?!DA)"),
    ("net income", r"\bnet (?:income|profit|margin|earnings)|\bearnings per share|\bEPS\b"),
    ("fcf", r"\bfree cash flow|\bFCF\b"),
    ("backlog", r"\bbacklog|\border book"),
    ("tax", r"\btax rate|\beffective tax"),
    ("dividend", r"\bdividend"),
    ("buyback", r"\bbuy-?backs?\b|\brepurchase"),
    ("revenue", r"\brevenues?\b|\bsales\b|\btop[- ]line"),
    ("roic", r"\bROIC\b|return on (?:invested )?capital"),
    ("wacc", r"\bWACC\b|cost of capital|discount rate"),
]
METRIC_RE = [(name, re.compile(p, re.I)) for name, p in METRIC_TERMS]
# The denominator of a share, which names no measure: "D&A as a share of revenue".
PER_REVENUE = re.compile(r"\s*(?:as a (?:share|percentage|%) of|of|to|/)\s+(?:total\s+)?"
                         r"(?:revenues?|sales)\b.*$|_pct_revenue\w*$", re.I)
CLAUSE_BREAK = re.compile(r"[;:(\[]|\.\s|\}\}")
OTHER_MEASURE = re.compile(r"\b(?:margins?|growth|multiples?|yields?)\b", re.I)
LEAD_CHARS = 50     # how far before a citation the words that name it may be


def metric_terms(text: str) -> list[tuple[int, str]]:
    """(position, measure) for each measure the text names, in order."""
    found = []
    for name, rx in METRIC_RE:
        found += [(m.start(), name) for m in rx.finditer(text)]
    return sorted(found)


def cited_measures(name: str, fx: "Facts | None") -> set[str]:
    """What a cited name measures: its fact's metric, or the model placeholder's words; an
    empty set when nothing says (a company statement, a CAGR)."""
    fact = find(fx.by_id, name) if fx is not None else None
    if fact is not None:
        label = PER_REVENUE.sub("", fact.metric or "") or PER_REVENUE.sub("", fact.id).replace("_", " ")
        return {m for _, m in metric_terms(label.replace("_", " "))}
    base = re.split(r"[:.]", name)[0]
    if base.startswith(("segment", "driver")):
        return set()
    words = " ".join(MODEL_WORDS.get(base, []))
    return {m for _, m in metric_terms(PER_REVENUE.sub("", base).replace("_", " ") + " " + words)}


def wrong_measure(sentence: str, fx: "Facts | None") -> list[str]:
    """Why each citation in `sentence` reads as a measure it is not: the nearest measure
    named before it in its clause is not its own, and none of its own follows it."""
    out = []
    for m in ANY_PLACEHOLDER.finditer(sentence):
        inner = m.group(0)[2:-2].strip()
        if inner.lower().startswith("assume"):
            continue
        op = re.match(r"(share|ratio|change)\s*:\s*([^/]+)/(.+)$", inner, re.I)
        names = [op.group(2).strip(), op.group(3).strip()] if op else [inner]
        own = set().union(*(cited_measures(n, fx) for n in names))
        if not own:
            continue
        before = sentence[max(0, m.start() - LEAD_CHARS):m.start()]
        breaks = list(CLAUSE_BREAK.finditer(before))
        lead = before[breaks[-1].end():] if breaks else before
        named = metric_terms(lead)
        if not named or named[-1][1] in own:
            continue
        # "R&D/SG&A as % of revenue trend, with modeled margins of {{ebit_margin}}": a
        # measure word after the name makes the citation that measure, of something else.
        if OTHER_MEASURE.search(lead[named[-1][0]:]):
            continue
        after = sentence[m.end():m.end() + 30]
        after = re.sub(r"^\s*(?:of|to|/)\s+(?:total\s+)?(?:revenues?|sales)\b", "", after, flags=re.I)
        follows = CLAUSE_BREAK.split(after)[0]
        if any(t in own for _, t in metric_terms(follows)):
            continue
        out.append(f"{m.group(0)} is {', '.join(sorted(own))}, but the words before it "
                   f"say it is {named[-1][1]} (\"{lead.strip()[-40:]}\")")
    return out


def misused(text: str, fx: "Facts", segments: list[str] | tuple = ()) -> list[tuple[str, str]]:
    """Sentences that cite a labelled fact for something its label rules out, as
    (sentence, why): a quarter's figure in a sentence that names only other periods,
    guidance in a sentence with no word saying it is guidance, or one segment's figure in
    the words about another (`segments`, the model's segment names)."""
    out = []
    for sentence in SENTENCE_END.split(text):
        for why in wrong_measure(sentence, fx)[:1]:
            out.append((sentence, why))
        names = mentioned_periods(ANY_PLACEHOLDER.sub(" ", sentence), fx.fy_end_month)
        previous = 0
        for m in REF.finditer(sentence):
            lead, previous = sentence[previous:m.start()], m.end()
            ids = [m.group("id")] if m.group("id") else [m.group("a"), m.group("b")]
            whole = _share_of_other(sentence, m, fx)
            if whole:
                out.append((sentence, whole))
                continue
            for fact_id in filter(None, ids):
                fact = find(fx.by_id, fact_id)
                if fact is None:
                    continue
                key = fact.period_key
                flows = [n for n in names if n[0] != "at"]
                if key and key[0] != "at" and flows and key not in flows:
                    out.append((sentence, f"{fact.id} ({fact.text}) is {_describe(key)} "
                                f"(\"{fact.period}\" in {fact.source}), but the sentence names "
                                + ", ".join(_describe(n) for n in dict.fromkeys(flows))))
                elif fact.data_type == "guidance" and not GUIDANCE.search(
                        ANY_PLACEHOLDER.sub(" ", sentence)):
                    out.append((sentence, f"{fact.id} ({fact.text}) is guidance ({fact.source}), "
                                "but the sentence presents it as a result"))
                else:
                    own = [s for s in segments if _names(fact.metric, s)]
                    # "led by {{a}} in Ratings and {{b}} in Indices": the segment follows
                    # its figure, and the words before b are a's (SPGI, 2026-10-05).
                    after = sentence[m.end():m.end() + 40]
                    lead = re.sub(r"^\s*(?:in|for|at|from)\s+(?:the\s+)?[A-Z][\w&.-]*"
                                  r"(?:\s+[A-Z][\w&.-]*)*", "", lead)
                    other = [s for s in segments if s not in own and _names(lead, s)]
                    trailing = any(re.match(r"\s*(?:in|for|at|from)\s+(?:the\s+)?"
                                            + re.escape(s) + r"(?!\w)", after, re.I) for s in own)
                    if own and other and not trailing and not any(_names(lead, s) for s in own):
                        out.append((sentence, f"{fact.id} ({fact.text}) is {own[0]}'s "
                                    f"(\"{fact.metric}\" in {fact.source}), but the words "
                                    f"before it are about {other[0]}"))
    return out


SHARE_OF = re.compile(r"\s+of\s+(?:the\s+|all\s+|its\s+|global\s+)?([a-z][\w-]*(?:\s+[a-z][\w-]*)?)",
                      re.I)
SHARE_NOUN = re.compile(r"\s+([A-Z]{2,}s?|[A-Z][a-z]+)\b")


def _share_of_other(sentence: str, m: re.Match, fx: "Facts") -> str:
    """Why a {{share:a/b}} is printed as a share of something b is not, or "". SPOT's of
    2026-10-07 used the latest quarter's share of trailing revenue (26.4%) as "Android
    pre-install on 26.4% of smartphones" and "a 26.4% MAU base"."""
    if (m.group("op") or "").lower() != "share":
        return ""
    a, b = find(fx.by_id, m.group("a")), find(fx.by_id, m.group("b"))
    if a is None or b is None:
        return ""
    known = _words(f"{a.metric} {a.id} {b.metric} {b.id} {b.table}".replace("_", " ")) | {
        "total", "group", "company", "consolidated", "overall", "combined", "business"}
    if "revenue" in known:
        known |= {"sale", "turnover", "top-line"}
    after = sentence[m.end():m.end() + 40]
    of = SHARE_OF.match(after)
    noun = None if of else SHARE_NOUN.match(after)
    said = (of or noun).group(1) if (of or noun) else ""
    if not said or _words(said) & known or not _words(said):
        return ""
    return (f"{m.group(0)} is {a.id} as a share of {b.id} ({b.metric or b.id}), but the "
            f"sentence prints it as {'a share of ' if of else ''}\"{said}\"")


def _names(text: str, segment: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(segment) + r"(?!\w)", text, re.I))


def _describe(key: tuple) -> str:
    span, end = key
    return (f"the balance at {end}" if span == "at"
            else f"the {span.replace('M', ' months')} to {end}")


def unresolved(text: str, fx: "Facts", assumptions: dict) -> list[tuple[str, str]]:
    """Sentences citing an id or an assumption that does not exist, which would print
    "n.a.": SPGI's fifth rewrite shipped "approximately n.a. of division"."""
    out = []
    for sentence in SENTENCE_END.split(text):
        for m in REF.finditer(sentence):
            name = m.group("assume")
            if m.group("id") and not FACT_ID.fullmatch(m.group("id")):
                continue        # a sheet name, or the model's placeholder: checked elsewhere
            ids = [m.group("id")] if m.group("id") else [m.group("a"), m.group("b")]
            if (name and name not in assumptions) or any(
                    i and find(fx.by_id, i) is None for i in ids if not name):
                out.append((sentence, f"{m.group(0)} names nothing in the fact database or "
                            "your `assumptions`"))
                break
    return out


def credited_assumptions(text: str) -> list[tuple[str, str]]:
    """Sentences that cite one of the writer's assumptions and name a source for it, as
    (sentence, why). SPGI's fourth facts-mode rewrite declared "net revenue retention
    above 100%" as an assumption and credited it to "(company disclosure, 2025 10-K)": a
    figure credited to a source is a fact about the world, and it must be in the database."""
    out = []
    for sentence in SENTENCE_END.split(text):
        if REF.search(sentence) and re.search(r"\{\{\s*assume\s*:", sentence, re.I):
            source = SOURCED.search(ANY_PLACEHOLDER.sub(" ", sentence))
            if source:
                out.append((sentence, f"an assumption credited to a source (\"{source.group(0)}\"): "
                            "a sourced figure must come from the fact database"))
    return out


def strip_misused(text: str, fx: "Facts", segments: list[str] | tuple = (),
                  assumptions: dict | None = None) -> tuple[str, list[tuple[str, str]]]:
    """The text without the sentences `misused` finds, and (sentence, why) for each."""
    found = (misused(text, fx, segments) + credited_assumptions(text)
             + (unresolved(text, fx, assumptions) + historical_assumptions(text, fx)
                if assumptions is not None else []))
    leaned = []
    for sentence, why in found:
        # The bold lead goes too when it holds the figure at fault: NVDA's of 2026-10-12
        # kept "**... R&D scales with revenue (n.a.) and SG&A grows at n.a..**".
        label = LABEL.match(sentence)
        bad = {m.group(0) for m in REF.finditer(why)}
        whole = bool(label and bad and any(m.group(0) in bad
                                           for m in REF.finditer(label.group(1))))
        text, went = drop_sentence(text, sentence, keep_label=not whole)
        leaned += [(s, LEANED_WHY) for s in went]
    found += leaned
    before = set(text.split("\n"))
    lines = [l for l in text.split("\n") if not (EMPTY.match(l) and l.strip() and l not in before)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)), found


def _fy_end(market_block: str) -> int:
    """The month the fiscal year ends: the first column of the annual statements."""
    m = re.search(r"\|\s*Line item\s*\|\s*(?:19|20)\d{2}-(\d{2})-\d{2}", market_block)
    return int(m.group(1)) if m else 12


# The notes' blocks that are tables read by code, every figure in a labelled row: the
# fact database. Everything else (web pages, search snippets, a filing's running text) is
# context the writer reads but cannot cite.
STRUCTURED = ("filing_tables", "sec_xbrl", "derived_facts")
DATABASE_INTRO = (
    "# Fact database\n\n"
    "These are the only figures you may cite, each by the id after it. Every one was read "
    "by code from market data, the company's SEC XBRL facts, tables of its latest filings, "
    "or calculations from those records, and is what its row and column say, for that period "
    "only. The research notes "
    "further down are context: describe what they say in words, but their numbers are not "
    "in this database and cannot be printed.")


CURRENCY_GAP = re.compile(r"([$€£¥])\s+(?=\d)")


def build(digest: str, market_block: str) -> Facts:
    """The fact database, and the notes as context.

    The second facts-mode rewrite (SPGI, 2026-10-02) had 860 ids scattered through 80,000
    characters of notes, a third of them on web figures no code could label, and typed 183
    figures anyway. Only the structured sources carry ids now: the market data block and
    the filing table and XBRL blocks, moved out of the notes into one database the writer
    reads first.

    The company's own sentences are kept apart as statements (S-ids), cited only when the
    writer states one: NOK's pitch of 2026-10-04 lost "a contracted net sales run-rate of
    EUR 1.4 billion" from its 6-K, which no table holds.
    """
    # "$ 102 million": a filing's inline spans leave a space after the currency sign,
    # and the figure then reads as a count, never the writer's "$102 million". GS's pitch
    # of 2026-10-13 marked its quotes of the earnings release unverified for it.
    digest = CURRENCY_GAP.sub(r"\1", digest)
    facts: dict[str, Fact] = {}
    statements: list[Fact] = []
    fy_end = _fy_end(market_block)
    heads = list(BLOCK.finditer(digest))
    context = [digest[:heads[0].start()]] if heads else [digest]
    tables = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(digest)
        block = digest[h.start():end]
        if h.group("name") in STRUCTURED:
            tables.append((h, block))
        else:
            context.append(block)
            # The annual report's business and risk sections (gather.py) are the company's
            # sentences too: NVO's of 2026-10-11 marked its 20-F Item 4's "more than 69,500
            # employees" and "USD 11.7 billion" unverified.
            if h.group("kind") == "PRIMARY" and h.group("name") in ("fetch_page", "filing_section"):
                source = _block_source(h.group("kind"), h.group("name"), h.group("arg"), block)
                statements += _statements(block[h.end() - h.start():], source, fy_end,
                                          len(statements))
    database = [DATABASE_INTRO,
                _tag(market_block, facts, lambda _: "market data", lambda _: True, fy_end,
                     lambda _: "market")]
    for h, block in tables:
        source = _block_source(h.group("kind"), h.group("name"), h.group("arg"), block)
        data_type = "calculated" if h.group("name") == "derived_facts" else "reported"
        database.append(_tag(block.strip(), facts, lambda _, src=source: src, lambda _: True,
                             fy_end, lambda _, kind=data_type: kind))
    notes = "\n\n".join(c.strip() for c in context if c.strip())
    return Facts(facts, notes, "\n\n".join(database), fy_end, statements=statements)


def untag(text: str) -> str:
    return TAGGED.sub("", text)


def _pct(x: float) -> str:
    return f"{x:.1f}%" if abs(x) < 100 else f"{x:.0f}%"


def _compute(op: str, a: Fact, b: Fact) -> tuple[str | None, str]:
    """The share, ratio or change between two facts, or (None, why not)."""
    fa, fb = a.figure, b.figure
    if fa.kind != fb.kind:
        return None, f"{a.id} ({a.text}) and {b.id} ({b.text}) are not in the same unit"
    # SPGI's sheet rewrite of 2026-10-03 divided one segment's share of revenue by
    # another's and printed "Ratings and Indices together represent 246%".
    if fa.kind in ("%", "pp"):
        return None, (f"{a.id} and {b.id} are percentages already: cite each, and say in words "
                      "how they combine")
    if fb.value == 0 or fa.value == 0:
        return None, f"{a.id} or {b.id} is zero"
    # Two amounts a thousand times apart are one in millions and one in units, not a
    # share: a table that did not state its scale.
    if fa.kind == "money" and not 1e-3 < abs(fa.value / fb.value) < 1e3:
        return None, (f"{a.id} ({a.text}) and {b.id} ({b.text}) look like different scales "
                      "(millions against units)")
    if op == "share":
        return _pct(fa.value / fb.value * 100), ""
    if op == "ratio":
        return f"{fa.value / fb.value:.1f}x", ""
    change = (fb.value / fa.value - 1) * 100
    return ("+" if change > 0 else "") + _pct(change), ""


def fill(text: str, facts: Facts | None, assumptions: dict[str, Assumption],
         cited: dict[str, Fact] | None = None) -> tuple[str, list[str]]:
    """Swap every fact, computed and assumption reference for its figure, with the source
    once per sentence after the last figure from it. Returns the text and the problems:
    ids that name nothing, and computations between figures that cannot be compared.
    Every fact used is added to `cited`, for the reviewer and the ledger."""
    cited = cited if cited is not None else {}
    problems: list[str] = []
    by_id = facts.by_id if facts else {}

    def swap(m: re.Match) -> str:
        if m.group("assume"):
            a = assumptions.get(m.group("assume"))
            if a is None:
                problems.append(f"{{{{assume:{m.group('assume')}}}}} is not in your `assumptions`")
                return "n.a."
            # Once per sentence: "... 50% (my assumption) (my assumption)." read badly.
            after = m.string[m.end():m.end() + 60]
            label = "" if re.match(r"[^.;]{0,40}\((?:my|our) (?:estimate|assumption)", after) else OWN_MARK
            return f"{a.text or a.figure.text}{label}"
        if m.group("id"):
            fact = find(by_id, m.group("id"))
            if fact is None:
                if not FACT_ID.fullmatch(m.group("id")):
                    return m.group(0)       # the model's placeholder, filled later
                problems.append(f"{m.group(0)} is not an id in the fact database")
                return "n.a."
            cited[fact.id] = fact
            return fact.text + MARK.format(fact.source)
        a, b = find(by_id, m.group("a")), find(by_id, m.group("b"))
        if a is None or b is None:
            problems.append(f"{m.group(0)} names an id that is not in the fact database")
            return "n.a."
        cited[a.id], cited[b.id] = a, b
        op = m.group("op").lower()
        # "EBITDA margins near {{ratio:F1/F2}}" printed "0.5x": a ratio named as a margin,
        # share or conversion is a percentage.
        around = m.string[max(0, m.start() - 40):m.end() + 25]
        if op == "ratio" and re.search(r"margin|share|conversion|of (?:revenue|sales)|percent",
                                       around, re.I):
            op = "share"
        value, why = _compute(op, a, b)
        if value is None:
            problems.append(f"{m.group(0)} cannot be computed: {why}")
            return "n.a."
        # A sheet figure computed already says so: "computed from computed from" read badly.
        sources = dict.fromkeys(s.removeprefix("computed from ") for s in (a.source, b.source))
        return value + MARK.format("computed from " + " and ".join(sources))

    filled = REF.sub(swap, _drop_extra_units(text, by_id))
    return _place_sources(filled), problems


# A unit typed around a citation whose figure carries its own: SPGI's rewrite of
# 2026-10-03 printed "~USD 785 millionmn" from "~{{F121}}mn".
EXTRA_UNITS = re.compile(r"(?P<pre>\$|USD\s?)?(?P<ref>\{\{\s*(?:(?P<id>" + NAME + r")|(?:share|ratio|change)"
                         r"\s*:[^{}]*)\s*\}\})(?P<post>x(?![A-Za-z])|%|\s?(?:million|billion|"
                         r"trillion|mn|bn|tn)\b)?", re.I)


def _drop_extra_units(text: str, by_id: dict[str, "Fact"]) -> str:
    def drop(m: re.Match) -> str:
        if not (m.group("pre") or m.group("post")):
            return m.group(0)
        fact = find(by_id, m.group("id"))
        if m.group("id") and fact is None and not FACT_ID.fullmatch(m.group("id")):
            return m.group(0)       # the model's placeholder: its filler drops units itself
        # A computed share or change is a percentage or a multiple, and prints its unit; a
        # fact keeps a typed unit only when it is a bare number.
        if fact is not None and fact.figure.kind == "bare":
            return m.group(0)
        return m.group("ref")
    return EXTRA_UNITS.sub(drop, text)


UNIT_AFTER = r"(mn|bn|tn|million|billion|trillion|k|%|x|pp|bps)\b"


def _place_sources(text: str) -> str:
    """Each source once per sentence, after the last figure that came from it, and after
    the unit the writer put after the id: "USD {{F97}}mn" reads "USD 16,121mn (market
    data)", not "16,121 (market data)mn"."""
    text = re.sub(r"(\x00[^\x00]*\x00)" + UNIT_AFTER, r"\2\1", text)
    text = re.sub(re.escape(OWN_MARK) + UNIT_AFTER, lambda m: m.group(1) + OWN_MARK, text)
    parts = re.split(f"({SENTENCE_END.pattern})", text)
    for i in range(0, len(parts), 2):
        last = {}
        for mk in MARKED.finditer(parts[i]):
            last[mk.group(1)] = mk.start()
        keep = set(last.values())
        parts[i] = MARKED.sub(lambda mk: f" ({mk.group(1)})" if mk.start() in keep else "",
                              parts[i])
    return "".join(parts)


def typed(text: str) -> list[tuple[Figure, str]]:
    """Every figure the writer typed rather than cited: the figures left once the
    references are taken out, each with its sentence."""
    bare = ANY_PLACEHOLDER.sub("", text)
    out = []
    for sentence in SENTENCE_END.split(bare):
        for f in figures(sentence):
            out.append((f, sentence))
    return out


def assumptions_from(headline: dict) -> tuple[dict[str, Assumption], list[str]]:
    """The writer's own estimates, declared in the JSON header as
    [{"id": "leverage_target", "value": "2.0x", "basis": "..."}]."""
    raw = headline.get("assumptions")
    if raw is None:
        return {}, []
    if not isinstance(raw, list):
        return {}, ["`assumptions` must be a list of {id, value, basis}"]
    out, problems = {}, []
    for item in raw:
        if not isinstance(item, dict) or not item.get("id"):
            problems.append(f"an assumption needs an `id`, a `value` and a `basis`: {item!r:.80}")
            continue
        value = str(item.get("value", "")).strip()
        found = figures(value) or _bare(value)
        # One figure, or a range of two: SPGI's writer kept declaring "2-3%".
        is_range = len(found) == 2 and bool(re.fullmatch(
            r"\s*(?:-|–|—|to)\s*", value[found[0].end:found[1].start]))
        if len(found) != 1 and not is_range:
            problems.append(f"assumption `{item['id']}` must have one figure or a range as its "
                            f"value, like \"2.0x\" or \"2-3%\"; it has {item.get('value')!r}")
            continue
        if not str(item.get("basis") or "").strip():
            problems.append(f"assumption `{item['id']}` has no `basis`: say why you chose it")
            continue
        text = value[found[0].start:found[-1].end]
        out[str(item["id"])] = Assumption(str(item["id"]), found[0], str(item["basis"]).strip(),
                                          text)
    return out, problems


# A basis that names a document or quotes one: the figure is a fact about the world, not
# the writer's estimate.
SOURCED_BASIS = re.compile(r"[\"“]\s*\w+(?:\W+\w+){3,}|\b(?:10-K|10-Q|8-K|20-F|earnings release|"
                           r"press release|annual report|investor day|transcript|filing|"
                           r"guided|guidance|disclos\w*|reported|according to|"
                           r"per (?:the|its|management))\b|"
                           # Third-party data: someone else's figure, not the writer's.
                           r"\b(?:IMF|IIF|OECD|World Bank|BIS|SIFMA|ICMA|Preqin|Bloomberg|"
                           r"Morningstar|Gartner|IDC|Statista|McKinsey|Nielsen|industry data|"
                           r"survey)\b", re.I)
# A basis that gives no reason: "estimate", "my assumption", "judgement".
EMPTY_BASIS = {"estimate", "estimated", "assumption", "assumed", "judgement", "judgment",
               "view", "analyst", "own", "base", "case", "model", "modelled", "modeled"}
# Model inputs an assumption may name and then must agree with, by unambiguous phrases.
MODEL_INPUT_WORDS = {"g": ["terminal growth", "perpetuity growth", "perpetual growth"],
                     "wacc": ["wacc", "discount rate", "cost of capital"],
                     "exit_multiple": ["exit multiple"], "tax_rate": ["tax rate"],
                     "new_capital_return": ["new capital return", "return on new capital"]}
ASSUMPTION_PROBLEM_END = "; in the final draft these sentences are deleted"


def _words_of(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", text.lower().replace("_", " "))}


def reconcile_assumptions(assumptions: dict[str, Assumption], fx: "Facts",
                          values: dict[str, str] | None = None
                          ) -> tuple[dict[str, Assumption], dict[str, str], list[str]]:
    """The writer's own assumptions, kept only where they are valid. SPGI's rewrite of
    2026-10-03 declared the 2Q26 adjusted margin (54.3%) and the buyback guidance (>USD 7bn)
    as its own assumptions, quoting the earnings release as the basis, and 42 figures
    printed "(my assumption)".

    - One that is a fact in the database, by a value written to 3 digits or a label it
      shares words with, becomes that fact: returned as {assumption id: "F12"}, to be
      cited in its place.
    - One that names a model input ("terminal growth") becomes the model's placeholder
      when it agrees, and is refused when it does not.
    - One whose basis names a source, or gives no reason, is refused.

    A refused one is a problem for the rewrite, and is left out, so its sentences print
    nothing and are taken out of the final draft."""
    kept, cite_as, problems = {}, {}, []
    for key, a in assumptions.items():
        about = f"{a.id} {a.basis}"
        same = [f for f in fx.by_id.values()
                if f.figure.kind == a.figure.kind and a.figure.kind != "bare"
                and abs(f.figure.value - a.figure.value) <= max(f.figure.slack, a.figure.slack)]
        words = _words_of(about)
        scored = sorted(same, key=lambda f: -len(words & _words_of(f"{f.metric} {f.period} {f.table}")))
        overlap = [len(words & _words_of(f"{f.metric} {f.period} {f.table}")) for f in scored]
        ranged = "-" in a.text or "–" in a.text
        if scored and not ranged and (
                (overlap[0] >= 2 and (len(scored) == 1 or overlap[0] > overlap[1]))
                # "54.3%" is one figure; "5%" could be any of a dozen.
                or (len(scored) == 1 and _significant(a.figure.text) >= 3)):
            cite_as[key] = scored[0].id
            continue
        def named(text: str) -> list[str]:
            low = text.lower().replace("_", " ")
            return [k for k, phrases in MODEL_INPUT_WORDS.items()
                    if k in (values or {}) and any(p in low for p in phrases)]

        def agrees(model_key: str) -> bool:
            mine = figures(values[model_key])
            # Strictly within rounding: a declared "2%" is not the model's 2.5%.
            return bool(mine and mine[0].kind == a.figure.kind and abs(
                a.figure.value - mine[0].value) < max(a.figure.slack, mine[0].slack))

        # What the assumption is comes from its id; its basis may name another input as a
        # yardstick. SPOT's rewrite of 2026-10-07 set new_capital_return to 18% "approx 1.6x
        # WACC" and was refused as a wrong WACC, which cost the draft the reviewer's fixes.
        own, compared = named(a.id), named(a.basis)
        if own and not ranged:
            if agrees(own[0]):
                cite_as[key] = own[0]
                continue
            problems.append(f"assumption `{key}` ({a.text}) is the model's input, which is "
                            f"{values[own[0]]}: cite {{{{{own[0]}}}}} instead, or change "
                            "the model" + ASSUMPTION_PROBLEM_END)
            continue
        match = next((k for k in compared if agrees(k)), None)
        if match and not ranged:
            cite_as[key] = match
            continue
        source = SOURCED_BASIS.search(a.basis)
        if source:
            # NOK's of 2026-10-04 refused a 3-4 year AI cycle because its basis reasoned
            # from management's guidance: naming a source is not the figure being the
            # source's. The company's own figure is cited as its sentence; one the notes
            # give otherwise, or the basis credits, is someone else's.
            group = figures(a.text) or _bare(a.text)
            said = _Said(_words(f"{a.id.replace('_', ' ')} {a.basis}"), years=_years(a.basis),
                         exact=False)
            stated = (_stated(group, said, fx.statements, _index(fx.statements),
                              _ranges(fx.statements)) if fx.statements else None)
            if stated:
                for fact in stated:
                    fx.by_id.setdefault(fact.id, fact)
                cite_as[key] = "}}-{{".join(f.id for f in stated)
                continue
            why = _sourced(a, group, said, fx)
            if why:
                problems.append(f"assumption `{key}` ({a.text}) credits a source (\""
                                f"{source.group(0).strip()}\") {why}: a figure from a source is "
                                "not your assumption; cite it from the fact database, or say in "
                                "words that the database does not give it" + ASSUMPTION_PROBLEM_END)
                continue
        reason = {w for w in _words_of(re.sub(r"\d[\d.,]*%?", " ", a.basis))} - EMPTY_BASIS
        if len(reason) < 3:
            problems.append(f"assumption `{key}` ({a.text}) gives no reason as its basis "
                            f"({a.basis!r}): say what the figure rests on, in a sentence"
                            + ASSUMPTION_PROBLEM_END)
            continue
        kept[key] = a
    return kept, cite_as, problems


# "not in fact database", "not disclosed in sources reviewed": a hedge, not a reason.
HEDGE = re.compile(r"\b(?:not|no)\b[^;,.]*?\b(?:in|disclosed|available|given|found|reported|"
                   r"provided)\b[^;,.]*", re.I)
# The words of its own a basis that names a source needs. Across the saved pitches, every
# basis that was a citation ("Company disclosure; not in fact database", "IMF/IIF
# estimates", "2Q26 earnings release: '...'") had at most eight; NOK's reasoned ones had
# 15 to 22.
SOURCED_REASON_WORDS = 10


def _sourced(a: Assumption, group: list[Figure], said: "_Said", fx: "Facts") -> str:
    """Why an assumption whose basis names a source is that source's figure, or "" when
    the basis reasons from the source to a figure of its own: NOK's 3-4 year AI cycle
    rested on "hyperscaler capex guidance and order visibility extend to 2028; beyond
    that, replacement cycles and inference infrastructure create a second wave". The
    notes giving its value make it someone else's, and so does a basis that is a citation
    and nothing more: SPGI's rewrite of 2026-10-02 declared "100%" net retention on
    "Company disclosure, 2025 10-K"."""
    notes = _statements(fx.digest, "research notes", fx.fy_end_month)
    if notes and _stated(group, said, notes, _index(notes), _ranges(notes)):
        return "and the research notes give it"
    rest = SOURCED_BASIS.sub(" ", HEDGE.sub(" ", TAGGED.sub("", ANY_PLACEHOLDER.sub(" ", a.basis))))
    rest = re.sub(r"\([^()]*\)|\d[\d.,]*%?", " ", rest)
    if len(_words_of(rest) - EMPTY_BASIS) < SOURCED_REASON_WORDS:
        return "and gives no reason of its own beyond it"
    return ""


def _equal(f: Figure, g: Figure) -> bool:
    """The same figure, a bare one read as millions against an amount: a declared "7000"
    is "$7 billion"."""
    if _same(f, g):
        return True
    bare, money = (f, g) if f.kind == "bare" else (g, f)
    return (bare.kind == "bare" and money.kind == "money"
            and abs(money.value - bare.value * 1e6) <= max(money.slack, bare.slack * 1e6))


def cite_instead(text: str, cite_as: dict[str, str]) -> str:
    """{{assume:id}} becomes {{F12}} or {{g}} for an assumption that is a fact or a model
    input."""
    if not cite_as:
        return text
    return re.sub(r"\{\{\s*assume\s*:\s*([\w.-]+)\s*\}\}",
                  lambda m: "{{" + cite_as[m.group(1)] + "}}" if m.group(1) in cite_as
                  else m.group(0), text)


def latest_reported(fx: "Facts") -> str:
    """The end of the latest period the database reports ("2026-06"), or ""."""
    ends = [f.period_key[1] for f in fx.by_id.values()
            if f.period_key and f.data_type in ("reported", "market")]
    return max(ends, default="")


def historical_assumptions(text: str, fx: "Facts") -> list[tuple[str, str]]:
    """Sentences that give an assumption as the figure for a period already reported, as
    (sentence, why): "{{assume:margin}} adjusted operating margin in 2Q26" is a claim about
    the past, which only a fact can make. A sentence that also names a later period is
    about the forecast and stays."""
    latest = latest_reported(fx)
    if not latest:
        return []
    out = []
    for sentence in SENTENCE_END.split(text):
        refs = list(re.finditer(r"\{\{\s*assume\s*:[^{}]*\}\}", sentence, re.I))
        if not refs:
            continue
        # NOK's of 2026-10-04 lost "margin moves from the Q2 2026 level toward the
        # {{assume:target}} target": the past period was where the path starts.
        if all(_heading_to(sentence, m) for m in refs):
            continue
        # "a normalization from the 2Q26 cyclical peak and the {{assume:x}} downside":
        # the reported period is where the scenario starts (SPGI, 2026-10-05).
        named = ORIGIN_PERIOD.sub(" ", ANY_PLACEHOLDER.sub(" ", _assumption_clauses(sentence)))
        periods = [p for p in mentioned_periods(named, fx.fy_end_month) if p[0] != "at"]
        if periods and all(p[1] <= latest for p in periods):
            out.append((sentence, "an assumption given as the figure for a period already "
                        "reported: the past is a fact, so cite it from the fact database"))
    return out


ASSUME_REF = re.compile(r"\{\{\s*assume\s*:", re.I)


def _assumption_clauses(sentence: str) -> str:
    """The sentence without the brackets and clauses that hold another figure's citation
    and no assumption: a period there dates that figure. ESLT's of 2026-10-07 lost
    "investing at a {{assume:capex_intensity}} capex intensity (FY2025 was
    {{capex_pct_revenue_FY2025}})", whose FY2025 is the fact's."""
    def other(text: str) -> bool:
        return bool(ANY_PLACEHOLDER.search(text)) and not ASSUME_REF.search(text)

    sentence = re.sub(r"\([^()]*\)", lambda m: " " if other(m.group(0)) else m.group(0), sentence)
    return " ".join(p for p in re.split(r"[;,]", sentence) if not other(p))


# A reported period named as a starting point or a comparison, not as the period a figure
# is for: "from the 2Q26 peak", "versus 1H25".
ORIGIN_PERIOD = re.compile(
    r"\b(?:from|since|off|versus|vs\.?|than|compared (?:with|to)|relative to)\s+"
    r"(?:the\s+|its\s+|a\s+)?(?:[1-4]Q\s?'?\d{2}|Q[1-4]\s?'?\d{2,4}|[12]H\s?'?\d{2}|"
    r"H[12]\s?'?\d{2,4}|FY\s?'?\d{2,4}|20\d{2})\b", re.I)


# Words that make the figure beside them a destination, not a period's result.
HEADING_TO = re.compile(r"\b(?:towards?|targets?|targeting|goal|reach\w*|medium[- ]term|"
                        r"long[- ]term|over time|going forward|will|expect\w*|guid\w*|aim\w*|"
                        r"(?:rise|rises|fall|falls|expand\w*|improv\w*|recover\w*|return\w*|"
                        r"climb\w*) to)\b", re.I)


def _heading_to(sentence: str, ref: re.Match) -> bool:
    """The assumption is where something is heading: a destination word in its clause
    before it, or "target" right after it ("toward the {{assume:x}} target")."""
    before = sentence[:ref.start()]
    clause = before[max(before.rfind(";"), before.rfind(","), len(before) - 80, 0):]
    return bool(HEADING_TO.search(clause)
                or re.match(r"\s*(?:\(my assumption\)\s*)?(?:\w+\s+){0,2}?(?:target|goal)\b",
                            sentence[ref.end():], re.I))


BOUND_BEFORE = re.compile(r"(?:[~<>≈≥≤]|\bc\.|\babout|\bover)\s*$")
LABEL = re.compile(r"^(\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*[^*]+\*\*:?\s*)?)")
EMPTY = re.compile(r"^\s*(?:[-*+]|\d+[.)])?\s*(?:\*\*[^*]*\*\*:?)?\s*$")


def _masked(text: str) -> str:
    """The text with every placeholder blanked out, same length, so figure positions hold."""
    return ANY_PLACEHOLDER.sub(lambda m: "\x01" * len(m.group(0)), text)


# The writer stating its own view: "We assume 3-4% issuance growth", "I model D&A at 7%".
OWN_VIEW = re.compile(r"\b(?:I|we|my|our)\b[^.;]{0,60}?\b(?:assum|model|project|forecast|"
                      r"estimat|expect|pencil|haircut|taper|underwrit)\w*|"
                      # The Risks section's prescribed lines are the writer's scenario
                      # inputs and thresholds: "Model impact: MI growth drops 3pp".
                      r"^\W*(?:\*\*)?(?:model impact|signpost|downside case)\b|"
                      r"\bwould (?:move|cut|upgrade|downgrade|turn)\b|"
                      # The writer's own thresholds for changing the call: "Upgrade to HOLD
                      # if organic growth sustains >7%" (SPGI, 2026-10-03).
                      r"\b(?:upgrade|downgrade)\b[^.;]{0,80}?\bif\b|"
                      r"\b(?:recommendation change )?triggers?\b\s*:|"
                      # A test's threshold and a scenario's input are the writer's: GS's
                      # "if the annualized credit cost ratio exceeds 40bps" and "the bear
                      # case models 80-100bps" of 2026-10-13 were marked unverified.
                      r"\bif\b[^.;]{0,80}?\b(?:exceeds?|surpass\w*|tops?|falls? below|"
                      r"drops? below|stays? (?:below|above)|rises? above|reach\w*|"
                      r"(?:is|are|stays?|remains?) (?:below|above|under|over))\b|"
                      r"\b(?:bear|bull|base|downside|upside|stress) case\b[^.;]{0,30}?"
                      r"\b(?:model|assum|cut|take|appl|use|see)\w*", re.I)
# Sources named only as where to look next, or as what the writer disagrees with, do not
# make the writer's figure a sourced one: "in the 3Q26 10-Q", "versus consensus".
NOT_A_SOURCE = re.compile(r"\b(?:in|from|at) the (?:next |coming |upcoming |\d+Q\d+ |FY\d+ )?"
                          r"(?:10-K|10-Q|8-K|20-F)\b|\b(?:versus|vs\.?|than|against) "
                          r"(?:the )?(?:consensus|analysts?)\b", re.I)
# A source named in the sentence: a figure credited to one is a fact about the world, and
# "we assume" does not make it the writer's. NFLX's second write of 2026-10-01 relabelled
# "25% of watch time (Nielsen, 2025)" as "my estimate of 25%".
SOURCED = re.compile(r"\([^()]*\b(?:19|20)\d{2}\b[^()]*\)|\b(?:10-K|10-Q|8-K|20-F|investor day|"
                     r"disclos\w*|according to|per (?:the|its|management)|reported|SIFMA|ICMA|"
                     r"Preqin|Bloomberg|Morningstar|Gartner|IDC|consensus|analysts?|survey|"
                     r"industry data|company data|guidance)\b", re.I)
OWN_MARK = " (my assumption)"
WRITER_LABEL = re.compile(r"\((?:my|our) (?:estimate|assumption)s?\)", re.I)


def _bare(value: str) -> list[Figure]:
    """"13500" or "185-195": a figure without a unit, as the writer declares a model
    amount in millions; one or a range, else nothing."""
    out = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", value):
        num = m.group(0)
        decimals = len(num.split(".")[1]) if "." in num else 0
        out.append(Figure("bare", float(num.replace(",", "")), 0.5 * 10 ** -decimals, num,
                          m.start(), m.end()))
    rest = re.sub(r"\d[\d,]*(?:\.\d+)?|\s|-|–|to", "", value)
    return out if out and len(out) <= 2 and not rest else []


def own_view(sentence: str) -> bool:
    """A sentence stating the writer's own estimate, with no source named for it."""
    bare = ANY_PLACEHOLDER.sub(" ", sentence)
    return bool(OWN_VIEW.search(bare)) and not SOURCED.search(NOT_A_SOURCE.sub(" ", bare))


def contradicts_model(sentence: str, values: dict[str, str] | None, fy_end: int = 12) -> bool:
    """A typed figure the sentence pins to one of the model's values, by a phrase specific
    to it or by the forecast year it names, that is not that value. SPGI's rewrite of
    2026-10-03 kept "my model embeds 5% terminal growth" as the writer's own estimate; the
    model's was 3%."""
    if not values:
        return False
    masked = _masked(sentence)
    periods = [p for p in mentioned_periods(masked, fy_end) if p[0] != "at"]
    model = _model_candidates(masked, values, fy_end, sentence)
    for f in figures(masked):
        # "growth" alone names any segment's growth as well as the company's.
        pinned = [m for m in model if m[1].kind == f.kind and (
            m[0].split(":")[0] in SPECIFIC_MODEL or (
                m[2] is not None and m[2] in periods
                and (m[0].split(":")[0] != "revenue_growth" or "revenue growth" in masked.lower())))]
        if pinned and not any(_same(f, m[1]) for m in pinned):
            return True
    return False


def mark_own(text: str, values: dict[str, str] | None = None,
             fy_end: int = 12) -> tuple[str, list[str]]:
    """Sentences that state the writer's own estimates in typed figures keep them, marked
    "(my assumption)" at the end; the analysis stays and reads as the writer's. One whose
    figure contradicts the model (`values`) is not the writer's to keep, and is left to be
    taken out. Returns the text and each sentence marked."""
    marked = []
    parts = re.split(f"({SENTENCE_END.pattern})", text)
    for i in range(0, len(parts), 2):
        part = parts[i]
        if (not part.strip() or part.lstrip().startswith(("#", "|"))
                or not figures(_masked(part)) or not own_view(part)
                or contradicts_model(part, values, fy_end)):
            continue
        marked.append(" ".join(part.split()))
        # The writer's own label stands for ours: "(my estimate)" becomes "(my assumption)"
        # rather than gaining a second one.
        if WRITER_LABEL.search(part) or OWN_MARK.strip() in part:
            parts[i] = WRITER_LABEL.sub(OWN_MARK.strip(), part)
            continue
        body = part.rstrip()
        end = len(body) - len(body.rstrip(".!?"))
        parts[i] = body[:len(body) - end] + OWN_MARK + body[len(body) - end:] + part[len(body):]
    return "".join(parts), marked


def strip_typed(text: str, preserve: bool = False) -> tuple[str, list[tuple[str, list[str]]]]:
    """The text without its typed figures, and what went: a sentence with one is deleted
    (its bold label stays, unless the figure is in it), a heading loses only the figure.
    Returns the text and
    (sentence, [figures]) for each deletion. With preserve=True, unmatched figures remain
    visible with a dagger for human verification: lack of a citation is not evidence that
    a figure is false."""
    removed: list[tuple[str, list[str]]] = []
    parts = re.split(f"({SENTENCE_END.pattern})", text)
    original = list(parts)
    for i in range(0, len(parts), 2):
        part = parts[i]
        if OWN_MARK.strip() in part:
            continue        # the writer's own estimate, marked as one (mark_own)
        found = figures(_masked(part))
        if not found:
            continue
        if part.lstrip().startswith("|"):
            # A typed table row goes without comment: tables are the code's, and the ones
            # under Model Outputs are replaced anyway.
            parts[i] = ""
            continue
        removed.append((part.strip(), [f.text for f in found]))
        if preserve:
            for f in sorted(found, key=lambda f: f.end, reverse=True):
                if part[f.end:f.end + 1] != "†":
                    part = part[:f.end] + "†" + part[f.end:]
            parts[i] = part
            continue
        if part.lstrip().startswith("#"):
            for f in sorted(found, key=lambda f: f.start, reverse=True):
                lo = len(BOUND_BEFORE.sub("", part[:f.start]))
                hi = f.end + len(re.match(r"\+?", part[f.end:]).group(0))
                # A stripped title such as "Backlog of 14bn drives growth" used to become
                # "Backlog of drives growth". The number's leading "of" belongs to it.
                prefix = part[:lo]
                # So do "to", "at" and "by", and a "CAGR" after it: SPOT's "margin expands
                # from current levels to 38%+ as ..." of 2026-10-11 printed "to as".
                prefix = re.sub(r"\b(?:of|to|at|by|near|around|about|reaching)\s*$", "",
                                prefix, flags=re.I)
                lo = len(prefix)
                tail = re.match(r"\s*(?:CAGR|compound(?:ed)? annual growth(?: rate)?)\b",
                                part[hi:], re.I)
                hi += tail.end() if tail else 0
                part = part[:lo] + part[hi:]
            # "Market Intelligence (37% of revenue)" left "( of revenue)": a bracket the
            # figure was the point of goes with it.
            part = re.sub(r"\s*\(\s*(?:of\s+[\w\s-]{0,30}?)?\s*\)", "", part)
            parts[i] = re.sub(r"[ \t]{2,}", " ", part).rstrip()
        else:
            label = LABEL.match(part).group(1)
            # A bold lead that carries a typed figure is a claim, not a label: SPOT's
            # "**FCF margin sustains ~22%: ...**" of 2026-10-07 stayed when its sentence went,
            # and printed 22% against a model at 15%.
            if any(f.start < len(label) for f in found):
                label = re.match(r"\s*(?:[-*+]\s+|\d+[.)]\s+)?", part).group(0)
            parts[i] = label
    if not preserve:
        _mend(parts, original, removed)
    before = set(text.split("\n"))
    lines = [l if l in before else _tidy(l) for l in "".join(parts).split("\n")
             if not (EMPTY.match(l) and l.strip() and l not in before)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)), removed


# A sentence that leans on the one before it, and reads as nonsense once that one is gone:
# ESLT's thesis of 2026-10-07 opened "This provides exceptional revenue visibility." after
# its backlog sentence was deleted.
LEANED_WHY = "it referred to the sentence before it, which was deleted"
LEANS_BACK = re.compile(r"^\s*(?:This|These|That|Those|Such|It|They)\b(?!\s*\*\*)")


def _mend(parts: list[str], original: list[str], removed: list) -> None:
    """Repair what deleting sentences left in `parts` (sentences at even indices, the
    breaks between them at odd ones; `original` before deletion). A sentence in the same
    paragraph that leans on a deleted one goes with it, listed with no figures; a ";" that
    now joins two sentences becomes a full stop: ESLT's "share of procurement; Over-reliance
    on IMOD" lost the clause in between."""
    gone = [i for i in range(0, len(parts), 2)
            if parts[i].strip() != original[i].strip() and not original[i].lstrip().startswith(("#", "|"))]
    i = 0
    while i < len(gone):
        j = gone[i] + 2
        if (j < len(parts) and j not in gone and "\n" not in parts[j - 1]
                and LEANS_BACK.match(parts[j]) and not LABEL.match(parts[j]).group(1).strip()):
            removed.append((parts[j].strip(), []))
            parts[j] = ""
            gone.insert(i + 1, j)
        i += 1
    for i in gone:
        if not original[i].rstrip().endswith((".", "!", "?")):
            continue
        k = i - 2
        while k >= 0 and k in gone and not parts[k].strip():
            k -= 2
        nxt = i + 2
        while nxt < len(parts) and nxt in gone and not parts[nxt].strip():
            nxt += 2
        if (k >= 0 and parts[k].rstrip().endswith(";") and "\n" not in "".join(parts[k + 1:nxt])
                and nxt < len(parts) and re.match(r"\s*[A-Z*]", parts[nxt])):
            parts[k] = re.sub(r";(\s*)$", r".\1", parts[k])


def _tidy(line: str) -> str:
    """A line a deletion changed, without the spaces the deleted sentences stood between."""
    return re.sub(r"(?<=\S)[ \t]{2,}", " ", re.sub(r"^[ \t]+(?=[^\s*+\d-])", "", line)).rstrip()


def drop_sentence(text: str, sentence: str, keep_label: bool = True) -> tuple[str, list[str]]:
    """`text` without `sentence` (its bold label stays unless `keep_label` is false),
    mended as `_mend` does, and the sentences that leaned on it, which went too."""
    label = LABEL.match(sentence) if keep_label else None
    cut = sentence[label.end():] if label and label.group(1).strip() else sentence
    at = text.find(cut) if cut.strip() else -1
    if at < 0:
        return text, []
    original = re.split(f"({SENTENCE_END.pattern})", text)
    parts, pos, hit = list(original), 0, False
    for i, p in enumerate(original):
        lo, hi = max(pos, at), min(pos + len(p), at + len(cut))
        if i % 2 == 0 and hi > lo:
            # The part without the cut's share of it: whole sentences, as the splitter
            # found them, or a clause the cut began or ended inside.
            parts[i] = p[:lo - pos] + p[hi - pos:]
            hit = True
        pos += len(p)
    if not hit:
        return text, []
    removed: list = []
    _mend(parts, original, removed)
    before = set(text.split("\n"))
    return ("\n".join(l if l in before else _tidy(l) for l in "".join(parts).split("\n")),
            [s for s, _ in removed])


# The notes' own tag, copied after a figure instead of citing it: "$1,339mn [#F180]",
# "6-8% [#F30-#F31]", "[#F174, #F175]". SPGI's facts-mode rewrite of 2026-10-02 wrote
# 20 to 36 citations this way.
INLINE_TAG = re.compile(r"\s*\[#(?P<ids>F\d+(?:\s*[-–,/]\s*#?F\d+)*)\]")


def adopt_tags(text: str, fx: "Facts") -> str:
    """Copied tags become citations where the figures before them are the tagged facts'
    values; a tag whose figures do not match is dropped and the figures stay typed."""
    def swap(sentence: str) -> str:
        out, last = [], 0
        for m in INLINE_TAG.finditer(sentence):
            ids = re.findall(r"F\d+", m.group("ids"))
            before = sentence[last:m.start()]
            found = figures(_masked(before))[-len(ids):] if ids else []
            facts_ = [find(fx.by_id, i) for i in ids]
            if (len(found) == len(ids) and all(facts_)
                    and all(_same(f, fact.figure) for f, fact in zip(found, facts_))):
                for f, fact in sorted(zip(found, facts_), key=lambda p: p[0].start, reverse=True):
                    before = before[:f.start] + "{{" + fact.id + "}}" + before[f.end:]
            out.append(before)
            last = m.end()
        out.append(sentence[last:])
        return "".join(out)

    parts = re.split(f"({SENTENCE_END.pattern})", text)
    return "".join(swap(p) if i % 2 == 0 else p for i, p in enumerate(parts))


def _same(typed: Figure, fact: Figure) -> bool:
    """The same figure, written as precisely as the source or rounded from it."""
    # A bare number may be a count or an amount written without its unit ("13500"), but
    # never a percentage or a multiple: "1%" is not a beta of 1.05.
    loose = {"bare", "count", "money"}
    if typed.kind != fact.kind and not ({typed.kind, fact.kind} <= loose
                                        and "bare" in (typed.kind, fact.kind)):
        return False
    slack = max(typed.slack, fact.slack, abs(fact.value) * 1e-9)
    return abs(typed.value - fact.value) <= slack


def suggestion(text: str, fx: "Facts | None", values: dict[str, str] | None,
               sentence: str = "") -> str:
    """Where a typed figure already is: an id in the notes, a placeholder of the model or
    the company's own sentence, measuring what the words around the figure say it does.
    The same value alone is no evidence: ESLT's writer of 2026-10-07 was told its "2% of
    GDP" was {{da_pct_revenue_FY2025}} and its 10.4% non-GAAP margin was
    {{bull_revenue_change:FY2026}}, and then cited D&A's share of revenue as its R&D."""
    found = figures(text)
    if not found:
        return ""
    f = found[0]
    at = _masked(sentence).find(text) if sentence else -1
    if at < 0:
        return ""
    masked = _masked(sentence)
    window = masked[max(0, at - LEAD_CHARS):at + len(text) + 30]
    said = {t for _, t in metric_terms(window)}
    near = _near(masked, at, at + len(text))
    if fx:
        same = [fact for fact in fx.by_id.values() if _same(f, fact.figure)
                and (cited_measures(fact.id, fx) & said
                     or (fact.metric and _metric_named(fact.metric, window)))]
        # The figure sheet's names first: they are what the writer was shown.
        named = [x.id for x in same if not FACT_ID.fullmatch(x.id)][:3]
        if named:
            return " (on the figure sheet as " + ", ".join("{{" + i + "}}" for i in named) + ")"
        if same and not fx.sheet:
            return (" (in the fact database as "
                    + ", ".join("{{" + x.id + "}}" for x in same[:3]) + ")")
        # The company's sentence the writer was nearly quoting: two of its specific words
        # beside the figure, one of them saying what is measured (`_stated` wants three).
        index = _index(fx.statements) if fx.statements else {}
        stated = [s for s in fx.statements if _same(f, s.figure)
                  and len(near & index[s.context][0]) >= 2 and near & index[s.context][1]]
        if stated:
            return (f" (the company states it: \"{stated[0].context[:120]}\"; write it with "
                    "the company's words around it)")
    fy_end = fx.fy_end_month if fx else 12
    for key, figure, _ in _model_candidates(window, values or {}, fy_end, sentence):
        if _same(f, figure):
            return " (your model has it as {{" + key + "}})"
    return ""


def describe_typed(removed: list[tuple[str, list[str]]], limit: int = 12,
                   fx: "Facts | None" = None, values: dict[str, str] | None = None) -> list[str]:
    """The rewrite request for typed figures, each with where it already is, if anywhere."""
    removed = [(s, figs) for s, figs in removed if figs]    # not the sentences that leaned
    if not removed:
        return []
    quoted = "; ".join(", ".join(repr(f) + suggestion(f, fx, values, s) for f in figs)
                       + f" in \"{' '.join(s.split())[:110]}\""
                       for s, figs in removed[:limit])
    more = f" (and {len(removed) - limit} more sentences)" if len(removed) > limit else ""
    return [f"these figures are typed, not cited: {quoted}{more}. Cite each one by its id from "
            "the fact database ({{F12}}), compute it from two ids ({{share:F2/F1}}), cite a model "
            "placeholder, or declare it under `assumptions` and cite {{assume:id}}; a figure the "
            "company states in a PRIMARY note, write as it does, with its words around it; if "
            "none of these gives it, say so in words. Unmatched figures are retained with † "
            "for verification; a missing citation alone is not proof the figure is false"]


# ------------------------------------------------------------------ fact sheet and matching

STOP_WORDS = {"total", "net", "and", "the", "for", "from", "with", "other", "less", "inc",
              "attributable", "company", "value", "line", "item", "reporting", "currency"}
# The model's computed values a sentence may name in words, by placeholder.
MODEL_WORDS = {
    "revenue": ["revenue", "sales"], "ebit": ["ebit", "operating income", "operating profit"],
    "ebitda": ["ebitda"], "ebit_margin": ["ebit margin", "operating margin"],
    "fcf": ["free cash flow", "fcf"], "fcf_margin": ["fcf margin", "free cash flow margin"],
    "wacc": ["wacc", "discount rate", "cost of capital"],
    "g": ["terminal growth", "perpetuity", "perpetual growth", "growth forever"],
    "da_pct_revenue": ["d&a", "depreciation"], "capex_pct_revenue": ["capex", "capital expenditure"],
    "tax_rate": ["tax rate", "tax"], "nwc_pct_revenue_change": ["working capital"],
    "revenue_growth": ["revenue growth", "grows", "growth"],
    "exit_multiple": ["exit multiple", "exit"], "tv_share": ["terminal value"],
    "ev_ebitda": ["trailing ebitda", "trades at", "x trailing"], "peer_ev_ebitda": ["peer median", "peers"],
    "model_cagr": ["cagr"],
    "implied_cagr": ["implied", "price assumes", "priced", "market prices", "justify",
                     "pays for"],
    "tv_ev_ebitda": ["terminal", "exit"], "wacc_premium": ["premium"],
    "beta": ["beta"], "risk_free": ["risk-free", "treasury"], "cost_of_equity": ["cost of equity"],
    "upside": ["upside"], "intrinsic": ["intrinsic", "dcf", "per share", "value"],
    "downside": ["downside"],
    "roic": ["roic", "return on invested capital", "return on capital", "returns on capital"],
    "roic_ex_goodwill": ["goodwill"], "invested_capital": ["invested capital"],
    "terminal_roic": ["new capital", "incremental capital"],
    "new_capital_return": ["new capital", "incremental capital"],
    "terminal_reinvestment": ["reinvest"],
    "net_debt": ["net debt", "net cash"], "net_cash": ["net cash"], "net_debt_to_ebitda": ["net debt", "net cash", "leverage"],
    "interest_coverage": ["interest cover", "coverage", "covered"],
    "interest_expense": ["interest expense", "interest cost"],
    "cost_of_debt": ["cost of debt", "borrowing cost"], "earnings_yield": ["earnings yield"],
    "buyback_eps_change": ["eps", "earnings per share", "accreti", "diluti"],
    "minority_interest": ["minority", "non-controlling", "noncontrolling"],
    # The bad case's inputs, named only in a sentence about the bad case (DOWNSIDE_WORDS).
    "downside_revenue_cut": ["revenue"], "downside_revenue": ["revenue"],
    "downside_ebit_margin": ["margin"], "downside_g": ["terminal growth", "perpetuity"],
    "downside_exit_multiple": ["exit", "multiple"], "downside_wacc": ["wacc", "discount rate"],
    # The excess-returns route (banks, insurers, utilities, REITs): GS's pitch of
    # 2026-10-13 typed its model's 15% terminal ROE, 1.15% NIM and 55bps credit cost and
    # had each marked unverified.
    "terminal_roe": ["terminal roe", "terminal return", "roe sustained", "sustained roe",
                     "through-cycle roe", "long-run roe"],
    "ke": ["cost of equity"], "g": ["terminal growth", "perpetuity", "perpetual growth",
                                     "growth forever", "sustained forever", "forever with"],
    "implied_roe": ["implied", "price assumes", "price implies", "prices a", "priced",
                    "market prices", "sustainable roe"],
    "implied_ke": ["implied cost of equity"],
    "roe": ["roe", "return on equity"], "rote": ["rote", "return on tangible"],
    "nim": ["nim", "net interest margin"], "credit_cost": ["credit cost", "provision"],
    "efficiency_ratio": ["efficiency"], "combined_ratio": ["combined ratio"],
    "non_interest_income": ["non-interest", "noninterest", "fee income"],
    "nii": ["net interest income", "nii"], "cet1_ratio": ["cet1"],
    "payout": ["payout"], "terminal_payout": ["payout"],
    "earning_assets": ["earning assets"], "loans": ["loans"],
    "pb": ["book value", "price to book", "p/b", "x book"],
    "ptbv": ["tangible book", "p/tbv", "price to tangible"],
    "implied_pb": ["implied", "book"], "implied_ptbv": ["implied", "tangible book"],
    "book_per_share": ["book value per share", "book per share"],
    "tangible_book_per_share": ["tangible book value per share", "tangible book per share"],
    "peer_pe": ["peer", "p/e", "multiple"], "peer_value": ["peer", "implies"],
    "street_target": ["street", "consensus", "analyst", "target"],
    "weighted_intrinsic": ["probability-weighted", "weighted"],
    "bull": ["bull"], "bear": ["bear", "downside case"],
}
DOWNSIDE_WORDS = re.compile(r"downside|bear|bad case|stress|scenario", re.I)


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z][a-z&-]+", text.lower())
            if len(w) >= 3 and w not in STOP_WORDS}


def _metric_named(metric: str, sentence: str) -> bool:
    """Every significant word of a row label is in the sentence: "Ratings" for a Ratings
    row, "revenue" for "Total Revenue". The row label is the part after the last section."""
    label = metric.split(":")[-1]
    need = _words(label)
    return bool(need) and need <= _words(sentence)


def _same_model(f: Figure, model: Figure) -> bool:
    """`_same`, reading basis points as the model's percentage: "55bps" of credit cost is
    the model's 0.55%."""
    if f.kind == "pp" and model.kind == "%" and abs(model.value) < 5:
        return abs(f.value - model.value) <= max(f.slack, model.slack)
    return _same(f, model)


def _model_candidates(sentence: str, values: dict[str, str], fy_end: int,
                      raw: str = "") -> list[tuple]:
    """(placeholder key, figure, period key or None) for each model value the sentence
    names in words."""
    low = sentence.lower()
    # A segment named only inside a driver's name is the driver's: SPOT's "Premium
    # subscriber compounding at 9% CAGR" of 2026-10-11 took Premium revenue's FY2030 growth.
    drivers = {k.partition(":")[0][7:] for k in values or {} if k.startswith("driver.")}
    unnamed = sentence
    for d in sorted(drivers, key=len, reverse=True):
        unnamed = re.sub(re.escape(d).replace(r"\ ", r"\s+") + r"?s?\b", " ", unnamed, flags=re.I)
    compound = re.search(r"\bcagr\b|compound", low)
    out = []
    for key, value in (values or {}).items():
        name, _, year = key.partition(":")
        base = name.split(".", 1)
        # A compound rate is not one year's growth.
        if compound and year and "growth" in base[0]:
            continue
        if (base[0] in ("segment", "driver", "segment_cagr", "segment_share", "segment_growth",
                        "driver_cagr") and len(base) == 2):
            own = sentence if base[0].startswith("driver") else unnamed
            words_ok = _metric_named(base[1], own) and (
                not base[0].endswith("_cagr") or re.search(r"cagr|a year|annual", low))
        else:
            words_ok = any(w in low for w in MODEL_WORDS.get(name, []))
        # "{{downside}}" in the sentence counts: the masked text has lost it.
        if name.startswith("downside_") and not DOWNSIDE_WORDS.search(raw or sentence):
            words_ok = False
        found = figures(value)
        if not words_ok or len(found) != 1:
            continue
        digits = re.search(r"(\d{4})", year)
        period = ("12M", f"{digits.group(1)}-{fy_end:02d}") if digits else None
        out.append((key, found[0], period))
    return out


# Model values named by a phrase specific enough to pin a short figure ("3% perpetuity
# growth", "18x exit"); a segment's "4%" could be any of its rates.
SPECIFIC_MODEL = {"wacc", "g", "exit_multiple", "tv_share", "ev_ebitda", "peer_ev_ebitda",
                  "model_cagr", "implied_cagr", "tax_rate", "beta", "risk_free",
                  "cost_of_equity", "tv_ev_ebitda", "wacc_premium", "downside_g",
                  "downside_revenue_cut", "downside_exit_multiple", "downside_wacc",
                  "terminal_roic", "net_debt_to_ebitda", "interest_coverage", "cost_of_debt",
                  "earnings_yield", "buyback_eps_change", "terminal_roe", "ke",
                  "implied_roe", "implied_ke", "peer_pe", "street_target", "pb", "ptbv",
                  "weighted_intrinsic", "terminal_payout"}
RANGE_JOIN = re.compile(r"^\s?(?:-|–|—|to)\s?$")


def _significant(text: str) -> int:
    digits = re.sub(r"\D", "", text.split(".")[0]).lstrip("0") + (
        re.sub(r"\D", "", text.split(".")[1]) if "." in text else "")
    return len(digits)


def _in_range(text: str, f: Figure, found: list[Figure]) -> bool:
    for g in found:
        if g is f:
            continue
        between = text[min(f.end, g.end):max(f.start, g.start)]
        if (g.end <= f.start or f.end <= g.start) and RANGE_JOIN.match(between):
            return True
    # "5" in "5-7%" is not read as a figure of its own; its partner is.
    return bool(re.search(r"\d\s?(?:-|–|—|to)\s?$", text[max(0, f.start - 8):f.start])
                or re.match(r"^\s?(?:-|–|—)\s?\d", text[f.end:f.end + 6]))


def _ref_value(ref: str, fx: "Facts", values: dict[str, str]) -> Figure | None:
    name = ref.strip("{} ")
    fact = find(fx.by_id, name)
    if fact:
        return fact.figure
    import pitch     # the placeholder names, folded as the filler folds them
    lookup = {pitch._placeholder_key(*(k.split(":", 1) + [None])[:2]): v for k, v in values.items()}
    key = pitch._placeholder_key(*(name.split(":", 1) + [None])[:2])
    found = figures(lookup.get(key, ""))
    return found[0] if len(found) == 1 else None


def _drop_beside(sentence: str, fx: "Facts", values: dict[str, str]) -> str:
    """"Capex at 1.5% of revenue ({{capex_pct_revenue}})": a typed figure right beside a
    placeholder or id of the same value goes, and the citation stays."""
    masked = _masked(sentence)
    for f in sorted(figures(masked), key=lambda f: f.start, reverse=True):
        near = sentence[f.end:f.end + 40] + " " + sentence[max(0, f.start - 40):f.start]
        # An assumption that starts within 40 characters after the figure, read whole:
        # "30% or more, consistent with the {{assume:ratings_cyclical_floor}}" was cut off
        # mid-name (SPGI, 2026-10-05).
        assumed = [m.group(0) for m in ANY_PLACEHOLDER.finditer(sentence, f.end)
                   if m.start() - f.end <= 40 and m.group(0).strip("{} ").lower().startswith("assume")]
        for ref in ANY_PLACEHOLDER.findall(near) + assumed:
            value = _ref_value(ref, fx, values)
            if value is not None and _same(f, value) and ref.strip("{} ").lower().startswith("assume"):
                # "issuance falls 30% or more, consistent with the {{assume:floor}}
                # assumption": the figure is the writer's own and says so (SPGI, 2026-10-05).
                sentence = sentence[:f.start] + ref + sentence[f.end:]
                break
            if value is not None and _same(f, value):
                # "to 48% in FY2030 ({{ebit_margin:FY2030}})": the bracketed citation takes
                # the figure's place, or SPGI's rewrite read "to in FY2030 (48.0%)".
                later = re.search(r"\s*\(\s*" + re.escape(ref) + r"\s*\)",
                                  sentence[f.end:f.end + 60])
                if later:
                    sentence = (sentence[:f.start] + ref + sentence[f.end:f.end + later.start()]
                                + sentence[f.end + later.end():])
                    break
                lo = len(BOUND_BEFORE.sub("", sentence[:f.start]))
                sentence = (sentence[:lo] + sentence[f.end:].lstrip(" ")).replace("  ", " ")
                break
    return sentence


SERIES_REF = re.compile(r"\{\{\s*([\w. &/-]+?)\s*:\s*(FY\s?\d{2,4})\s*\}\}", re.I)


def _adopt_series(sentence: str, values: dict[str, str]) -> str:
    """"D&A falls from 8% to 3.5% of revenue ... ({{da_pct_revenue:FY2030}})": a sentence
    that cites one year of a model series has its typed figures matched against that
    series' other years, and each equal to exactly one year becomes that year's
    placeholder. The bracketed citation then repeats a figure the sentence already gives,
    and goes."""
    cited = {m.group(1).strip().lower() for m in SERIES_REF.finditer(sentence)}
    if not cited or not values:
        return sentence
    series: dict[str, list[tuple[str, Figure]]] = {}
    for key, value in values.items():
        name, _, year = key.partition(":")
        found = figures(value)
        if year and name.lower() in cited and len(found) == 1:
            series.setdefault(name.lower(), []).append((key, found[0]))
    masked = _masked(sentence)
    found = figures(masked)
    # No range test: "from 8% to 3.5%" reads as one, and each end is matched to a year of
    # the series the sentence cites, which is exact.
    for f in sorted(found, key=lambda f: f.start, reverse=True):
        hits = {key for entries in series.values() for key, fig in entries if _same(f, fig)}
        if len(hits) == 1:
            ref = "{{" + hits.pop() + "}}"
            lo = len(BOUND_BEFORE.sub("", sentence[:f.start]))
            sentence = sentence[:lo] + ref + sentence[f.end:]
    # "(... {{x:FY2030}})" after the same placeholder in the text reads the figure twice.
    for m in reversed(list(re.finditer(r"\s*\(\s*(\{\{[^{}]+\}\})\s*\)", sentence))):
        if sentence.count(m.group(1)) > 1:
            sentence = sentence[:m.start()] + sentence[m.end():]
    return sentence


def _adopt_ranges(sentence: str, labelled: list[Fact]) -> str:
    """"6-8%" for a guidance range the database holds as two ids ("6.0% to 8.0%"): both
    ends match the two figures of one cell, consecutive ids with one metric and period."""
    masked = _masked(sentence)
    found = figures(masked)
    by_id = {f.id: f for f in labelled}
    for a, b in reversed(list(zip(found, found[1:]))):
        if not RANGE_JOIN.match(masked[a.end:b.start]):
            continue
        for x in labelled:
            if not FACT_ID.fullmatch(x.id):
                continue        # a sheet figure holds its range in one name
            y = by_id.get(f"F{int(x.id[1:]) + 1}")
            if (y and x.metric == y.metric and x.period == y.period
                    and _same(a, x.figure) and _same(b, y.figure)
                    and _metric_named(x.metric, masked)):
                sentence = (sentence[:a.start] + "{{" + x.id + "}}" + sentence[a.end:b.start]
                            + "{{" + y.id + "}}" + sentence[b.end:])
                break
    return sentence


# How far the words naming a model value may sit from its figure to pick it out.
NEAREST_WORDS = 24


def _nearest_model(masked: str, f: Figure, models: list[tuple]) -> list[str]:
    """Among model values a figure could be, the one whose words sit right beside it:
    "the 3% terminal growth" is {{g}}, not FY2030's 3.2% revenue growth that the year in
    the same sentence also lets in (SPGI, 2026-10-05). Ties go to the closer value."""
    def gap(name: str) -> int:
        best = NEAREST_WORDS + 1
        for phrase in MODEL_WORDS.get(name, []):
            for m in re.finditer(re.escape(phrase), masked, re.I):
                d = m.start() - f.end if m.start() >= f.end else f.start - m.end()
                best = min(best, max(d, 0))
        return best

    ranked = sorted(models, key=lambda m: (gap(m[0].split(":")[0]),
                                           abs(m[1].value - f.value)))
    if not ranked or gap(ranked[0][0].split(":")[0]) > NEAREST_WORDS:
        return []
    top = ranked[0]
    tied = [m for m in ranked if gap(m[0].split(":")[0]) == gap(top[0].split(":")[0])
            and abs(m[1].value - f.value) == abs(top[1].value - f.value)]
    return ["{{" + m[0] + "}}" for m in tied]


def adopt_typed(text: str, fx: "Facts", values: dict[str, str] | None = None) -> str:
    """A typed figure becomes a citation when it is exactly one labelled fact, or one value
    of the model, whose metric the sentence names, for a period the sentence names (or for
    any period when the sentence names none and only one fits). Anything less certain stays
    typed, and is deleted."""
    labelled = [f for f in fx.by_id.values() if f.metric]

    def swap(sentence: str) -> str:
        masked = _masked(sentence)
        found = figures(masked)
        if not found:
            return sentence
        periods = [p for p in mentioned_periods(masked, fx.fy_end_month) if p[0] != "at"]
        model = _model_candidates(masked, values or {}, fx.fy_end_month, sentence)
        sentence = _adopt_series(sentence, values or {})
        sentence = _drop_beside(sentence, fx, values or {})
        sentence = _adopt_ranges(sentence, labelled)
        masked = _masked(sentence)
        found = figures(masked)
        for f in sorted(found, key=lambda f: f.start, reverse=True):
            # A range is an estimate, and half a range matched is worse than none: the
            # second rewrite's "Energy grows 4-5%" met two unrelated Energy figures.
            if _in_range(masked, f, found):
                continue
            facts_ = [x for x in labelled if _same(f, x.figure) and _metric_named(x.metric, masked)]
            models = [m for m in model if _same_model(f, m[1])]
            if periods:
                facts_ = [x for x in facts_ if x.period_key in periods]
                models = [m for m in models if m[2] is None or m[2] in periods]
            elif _significant(f.text) < 3:
                # With no period to pin it, "4%" fits too many figures to be one of them.
                facts_ = []
                models = [m for m in models if m[0].split(":")[0] in SPECIFIC_MODEL]
            # A sheet figure and the table cell it was read from are one figure: the
            # sheet's name is cited.
            named = [x for x in facts_ if not FACT_ID.fullmatch(x.id)]
            if named:
                facts_ = named
            # One cell read twice is one figure: NVO's 2Q26 6-K is both its results release
            # and its interim report, so each cell was two facts and none was adopted.
            facts_ = list({(x.metric.lower(), x.period_key, x.figure.value): x
                           for x in reversed(facts_)}.values())[::-1]
            # Aliases of one model value are one citation: {{ke}} and {{cost_of_equity}}
            # are both GS's 10.6%, and the pair left it typed (2026-10-13).
            if models and not facts_ and len({(values or {}).get(m[0]) for m in models}) == 1:
                models = models[:1]
            choices = [("{{" + x.id + "}}") for x in facts_] + [("{{" + m[0] + "}}") for m in models]
            if len(set(choices)) > 1 and not facts_:
                choices = _nearest_model(masked, f, models)
            if len(set(choices)) != 1:
                continue
            ref = choices[0]
            # "15.0x {{ev_ebitda}}": the writer typed it and cited it; keep the citation.
            if ref in sentence[f.end:f.end + 40]:
                lo = len(BOUND_BEFORE.sub("", sentence[:f.start]))
                sentence = sentence[:lo] + sentence[f.end:].lstrip(" ")
            else:
                sentence = sentence[:f.start] + ref + sentence[f.end:]
        return sentence

    parts = re.split(f"({SENTENCE_END.pattern})", text)
    # What no table or model value is may be a sentence of the company's.
    return adopt_statements("".join(swap(p) if i % 2 == 0 else p for i, p in enumerate(parts)),
                            fx)


# ------------------------------------------------------------------ company statements

# The sentences of a filing or release: split where a sentence ends, at a blank line and
# at a bullet.
STATEMENT_SPLIT = re.compile(rf"(?<=[.;!?]){NOT_AN_END}\s+(?=[A-Z(])|\n\s*\n|\s[•●▪·]\s")
# "between 18% and 20%", "18-20%", "12 to 14%": a range in the company's words.
STATEMENT_RANGE = re.compile(r"^\s?(?:-|–|—|to|and)\s?$")
LINE_BULLET = re.compile(r"\n\s*[–—•●▪·-]\s*\n")
NEAR_WORDS = 8          # the words either side of a typed figure, which say what it is
# The evidence a typed figure is the company's: words around it its sentence has and the
# company's other sentences do not all use, and how many of those are lowercase in the
# sentence, saying what is measured rather than whose it is. CAT's "Resource Industries
# (20%)" of revenue shared only segment names with "20 percent in Resource Industries" of
# its tariff costs. A range's two ends matching is evidence of its own.
STATEMENT_MATCH = {1: (3, 2), 2: (2, 1)}
PROSE_VERB = re.compile(r"\b(?:was|were|is|are|amounted|totall?ed|reached|increased|decreased|"
                        r"rose|fell|grew|declined|expects?|represent(?:s|ed)?)\b")
SHARE_OF_REVENUE = re.compile(r"\s*of\s+(?:total\s+|net\s+)?(?:revenues?|sales)\b", re.I)
# A bound in front of a figure: "Non-GAAP operating margins surpassing the 10% mark" is
# not a margin of 10% (ESLT, 2026-10-01).
BOUND = re.compile(r"(?:more than|over|above|exceed\w*|surpass\w*|cross\w*|at least|beyond|up to|below|"
                   r"less than|under|nearly|almost|\+|>|<|≥|≤)\s*(?:the\s+)?(?:[A-Z]{3}\s?|\$)?$",
                   re.I)


def _statements(block: str, source: str, fy_end: int, start: int = 0) -> list[Fact]:
    """The figures in the sentences of one source, each with its sentence as context. A
    table read into a line ("Net sales 4 497 4 390 2% Gross margin % 44.2% 41.5%") or a
    cover page is not a sentence: one has four words or more to each figure, at most four
    figures and at most 60 words."""
    out: list[Fact] = []
    for raw in STATEMENT_SPLIT.split(block):
        # A list item: "S&P Global Ratings / – 34% of revenue / – 43% of adjusted operating
        # profit (65% margin)", each part on a line of its own (SPGI's recast of
        # 2026-07-06). Fewer words to a figure than a sentence, but not a table.
        listed = bool(LINE_BULLET.search(raw))
        sentence = " ".join(LINE_BULLET.sub(" – ", raw).split())
        found = [f for f in figures(sentence)
                 if not NOT_A_FIGURE.search(sentence[max(0, f.start - 14):f.start])
                 and not _in_url(sentence, f.start)]
        words = len(re.findall(r"[A-Za-z]{3,}", sentence))
        # A sentence with a verb that ends as one is prose however dense: ESLT's "GAAP
        # operating income in the second quarter of 2026 was $218.8 million (9.6% of
        # revenues), as compared to $157.8 million (8.0% of revenues) in the second quarter
        # of 2025." has 15 words to its four figures, and was read as a table row.
        prose = bool(PROSE_VERB.search(sentence)) and sentence.rstrip().endswith(".")
        per = 2 if listed else 3 if prose else 4
        if not found or len(found) > 4 or not per * len(found) <= words <= 60:
            continue
        periods = {p for p in mentioned_periods(sentence, fy_end) if p[0] != "at"}
        kind = "guidance" if GUIDANCE.search(sentence) else "reported"
        for f in found:
            out.append(Fact(f"S{start + len(out) + 1}", f, source, True, sentence,
                            table="company list" if listed else "company statement",
                            period_key=next(iter(periods)) if len(periods) == 1 else None,
                            data_type=kind))
    return out


@dataclass
class _Said:
    """What the writer's words say about a typed figure, to hold a statement to."""
    near: set[str]                  # the words around it
    periods: list = field(default_factory=list)
    years: set[str] = field(default_factory=set)
    bounded: bool | None = None     # a bound in front of it; None when not known
    # Written no more precisely than the sentence ("30.5%" is not "30 percent"); a declared
    # assumption is a bare amount in millions ("7000"), compared as one.
    exact: bool = True


def _years(text: str) -> set[str]:
    """The years a text names: "2026", "FY26", "2Q26", "Q2'26"."""
    found = set(re.findall(r"(?<!\d)(20\d{2})(?!\d)", text))
    found |= {"20" + y for y in re.findall(r"\b(?:FY\s?'?|[1-4]Q\s?'?|Q[1-4]\s?'?|[12]H\s?'?|"
                                             r"H[12]\s?'?)(\d{2})\b", text, re.I)}
    return found


def _index(found: list[Fact]) -> dict[str, tuple[set[str], set[str], set[str]]]:
    """Each sentence's (specific words, the lowercase ones, years). Specific words say what
    its figures are: the figures themselves are left out, and so are the words in more
    than a fifth of the sentences (the company's name, "net sales")."""
    sets: dict[str, tuple[set[str], set[str]]] = {}
    for f in found:
        if f.context not in sets:
            text = f.context
            for g in sorted((g for g in found if g.context == f.context),
                            key=lambda g: g.figure.start, reverse=True):
                text = text[:g.figure.start] + " " + text[g.figure.end:]
            lower = " ".join(re.findall(r"(?<![\w&-])[a-z][a-z&-]+", text))
            sets[f.context] = (_words(text), _words(lower))
    df = Counter(w for ws, _ in sets.values() for w in ws)
    common = max(3, len(sets) // 5)
    return {s: ({w for w in ws if df[w] <= common}, {w for w in low if df[w] <= common},
                _years(s)) for s, (ws, low) in sets.items()}


def _ranges(found: list[Fact]) -> list[tuple[int, int]]:
    """(i, i + 1) for each range the sentences state: two figures of one kind, joined."""
    return [(i, i + 1) for i in range(len(found) - 1)
            if found[i].context == found[i + 1].context
            and found[i].figure.kind == found[i + 1].figure.kind
            and STATEMENT_RANGE.match(found[i].context[found[i].figure.end:found[i + 1].figure.start])]


QUOTE_RUN = 6           # words in a row the writer's sentence shares with the company's


def _word_list(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def _quoted(f: Figure, masked: str, found: list[Fact]) -> Fact | None:
    """The company's sentence the writer quoted: the same figure, and QUOTE_RUN words in a
    row shared with it. GS's pitch of 2026-10-13 wrote "Net revenues in Equities were $7.42
    billion, 72% higher than the second quarter of 2025" from its release word for word,
    and lost both figures: "net revenues" is in most of the release's sentences, so
    `_stated` found too few specific words. Only one sentence may fit."""
    mine = _word_list(masked)
    runs = {tuple(mine[i:i + QUOTE_RUN]) for i in range(len(mine) - QUOTE_RUN + 1)}
    if not runs:
        return None
    fits = []
    for fact in found:
        if not (_same(f, fact.figure) and f.slack >= fact.figure.slack):
            continue
        theirs = _word_list(fact.context)
        if any(tuple(theirs[i:i + QUOTE_RUN]) in runs
               for i in range(len(theirs) - QUOTE_RUN + 1)):
            fits.append(fact)
    values = {(x.figure.kind, x.figure.value) for x in fits}
    return fits[0] if len(values) == 1 else None


def _stated(group: list[Figure], said: _Said, found: list[Fact], index: dict,
            pairs: list[tuple[int, int]]) -> list[Fact] | None:
    """The facts among `found` a typed figure is, or a typed range's two ends: the same
    values, written no more precisely than the sentence does ("30.5%" is not "30
    percent"), a range for a range and a single figure for a single one, bounded if the
    sentence bounds it, for no other period or year, in the sentence that shares the most
    specific words with what the writer said, at least STATEMENT_MATCH of them."""
    if len(group) == 2:
        candidates = [[found[i], found[j]] for i, j in pairs]
    else:
        in_range = {i for pair in pairs for i in pair}
        candidates = [[f] for i, f in enumerate(found) if i not in in_range]
    need, need_lower = STATEMENT_MATCH[len(group)]
    best, score = None, (need - 1, 0.0)
    for facts_ in candidates:
        if not all(_same(g, f.figure) and g.slack >= f.figure.slack if said.exact
                   else _equal(g, f.figure) for g, f in zip(group, facts_)):
            continue
        first = facts_[0]
        if said.periods and first.period_key and first.period_key not in said.periods:
            continue
        specific, lower, years = index[first.context]
        if said.years and years and not said.years & years:
            continue
        if (said.bounded is False
                and BOUND.search(first.context[max(0, first.figure.start - 20):first.figure.start])):
            continue
        # A share of revenue is a margin: "operating income was $237.5 million (10.4% of
        # revenues)" is ESLT's 10.4% operating margin, which its writer of 2026-10-07 wrote
        # as one and lost.
        if SHARE_OF_REVENUE.match(first.context[first.figure.end:]):
            specific, lower = specific | {"margin"}, lower | {"margin"}
        shared = said.near & specific
        # Among sentences as close in words, the nearest value: "6-8% organic" is SPGI's
        # "organic constant currency growth of 6.0% to 8.0%", not its "revenue growth of
        # 5.9% to 7.9%" in the same sentence.
        rank = (len(shared), -sum(abs(g.value - f.figure.value) / max(abs(f.figure.value), 1e-9)
                                  for g, f in zip(group, facts_)))
        if rank > score and len(shared & lower) >= need_lower:
            best, score = facts_, rank
    return best


# The writer's own estimate just before a figure, with nothing crediting the company in
# between: "I assume 50% mitigation succeeds", not "I model 7% (management guide 6-8%)".
OWN_BEFORE = re.compile(r"\b(?:I|we|my|our)\b[^.;]{0,40}?\b(?:assum|model|project|forecast|"
                        r"estimat|pencil|haircut|taper|underwrit)\w*(?P<rest>[^.;]*)$")
CREDITED = re.compile(r"\b(?:management|company|guid\w*|per|according|reported|disclos\w*|"
                      r"stated|states|said|says|release|filing|10-[KQ]|20-F)\b", re.I)


# How far from a typed figure the name of a list item's label may sit.
LIST_LABEL_CHARS = 40


def _listed(f: Figure, masked: str, said: _Said, found: list[Fact]) -> Fact | None:
    """A typed figure that is one item of the company's list: the same value, the item's
    label named right beside it, and a word of its own part of the item ("margin" for
    "(65% margin)") among the writer's words. The parts of a list share their measure
    words ("adjusted", "revenue"), so the sentence test finds nothing specific in them:
    SPGI's pitch of 2026-10-05 lost "Ratings operates at a 65% adjusted margin and Indices
    at 70%", both from the July 6 recast's division list."""
    best, nearest = None, LIST_LABEL_CHARS + 1
    for fact in found:
        if fact.table != "company list" or not (
                _same(f, fact.figure) and f.slack >= fact.figure.slack if said.exact
                else _equal(f, fact.figure)):
            continue
        parts = fact.context.split(" – ")
        label, part, offset = parts[0], None, 0
        for p in parts:
            start = fact.context.find(p, offset)
            if start <= fact.figure.start < start + len(p):
                part = p
                break
            offset = start + len(p)
        if part is None or part == label:
            continue
        measure = _words(" ".join(re.findall(r"(?<![\w&-])[a-z][a-z&-]+", part)))
        if not measure & said.near:
            continue
        gap = LIST_LABEL_CHARS + 1
        for word in re.findall(r"[A-Za-z][A-Za-z&-]{2,}", label):
            if word.lower() in {"global", "the", "and"} or not word[0].isupper():
                continue
            for m in re.finditer(r"\b" + re.escape(word), masked):
                d = m.start() - f.end if m.start() >= f.end else f.start - m.end()
                gap = min(gap, max(d, 0))
        if gap < nearest:
            best, nearest = fact, gap
        elif gap == nearest and best is not None and best.context != fact.context:
            best = None     # two items as near: not one of them
    return best


def _own(before: str) -> bool:
    m = OWN_BEFORE.search(before)
    return bool(m) and not CREDITED.search(m.group("rest"))


def _near(text: str, lo: int, hi: int) -> set[str]:
    before = re.findall(r"[A-Za-z][A-Za-z&-]+", text[:lo])[-NEAR_WORDS:]
    after = re.findall(r"[A-Za-z][A-Za-z&-]+", text[hi:])[:NEAR_WORDS]
    return _words(" ".join(before + after))


def adopt_statements(text: str, fx: "Facts") -> str:
    """Typed figures the company states in its own filings and releases become citations
    of its sentence (an S-id), printed as it wrote them with the source. NOK's pitch of
    2026-10-04 lost "a contracted net sales run-rate of EUR 1.4 billion" (6-K) and
    management's 18-20% growth for Optical and IP Networks (Q1 report): no table holds
    either, so neither was on the sheet. The words around a typed figure say what it is
    (`_stated`); anything less certain stays typed, and is deleted."""
    found = fx.statements
    if not found:
        return text
    index, pairs = _index(found), _ranges(found)

    def swap(sentence: str) -> str:
        masked = _masked(sentence)
        typed_ = figures(masked)
        if not typed_:
            return sentence
        periods = [p for p in mentioned_periods(masked, fx.fy_end_month) if p[0] != "at"]
        groups, i = [], 0
        while i < len(typed_):
            if i + 1 < len(typed_) and RANGE_JOIN.match(masked[typed_[i].end:typed_[i + 1].start]):
                groups.append(typed_[i:i + 2])
                i += 2
            else:
                groups.append(typed_[i:i + 1])
                i += 1
        for group in reversed(groups):
            lo, hi = group[0].start, group[-1].end
            # The writer's own figure: "I assume 50% mitigation succeeds", "offset 50% of
            # tariff costs (my estimate)" (CAT, 2026-10-01) met the company's 50 percent of
            # its tariff costs.
            if _own(masked[max(0, lo - 80):lo]) or WRITER_LABEL.search(masked[hi:hi + 40]):
                continue
            said = _Said(_near(masked, lo, hi), periods, _years(masked),
                         bool(BOUND.search(masked[max(0, lo - 20):lo])) or group[0].approx)
            hit = _stated(group, said, found, index, pairs)
            if not hit and len(group) == 1:
                item = _listed(group[0], masked, said, found)
                hit = [item] if item else None
            if not hit and len(group) == 1:
                item = _quoted(group[0], masked, found)
                hit = [item] if item else None
            if not hit:
                continue
            for f, fact in sorted(zip(group, hit), key=lambda p: p[0].start, reverse=True):
                fx.by_id.setdefault(fact.id, fact)
                sentence = sentence[:f.start] + "{{" + fact.id + "}}" + sentence[f.end:]
        return sentence

    parts = re.split(f"({SENTENCE_END.pattern})", text)
    return "".join(swap(p) if i % 2 == 0 else p for i, p in enumerate(parts))
