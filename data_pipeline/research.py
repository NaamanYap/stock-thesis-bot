"""Stage 1: research, by code only. Reads the sources in parallel, builds the fact
database and the written notes the analyst reasons from.

    python research.py SPGI            build and print a summary; saves out/SPGI_facts.json

Sources, most trusted first: the company's tagged SEC figures (company facts, and the
inline tags of the latest 10-Q and 10-K, which carry segments), the latest earnings
release's tables (guidance, and a quarter filed before its 10-Q), then Yahoo Finance
(market data; statements only for companies that do not file with the SEC). The notes
are the 10-K's business and risk sections, the latest filing's MD&A, the release's text,
serious 8-K events, insider trades and recent news.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import config
from . import derive, filing_tables
import gather
from . import market
import market_data
from . import release
import research_tools
import sec_xbrl
from . import xbrl
from .facts_db import FactDB
from market_data import MarketData
from .periods import Period

log = logging.getLogger(__name__)

COMPANY_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ANNUAL_FORMS = ("10-K", "20-F", "40-F")
PERIODIC = ("10-Q",) + ANNUAL_FORMS
MDNA_CHARS = 12000
# A 20-F's operating review (its MD&A), business and risk sections.
OPERATING_REVIEW = re.compile(r"Item\s*5\.?\s*[-–—:]?\s*Operating\s+and\s+Financial\s+Review",
                              re.I)
TWENTY_F_BUSINESS = re.compile(r"^\s*Item\s*4\.?\s*[-–—:]?\s*Information\s+on\s+the\s+"
                               r"Company\b", re.I | re.M)
TWENTY_F_AFTER_BUSINESS = re.compile(r"^\s*Item\s*4A\.?|^\s*Item\s*5\.?\s*[-–—:]?\s*Operating",
                                     re.I | re.M)
TWENTY_F_RISKS = re.compile(r"^\s*(?:D\.|Item\s*3\.?D\.?)\s*Risk\s+Factors\b", re.I | re.M)
TWENTY_F_AFTER_RISKS = re.compile(r"^\s*Item\s*4\.?\s*[-–—:]?\s*Information\s+on", re.I | re.M)
RELEASE_CHARS = 8000


@dataclass
class Note:
    kind: str           # "10-K business", "10-K risks", "MD&A", "release", "events", "news"...
    title: str
    url: str
    text: str
    primary: bool = True    # the company's own filing or release, not second-hand

    def to_json(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Filing:
    form: str
    filed: str
    report_date: str
    accession: str
    url: str
    items: str = ""

    def label(self, db: FactDB) -> str:
        """"10-Q for 2Q26, filed 2026-07-28"."""
        what = self.form
        if self.report_date:
            from datetime import date
            span = "12M" if self.form in ANNUAL_FORMS else "3M"
            p = Period(span, date.fromisoformat(self.report_date), db.fy_end)
            what = f"{self.form} for {p.label}"
        return f"{what}, filed {self.filed}"


@dataclass
class Research:
    db: FactDB
    data: MarketData
    notes: list[Note] = field(default_factory=list)
    filings: list[Filing] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"facts": self.db.to_json(), "notes": [n.to_json() for n in self.notes],
                "filings": [f.__dict__ for f in self.filings], "timings": self.timings}


class Timer:
    def __init__(self):
        self.times: dict[str, float] = {}

    def run(self, name: str, fn, *args, **kwargs):
        start = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            self.times[name] = round(time.monotonic() - start, 1)


# ---------------------------------------------------------------- EDGAR

def edgar_filings(cik: int) -> list[Filing]:
    """The company's recent filings, newest first."""
    r = gather._sec_get(research_tools.SEC_SUBMISSIONS_URL.format(cik=cik)).json()
    recent = r["filings"]["recent"]
    n = len(recent["form"])
    out = []
    for i in range(n):
        accession = recent["accessionNumber"][i]
        base = research_tools.SEC_ARCHIVE.format(cik=cik, accession=accession.replace("-", ""))
        out.append(Filing(recent["form"][i], recent["filingDate"][i],
                          (recent.get("reportDate") or [""] * n)[i] or "", accession,
                          base + recent["primaryDocument"][i],
                          (recent.get("items") or [""] * n)[i] or ""))
    return out


