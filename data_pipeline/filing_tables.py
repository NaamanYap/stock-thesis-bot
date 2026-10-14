"""The tables of an SEC filing, each figure with the row and column it sits in.

A filing's tables reached the research notes flattened into text: "Revenue $ 4,146 $
3,755 10% $ 8,318 $ 7,532 10%" says nothing about which figure is the quarter and which
the half year, and SPGI's pitch of 2026-10-02 presented the fourth quarter's segment
growth as the second's. Read from the HTML, every figure has its row label, its column
heading and the table's scale, so the code can label it (facts.py) without the writer's
reading of it.

A table renders as one line per row, under a heading that names the table and its scale:

    ### Condensed Consolidated Statements of Income (USD in millions)
    - Revenue — Three Months 2026: USD 4,146mn; Three Months 2025: USD 3,755mn; ...
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

NUMBER = re.compile(r"^\(?-?\$?\d[\d,]*(?:\.\d+)?\)?%?\)?$")
PARTS = re.compile(r"^(?P<neg>\()?-?\$?(?P<num>\d[\d,]*(?:\.\d+)?)\)?(?P<pct>%)?\)?$")
YEAR = re.compile(r"^(?:19|20)\d{2}$")
# A cell that states its figure in words or as a range, as guidance tables do: "~$785
# million", "$205 to $215 million", "5.9% to 7.9%", "335 to 360 bps". Kept as written.
WORDED = re.compile(r"^~?\$?\d[\d,]*(?:\.\d+)?%?(?:\s*(?:to|-|–)\s*~?\$?\d[\d,]*(?:\.\d+)?%?)?"
                    r"\s*(?:million|billion|bps|basis points|%)?$", re.I)
# "in millions", "in € millions", "in EUR millions"
# "in millions", "in € millions", "in EUR millions"; and "DKK million", as NVO's 6-K heads
# its tables, whose DKK 9,122 of H1 2026 sales read as DKK 9,122 (2026-10-11).
SCALE = re.compile(r"\bin\s+(?:(?:US\$|€|£|¥|\$|[A-Z]{3})\s*)?(millions|thousands|billions)\b"
                   r"|\b(?:DKK|EUR|SEK|NOK|CHF|GBP|USD|JPY)\s+(million|thousand|billion)s?\b",
                   re.I)
# The currency a table is in. SPOT's 6-K report is "(in € millions)": read as no currency
# and no scale, its EUR 4,777mn of revenue became a count of 4,777.
CURRENCY_MARKS = [(re.compile(r"€|\beuros?\b|\bEUR\b"), "EUR"),
                  (re.compile(r"£|\bGBP\b|\bpounds sterling\b"), "GBP"),
                  (re.compile(r"\bNT\$|\bTWD\b|\bNT dollars\b"), "TWD"),
                  (re.compile(r"¥|\bJPY\b|\byen\b"), "JPY"),
                  (re.compile(r"\b(CHF|SEK|DKK|NOK|CNY|HKD|KRW|INR|CAD|AUD)\b"), None),
                  (re.compile(r"\$|\bUSD\b"), "USD")]
SCALE_SUFFIX = {"millions": "mn", "thousands": "k", "billions": "bn"}
# A column of rates whose cells carry no "%": NVO's 6-K prints "Growth at CER" as "3",
# which read as DKK 3 (2026-10-11).
PERCENT_COLUMN = re.compile(r"%|\bgrowth\b|\bat CER\b", re.I)
# Rows that are counts or per-share amounts, not the table's money.
PER_SHARE = re.compile(r"per\s+(?:common\s+)?share|\bEPS\b", re.I)
SHARE_COUNT = re.compile(r"\bshares\b|share count", re.I)
TITLE_WORDS = 20
MIN_FIGURES = 6
# The tables a pitch needs most come first when the limit cuts: results by segment, the
# income statement, guidance; then the balance sheet and cash flows; reconciliations last.
FIRST = re.compile(r"segment|revenue|income|operations|guidance|outlook|pro forma", re.I)
LAST = re.compile(r"reconciliation|non-gaap", re.I)


@dataclass
class Cell:
    row: str            # the row label, with the section it sits under: "Assets: Goodwill"
    column: str         # the column heading: "Three Months 2026", "June 30, 2026"
    text: str           # as printed here: "USD 4,146mn", "10%", "USD 4.12"


@dataclass
class Table:
    title: str          # the words before the table: "Condensed Consolidated Statements of Income"
    scale: str          # "millions", "thousands", "" when the table does not say
    cells: list[Cell]

    def render(self) -> str:
        rows: dict[str, list[Cell]] = {}
        for c in self.cells:
            rows.setdefault(c.row, []).append(c)
        scale = f" (in {self.scale})" if self.scale and self.scale not in self.title else ""
        lines = [f"### {self.title}{scale}"]
        for label, cells in rows.items():
            lines.append(f"- {label} — " + "; ".join(f"{c.column}: {c.text}" for c in cells))
        return "\n".join(lines)


def _text(td: Tag) -> str:
    return " ".join(td.get_text(" ", strip=True).split())


def _grid(table: Tag) -> list[list[str]]:
    """The table as rows of equal width, a merged cell repeated across its columns."""
    rows = []
    for tr in table.find_all("tr"):
        row = []
        for td in tr.find_all(["td", "th"]):
            try:
                span = max(1, min(int(td.get("colspan") or 1), 50))
            except ValueError:
                span = 1
            row += [_text(td)] * span
        rows.append(row)
    width = max((len(r) for r in rows), default=0)
    return [r + [""] * (width - len(r)) for r in rows]


def _is_number(text: str) -> bool:
    return bool(NUMBER.match(text.replace(" ", ""))) or bool(
        WORDED.match(text.strip()) and re.search(r"[a-z%~]|\bto\b", text, re.I))


def _title(table: Tag) -> str:
    """The nearest words before the table, which in a filing name it: from the start of
    the last sentence or line they contain."""
    pieces: list[str] = []
    count = 0
    for s in table.find_all_previous(string=True):
        text = " ".join(str(s).split())
        if not text or _is_number(text):
            continue
        pieces.insert(0, text)
        count += len(text.split())
        if count >= TITLE_WORDS * 2:
            break
    joined = " ".join(pieces)
    tail = re.split(r"(?<=[.:])\s+(?=[A-Z])|\bPage \d+\b", joined)[-1].strip()
    words = (tail or joined).split()
    return " ".join(words[-TITLE_WORDS:])


def currency_of(text: str) -> str:
    """The currency a table's title or text names, or "" for none."""
    for pattern, code in CURRENCY_MARKS:
        m = pattern.search(text)
        if m:
            return code or m.group(1)
    return ""


