"""Periods: what span of time a figure covers, and the label a reader knows it by.

A 2Q26 margin presented as an FY2026 one, or a quarter's growth as a CAGR, was the most
common error in V1's pitches. Every fact therefore carries a Period, and two facts are the
same figure only when their periods are equal, not when their labels look alike.

Fiscal years are named by the calendar year they end in (NVDA's year to January 2026 is
FY2026). Companies on 52/53-week years end a few days into the next month (a year "to
December" ending 3 January), so an end date in the first week counts as the month before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

SPANS = ("3M", "6M", "9M", "12M")
TYPES = {"3M": "quarter", "6M": "half", "9M": "nine_months", "12M": "annual",
         "TTM": "ttm", "at": "point", "latest": "point"}
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
               "Dec"]


def span_of(start: date, end: date) -> str | None:
    """The span of a reported duration, or None for one that is not a standard period."""
    days = (end - start).days
    for span, lo, hi in (("3M", 80, 100), ("6M", 170, 195), ("9M", 260, 285),
                         ("12M", 350, 380)):
        if lo <= days <= hi:
            return span
    return None


def month_of(end: date) -> tuple[int, int]:
    """(year, month) a period end belongs to: the 3rd of January closes December."""
    if end.day <= 7:
        end = end - timedelta(days=end.day)
    return end.year, end.month


@dataclass(frozen=True)
class Period:
    span: str               # "3M", "6M", "9M", "12M", "TTM", "at" (a balance), "latest" (market)
    end: date | None        # the last day covered, or the balance date
    fy_end: int = 12        # the month the company's fiscal year ends

    @property
    def type(self) -> str:
        return TYPES[self.span]

    @property
    def key(self) -> tuple:
        if self.span == "at" and self.end is not None and not self.at_quarter_end():
            return (self.span, self.end.isoformat())
        """What makes two periods the same: span and end month, not the exact day (a 10-Q
        and a release can date one quarter 28 and 30 June)."""
        if self.end is None:
            return (self.span,)
        return (self.span, *month_of(self.end))

    def fiscal_year(self) -> int:
        year, month = month_of(self.end)
        return year if month <= self.fy_end else year + 1

    def quarter(self) -> int:
        """Which fiscal quarter the period ends in, 1 to 4."""
        _, month = month_of(self.end)
        return ((month - self.fy_end - 1) % 12) // 3 + 1

    @property
    def label(self) -> str:
        if self.span == "latest":
            return "latest"
        fy = self.fiscal_year()
        yy = f"{fy % 100:02d}"
        q = self.quarter()
        if self.span == "12M":
            return f"FY{fy}"
        if self.span == "3M":
            return f"{q}Q{yy}"
        if self.span == "6M":
            return f"1H{yy}" if q == 2 else f"6M to {q}Q{yy}"
        if self.span == "9M":
            return f"9M{yy}" if q == 3 else f"9M to {q}Q{yy}"
        if self.span == "TTM":
            return f"TTM to {q}Q{yy}" if q != 4 else f"TTM to FY{fy}"
        # A balance at the end of a fiscal year or quarter, as it is reported; any other
        # date (a share count on a filing's cover) by the date itself.
        if not self.at_quarter_end():
            return f"at {short_date(self.end)}"
        return f"end FY{fy}" if q == 4 else f"end {q}Q{yy}"

    def at_quarter_end(self) -> bool:
        _, month = month_of(self.end)
        if (month - self.fy_end) % 3:
            return False
        day = self.end.day
        return day >= 24 or day <= 7

    @property
    def slug(self) -> str:
        """The label as it appears in a fact id: "2Q26", "FY2025", "ttm_2Q26", "end_FY2025"."""
        return re.sub(r"[^A-Za-z0-9]+", "_", self.label.replace("TTM to ", "ttm ")).strip("_")

    def prior_year(self) -> "Period":
        """The same period a year earlier."""
        return Period(self.span, _shift_year(self.end, -1), self.fy_end)

    def to_json(self) -> dict:
        return {"span": self.span, "end": self.end.isoformat() if self.end else None,
                "type": self.type, "label": self.label}


def _shift_year(d: date | None, years: int) -> date | None:
    if d is None:
        return None
    try:
        return d.replace(year=d.year + years)
    except ValueError:          # 29 February
        return d.replace(year=d.year + years, day=28)


def month_end(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def fiscal_period(span: str, fiscal_year: int, quarter: int, fy_end: int) -> Period:
    """The period of `span` ending with fiscal quarter `quarter` of `fiscal_year`."""
    month = fy_end - 3 * (4 - quarter)
    year = fiscal_year
    while month <= 0:
        month += 12
        year -= 1
    return Period(span, month_end(year, month), fy_end)


# ---------------------------------------------------------------- reading headings

MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june",
                                          "july", "august", "september", "october",
                                          "november", "december"])}
SPAN_WORDS = {"three": "3M", "six": "6M", "nine": "9M", "twelve": "12M"}
# "2Q26", "2Q '26", "Q2 2026", "Q2 FY26", "second quarter of 2026"
QUARTER = re.compile(r"\b(?:(?P<q1>[1-4])Q\s?'?(?P<y1>\d{4}|\d{2})|Q(?P<q2>[1-4])\s?(?:FY)?\s?'?"
                     r"(?P<y2>\d{4}|\d{2})|(?P<word>first|second|third|fourth)[- ]quarter"
                     r"(?: of)?(?: fiscal)?(?: year)? (?P<y3>\d{4}))\b", re.I)
ORDINAL = {"first": 1, "second": 2, "third": 3, "fourth": 4}
FULL_YEAR = re.compile(r"\b(?:FY\s?'?(?P<y1>\d{4}|\d{2})|fiscal (?:year )?(?P<y2>\d{4})|"
                       r"full[- ]year (?P<y3>\d{4}))\b", re.I)


def _year(text: str) -> int:
    y = int(text)
    return y + 2000 if y < 100 else y


def from_heading(column: str, title: str, fy_end: int) -> Period | None:
    """The period a table column covers, read from its heading and the table's title, or
    None when they do not say. "Three Months 2026" under "...ended June 30, 2026" is the
    quarter to June 2026; "June 30, 2026" in a balance sheet is a balance at that date."""
    q = QUARTER.search(column)
    if q:
        if q.group("q1"):
            n, fy = int(q.group("q1")), _year(q.group("y1"))
        elif q.group("q2"):
            n, fy = int(q.group("q2")), _year(q.group("y2"))
        else:
            n, fy = ORDINAL[q.group("word").lower()], _year(q.group("y3"))
        return fiscal_period("3M", fy, n, fy_end)
    if re.search(r"change|%|y/y|growth", column, re.I):
        return None
    year = re.search(r"\b((?:19|20)\d{2})\b", column)
    if not year:
        return None
    both = f"{column} {title}"
    span = re.search(r"\b(three|six|nine|twelve)\s+months\b", column, re.I) or \
        re.search(r"\b(three|six|nine|twelve)\s+months\b", title, re.I)
    month = re.search(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2}),", both, re.I)
    end_month = MONTHS[month.group(1).lower()] if month else fy_end
    end = month_end(int(year.group(1)), end_month)
    if span and not month and SPAN_WORDS[span.group(1).lower()] != "12M":
        # "Three Months 2026" with no month anywhere: which quarter is not said.
        return None
    if span and not re.search(r"balance sheet|financial position", title, re.I):
        # A title naming two spans ("Three and six months") leaves the column to say which.
        spans = re.findall(r"\b(three|six|nine|twelve)\b", title, re.I)
        if len(set(s.lower() for s in spans)) > 1 and not re.search(
                r"\b(three|six|nine|twelve)\s+months\b", column, re.I):
            return None
        return Period(SPAN_WORDS[span.group(1).lower()], end, fy_end)
    if re.search(r"balance sheet|financial position", title, re.I) or (
            month and re.search(r"\b(?:at|as of)\b", both, re.I)):
        return Period("at", end, fy_end)
    if re.search(r"\b(?:year|fiscal|annual)\b", both, re.I) and not month:
        return Period("12M", end, fy_end)
    return None


def full_year_in(text: str, fy_end: int) -> Period | None:
    """The fiscal year a guidance title names: "Full-Year 2026 Outlook" -> FY2026."""
    m = FULL_YEAR.search(text)
    if m:
        year = _year(next(g for g in m.groups() if g))
    else:
        m = re.search(r"\b(20\d{2})\b", text)
        if not m:
            return None
        year = int(m.group(1))
    return fiscal_period("12M", year, 4, fy_end)


def fy_end_from_annual_ends(ends: list[date]) -> int:
    """The fiscal year-end month: the commonest end month of the annual periods filed."""
    months = [month_of(e)[1] for e in ends]
    return max(set(months), key=months.count) if months else 12


def short_date(d: date) -> str:
    return f"{d.day} {MONTH_NAMES[d.month - 1]} {d.year}"