def pick_filings(filings: list[Filing]) -> dict[str, Filing]:
    """The latest 10-Q or annual report, the latest annual report, and the latest earnings
    release (an 8-K under item 2.02)."""
    out: dict[str, Filing] = {}
    for f in filings:
        if "periodic" not in out and f.form in PERIODIC:
            out["periodic"] = f
        if "annual" not in out and f.form in ANNUAL_FORMS:
            out["annual"] = f
        if "release" not in out and f.form == "8-K" and "2.02" in f.items:
            out["release"] = f
    if out.get("annual") is out.get("periodic"):
        out.pop("annual", None)
    return out


def _foreign_quarter(cik: int, filings: list[Filing], chosen: dict[str, Filing]) -> None:
    """A foreign issuer's quarter, from its 6-Ks (research_tools.foreign_quarterlies): the
    results release, whose tables become facts, and the financial report as the periodic
    filing, its 20-F kept as the annual one."""
    rows = [(f.form, f.filed, f.accession, f.url.rsplit("/", 1)[-1], f.report_date)
            for f in filings]
    found = research_tools.foreign_quarterlies(cik, rows)
    by_accession = {f.accession: f for f in filings}
    for key, name in (("release", "release"), ("interim", "periodic")):
        if key not in found:
            continue
        filed, accession, url = found[key]
        f = by_accession[accession]
        if name == "periodic" and "periodic" in chosen:
            chosen.setdefault("annual", chosen["periodic"])
        chosen[name] = Filing(f.form, filed, f.report_date, accession, url, f.items)


def _gather_rows(filings: list[Filing]) -> list[dict]:
    """The filings in the row shape gather.py reads."""
    return [{"form": f.form, "date": f.filed, "items": f.items, "url": f.url} for f in filings]


def _html(url: str) -> str:
    return gather._sec_get(url).text


# A large filer's 10-K and 10-Q each take ~100 MB to parse; three parses at once put GS
# over Render's 512 MB on 2026-10-13. Downloads still run in parallel, parses one at a time.
PARSE_LOCK = threading.Lock()


def _locked(fn, *args):
    with PARSE_LOCK:
        return fn(*args)


def _text_of(html: str) -> str:
    with PARSE_LOCK:
        return _parse_text(html)


def _parse_text(html: str) -> str:
    return research_tools.page_text(html)[1]


def _mdna(text: str) -> str:
    """The filing's MD&A (a 20-F's operating review), opened at its results of operations."""
    heads = [m.start() for m in research_tools.MDNA_HEADING.finditer(text)] or \
        [m.start() for m in OPERATING_REVIEW.finditer(text)]
    if not heads:
        return text[:MDNA_CHARS]
    start = heads[min(1, len(heads) - 1)]
    results = research_tools.RESULTS_HEADING.search(text, start,
                                                    start + research_tools.RESULTS_SEARCH_CHARS)
    begin = results.start() if results else start
    return text[begin:begin + MDNA_CHARS]


# ---------------------------------------------------------------- build