def _value(raw: str, row: str, money: bool, scale: str, currency: str = "USD",
           percent_column: bool = False) -> str:
    """The figure as printed in the notes: scaled and with its currency where the table
    gives them, so "4,146" in a table in millions reads "USD 4,146mn"."""
    m = PARTS.match(raw.replace(" ", ""))
    if not m:
        # Worded or a range: the money is spelled out; "~$785 million" reads as written,
        # with the currency the table is in.
        return raw.replace("$", f"{currency} ") if money and "$" in raw else raw
    text = m.group("num")
    sign = "-" if m.group("neg") or raw.strip().startswith("-") else ""
    if m.group("pct") or percent_column:
        return sign + text + "%"
    if PER_SHARE.search(row):
        return f"{currency} {sign}{text}" if money else sign + text
    suffix = SCALE_SUFFIX.get(scale, "")
    if SHARE_COUNT.search(row):
        return f"{sign}{text}{suffix} shares"
    if money:
        return f"{currency} {sign}{text}{suffix}"
    return sign + text + suffix


def read(table: Tag, context: str = "") -> Table | None:
    """One table, or None when it holds too few figures to be a financial table."""
    grid = _grid(table)
    if not grid:
        return None
    # The first row with a figure in it; a row of years is a heading.
    first_data = next((i for i, r in enumerate(grid)
                       if any(_is_number(c) and not YEAR.match(c) for c in r[1:])), None)
    if first_data is None:
        return None
    columns = []
    for j in range(len(grid[0])):
        parts: list[str] = []
        for h in grid[:first_data]:
            if h[j] and h[j] not in parts and not h[j].lower().startswith("(unaudited"):
                parts.append(h[j])
        columns.append(" ".join(parts))

    head = context + " " + table.get_text(" ")[:400]
    scale_match = SCALE.search(head)
    scale = ""
    if scale_match:
        word = (scale_match.group(1) or scale_match.group(2)).lower()
        scale = word if word.endswith("s") else word + "s"
    # The scale's own words, then the table, then the text before it.
    currency = (currency_of(scale_match.group(0) if scale_match else "")
                or currency_of(table.get_text(" ")[:400]) or currency_of(context))
    money = "$" in table.get_text() or bool(scale) or currency not in ("", "USD")
    currency = currency or "USD"
    # Section rows ("Earnings per share:", then "Net income:") name the rows under them;
    # the two latest are kept, so "Basic" under both is still a per-share figure.
    cells, sections = [], []
    for row in grid[first_data:]:
        label = next((c for c in row if c and not _is_number(c) and c != "$"), "")
        values = [(j, c) for j, c in enumerate(row) if _is_number(c)]
        if not values:
            if label.endswith(":"):
                sections = (sections + [label.rstrip(":")])[-2:]
            continue
        name = ": ".join([*sections, label] if label else sections)
        seen = set()
        for j, raw in values:
            if (columns[j], raw) in seen:      # a merged value cell repeated by the grid
                continue
            seen.add((columns[j], raw))
            cells.append(Cell(name, columns[j] or "value",
                              _value(raw, name, money, scale, currency,
                                     bool(PERCENT_COLUMN.search(columns[j] or "")))))
    if len(cells) < MIN_FIGURES:
        return None
    return Table(_title(table), scale, cells)