def build(query: str, data: MarketData | None = None, news: bool = True) -> Research:
    timer = Timer()
    data = data or timer.run("market data", market_data.fetch, query)
    symbol = data.symbol
    db = FactDB(symbol, entity=data.name, currency=data.reporting_currency,
                trading_currency=data.currency)
    research = Research(db, data)

    cik = None
    if config.SEC_USER_AGENT:
        try:
            cik = sec_xbrl.cik_for(symbol)
        except Exception as exc:
            db.gaps.append(f"EDGAR unreachable: {exc}")
    else:
        db.gaps.append("SEC_USER_AGENT is not set: no SEC filings were read")

    with ThreadPoolExecutor(max_workers=6) as pool:
        closes_job = pool.submit(timer.run, "price history", market.price_history, symbol)
        news_job = (pool.submit(timer.run, "news", gather.news, data.name or symbol, symbol)
                    if news else None)
        if cik is None:
            if config.SEC_USER_AGENT:
                db.gaps.append(f"{symbol} does not file with the SEC: statements from Yahoo")
            frames_job = pool.submit(timer.run, "Yahoo statements", market._frames, symbol)
        else:
            facts_job = pool.submit(timer.run, "SEC company facts", lambda: gather._sec_get(
                COMPANY_FACTS.format(cik=cik)).json())
            filings = timer.run("SEC filing list", edgar_filings, cik)
            research.filings = filings
            chosen = pick_filings(filings)
            if not any(f.form in ("8-K", "10-Q") for f in filings):
                timer.run("SEC 6-K results", _foreign_quarter, cik, filings, chosen)
            html_jobs = {k: pool.submit(timer.run, f"SEC {k}", _html, f.url)
                         for k, f in chosen.items() if k != "release"}
            if "release" in chosen:
                r = chosen["release"]
                base = research_tools.SEC_ARCHIVE.format(cik=cik,
                                                         accession=r.accession.replace("-", ""))
                # A 6-K's release was found by its document already (_foreign_quarter).
                html_jobs["release"] = pool.submit(timer.run, "SEC release", lambda: _html(
                    r.url if r.form == "6-K"
                    else research_tools._earnings_exhibit(base, r.accession) or r.url))
            extras_job = pool.submit(timer.run, "10-K sections, events, insiders",
                                     lambda: (_locked(gather.ten_k_sections,
                                                      _gather_rows(filings)),
                                              gather.red_8k(_gather_rows(filings)),
                                              gather.insiders(_gather_rows(filings))))

        closes, splits = closes_job.result()
        if cik is None:
            market.yahoo_statements(db, symbol, frames_job.result(), set_fy_end=True)
        else:
            try:
                company_facts = facts_job.result()
                if xbrl.restatements(company_facts):
                    company_facts = None  # not two copies of a large filer's facts at once
                    # A figure that moved by a fifth between filings is more often a bad
                    # response than a restatement: ask once more before excluding it.
                    company_facts = timer.run("SEC company facts, again", lambda: gather._sec_get(
                        COMPANY_FACTS.format(cik=cik)).json())
                xbrl.from_company_facts(db, company_facts, cik)
            except Exception as exc:
                db.gaps.append(f"SEC company facts unavailable: {exc}")
            if db.currency != data.reporting_currency:
                db.gaps.append(f"filings report in {db.currency}, Yahoo says "
                               f"{data.reporting_currency}")
            # The annual report before the newer 10-Q, so the 10-Q's figures are the latest.
            for key in ("annual", "periodic"):
                job = html_jobs.get(key)
                if job is None:
                    continue
                f = chosen[key]
                try:
                    html = job.result()
                except Exception as exc:
                    db.gaps.append(f"could not read the {f.form} filed {f.filed}: {exc}")
                    continue
                with PARSE_LOCK:
                    timer.run(f"parse {key}", xbrl.from_inline, db, html, f.label(db),
                              f.filed, f.url)
                if f.form == "6-K":
                    # A foreign issuer's financial report is plain HTML, not tagged: its
                    # statements are read as tables, like a release's (SPOT's 2Q26 report).
                    timer.run("parse 6-K statements", lambda: release.from_release(
                        db, filing_tables.tables(html), f"financial report ({f.label(db)})",
                        f.filed, f.url))
                text = _text_of(html)
                if f.form in ("20-F", "40-F"):
                    for kind, head, end, limit in (
                            ("Item 4, Information on the Company", TWENTY_F_BUSINESS,
                             TWENTY_F_AFTER_BUSINESS, gather.BUSINESS_CHARS),
                            ("Item 3.D, Risk Factors", TWENTY_F_RISKS, TWENTY_F_AFTER_RISKS,
                             gather.RISKS_CHARS)):
                        found = gather.section(text, head, end)
                        if found:
                            research.notes.append(Note(
                                "annual report", f"{f.label(db)}, {kind}", f.url,
                                gather._clip(found, limit)))
                if key == "periodic":
                    research.notes.append(Note("MD&A", f"MD&A of the {f.label(db)}", f.url,
                                               _mdna(text)))
                    deal = research_tools.deal_terms(text)
                    if deal:
                        research.notes.append(Note("deal terms", "Deal terms, from the notes "
                                                   "to the financial statements", f.url, deal))
            if "release" in html_jobs:
                f = chosen["release"]
                try:
                    html = html_jobs["release"].result()
                    label = f"earnings release ({f.form} filed {f.filed})"
                    timer.run("parse release", lambda: release.from_release(
                        db, filing_tables.tables(html), label, f.filed, f.url))
                    research.notes.append(Note("release", label, f.url,
                                               _text_of(html)[:RELEASE_CHARS]))
                except Exception as exc:
                    db.gaps.append(f"could not read the earnings release: {exc}")
            try:
                sections, flags, insider = extras_job.result()
                for kind, title, text in sections:
                    research.notes.append(Note("10-K", title, kind, text))
                events = ("Serious 8-K items, last 180 days:\n" + "\n".join(flags)) if flags \
                    else "No 8-K reporting an impairment, auditor change, restatement or " \
                         "bankruptcy in the last 180 days."
                research.notes.append(Note("events", "8-K events", "", events))
                if insider:
                    research.notes.append(Note("insiders", "Form 4 insider trades", "", insider))
            except Exception as exc:
                db.gaps.append(f"10-K sections and events unavailable: {exc}")
        if news_job:
            try:
                for _, url, text in news_job.result() or []:
                    research.notes.append(Note("news", url, url, text, primary=False))
            except Exception as exc:
                db.gaps.append(f"news unavailable: {exc}")

    timer.run("market facts", market.market_facts, db, data)
    if cik is not None and not any(f.form == "10-Q" for f in research.filings[:40]):
        # A foreign filer: its 20-F is annual and its quarterly results (6-K) are not
        # tagged, so Yahoo fills the quarters the 6-K report did not give, and anything else
        # the 20-F left empty. The filings' figures were added first and are kept.
        n = timer.run("Yahoo statements", market.yahoo_statements, db, symbol)
        if n:
            db.gaps.append(f"no 10-Q: {n} figures (mostly quarters) come from Yahoo's "
                           "statements, the least trusted source")
    timer.run("calculations", derive.derive_all, db, closes, data.fx, splits)
    research.timings = timer.times
    return research


def summary(r: Research) -> str:
    db = r.db
    kinds: dict[str, int] = {}
    for f in db.facts.values():
        kinds[f.data_type] = kinds.get(f.data_type, 0) + 1
    key = ["share_price", "market_cap", "enterprise_value", "ev_to_ebitda", "pe", "fcf_yield",
           "net_debt_to_ebitda", "ev_to_ebitda_hist_low", "ev_to_ebitda_hist_median",
           "ev_to_ebitda_hist_high", "ev_to_revenue_hist_low", "ev_to_revenue_hist_median",
           "ev_to_revenue_hist_high", "pe_hist_median", "street_target_mean", "beta",
           "risk_free_rate"]
    for metric in ("revenue", "operating_margin", "fcf", "revenue_growth", "roic"):
        for span in ("12M", "TTM", "3M"):
            f = db.latest(metric, span)
            if f:
                key.append(f.id)
    lines = [f"{db.entity} ({db.symbol}), reporting in {db.currency}, fiscal year to month "
             f"{db.fy_end}", f"{len(db.facts)} facts: {kinds}",
             f"{len(db.conflicts)} conflicts, {sum(not f.usable for f in db.facts.values())} "
             f"excluded", "", db.table([k for k in key if k in db.facts]), ""]
    lines.append("Segments: " + json.dumps(db.segments()))
    lines.append("Notes: " + ", ".join(f"{n.kind} ({len(n.text)} chars)" for n in r.notes))
    lines.append("Gaps: " + ("; ".join(db.gaps) or "none"))
    lines.append("Timings (s): " + json.dumps(r.timings))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if len(argv) < 2:
        print(__doc__)
        return 2
    started = time.monotonic()
    r = build(argv[1])
    print(summary(r))
    print(f"\nTotal {time.monotonic() - started:.0f}s")
    config.OUT_DIR.mkdir(exist_ok=True)
    path = config.OUT_DIR / f"{r.db.symbol}_facts.json"
    path.write_text(json.dumps(r.to_json(), indent=1, default=str), encoding="utf-8")
    print(f"Saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