def tables(html: str | BeautifulSoup) -> list[Table]:
    """Every financial table in a filing, in order."""
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "lxml")
    read_from = []
    for t in soup.find_all("table"):
        before = " ".join(str(x) for x in t.find_all_previous(string=True, limit=8))
        found = read(t, before)
        if found:
            read_from.append((t, before, found))
    # SPGI's 2Q26 release stated "in millions" on most tables but not on its pro forma
    # revenue table, whose USD 7,397 then read as dollars and printed a 35703664% share.
    # A table that gives no scale takes the one most of the document's tables state.
    stated = Counter(f.scale for _, _, f in read_from if f.scale)
    common, n = stated.most_common(1)[0] if stated else ("", 0)
    out = []
    for t, before, found in read_from:
        if (not found.scale and common and n >= 0.6 * sum(stated.values())
                and ("$" in t.get_text() or currency_of(before))):
            found = read(t, f"{before} in {common}") or found
        out.append(found)
    return out


def _priority(t: Table) -> int:
    rows = " ".join(dict.fromkeys(c.row for c in t.cells))
    return ((0 if FIRST.search(t.title) or re.search(r"(?:^|\s)Revenue", rows) else 1)
            + (2 if LAST.search(t.title) else 0))


def render(found: list[Table], limit: int) -> str:
    """Tables up to `limit` characters, whole tables only, the most useful first, printed
    in the filing's order."""
    keep, total = set(), 0
    for i, t in sorted(enumerate(found), key=lambda it: _priority(it[1])):
        size = len(t.render()) + 2
        if total + size <= limit:
            keep.add(i)
            total += size
    return "\n\n".join(t.render() for i, t in enumerate(found) if i in keep)
