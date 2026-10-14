"""Client-side research tools for the model.

NVIDIA NIM has no server-side search, so the bot supplies its own: Tavily (with
TAVILY_API_KEY) or the keyless ddgs engines for discovery, and a plain fetcher for
reading a page. Both are deliberately forgiving — a failed lookup returns a message the
model can react to rather than an exception that kills a long generation.
"""

from __future__ import annotations

import logging
import re
import warnings
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from ddgs import DDGS

import config
import filing_tables

log = logging.getLogger("stockbot.research")

# SEC's inline-XBRL filings open with an XML declaration. The HTML parser reads them
# fine; without this bs4 warns on every one.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

TAVILY_URL = "https://api.tavily.com/search"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
FETCH_TIMEOUT = 20
MAX_PAGE_CHARS = 3000
# A company's own release or IR page is where the reported figures are. At 3,000
# characters the model saw an earnings release's headline bullets and none of the
# segment paragraphs, which is where NEE's backlog and merger terms were.
PRIMARY_PAGE_CHARS = 8000
MAX_RESULTS = 6
# A 10-Q or 10-K runs to 200k+ characters, and its opening pages are the cover and the
# contents. Filings get a larger budget, starting at MD&A.
SEC_PAGE_CHARS = 12000
MDNA_HEADING = re.compile(r"Item\s*[27]\.?\s*Management.s\s+Discussion\s+and\s+Analysis", re.I)
# MD&A opens with pages of overview; the figures start here. Found 15-21k characters in
# for RTX and NVIDIA; Apple has no such heading, and its figures sit near the top anyway.
RESULTS_HEADING = re.compile(r"^(?:consolidated\s+)?results\s+of\s+operations$", re.I | re.M)
RESULTS_SEARCH_CHARS = 40000
HIDDEN = re.compile(r"display\s*:\s*none", re.I)

STRIP_TAGS = ["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]

# Social posts, document dumps and Q&A sites. The NVDA pitch of 2026-09-06 cited Facebook
# posts, a Scribd upload and LinkedIn for figures its filings carried first-hand.
EXCLUDED_DOMAINS = [
    "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com", "linkedin.com",
    "pinterest.com", "reddit.com", "quora.com", "scribd.com", "slideshare.net",
    "youtube.com", "medium.com", "pornhub.com", "xvideos.com", "xnxx.com",
    "redtube.com", "youporn.com", "xhamster.com", "spankbang.com",
]

# Where a company's own numbers are published: regulators' filing systems, the wire
# services that carry earnings releases, and investor-relations sites.
PRIMARY_DOMAINS = [
    "sec.gov", "businesswire.com", "prnewswire.com", "globenewswire.com",
    "hkexnews.hk", "sedarplus.ca", "asx.com.au", "londonstockexchange.com", "sgx.com",
    "tdnet.info", "dart.fss.or.kr", "mops.twse.com.tw", "annualreports.com",
]
# News, data and commentary sites. A page on one of these is never primary, whatever its
# host prefix or path says (news.yahoo.com, seekingalpha.com/.../investor-relations).
SECONDARY_DOMAINS = [
    "yahoo.com", "reuters.com", "bloomberg.com", "cnbc.com", "marketwatch.com", "wsj.com",
    "ft.com", "barrons.com", "seekingalpha.com", "fool.com", "investing.com", "msn.com",
    "nasdaq.com", "zacks.com", "morningstar.com", "benzinga.com", "marketbeat.com",
    "tipranks.com", "simplywall.st", "stockanalysis.com", "macrotrends.net", "google.com",
    "wikipedia.org", "forbes.com", "businessinsider.com", "investopedia.com", "nytimes.com",
    "theguardian.com", "bbc.co.uk", "bbc.com", "cnn.com", "techcrunch.com", "theverge.com",
]
PRIMARY_HOST_PREFIXES = ("investor.", "investors.", "ir.", "newsroom.", "news.", "press.")
PRIMARY_PATH = re.compile(r"/(?:investors?|investor-relations|ir|press-releases?|newsroom|"
                          r"financial-results|annual-reports?|quarterly-results)(?:/|$|\.)", re.I)

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current information. Use this for market sizing, "
                "competitor data, recent quarterly results, industry reports, analyst "
                "commentary, and regulatory news. Returns titles, URLs and snippets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query. Be specific and include the year.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "Fetch and read the text of a web page found via web_search. Use this "
                "when a snippet is not enough and you need the underlying detail, such "
                "as an earnings release, an SEC filing or an industry report. A 10-Q or "
                "10-K on sec.gov opens at its MD&A section."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to fetch."}
                },
                "required": ["url"],
            },
        },
    },
]


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _on(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_excluded(url: str) -> bool:
    return _on(_host(url), EXCLUDED_DOMAINS)


_QUERY_STOP = {"about", "after", "against", "from", "into", "latest", "quarter", "stock",
               "ticker", "what", "when", "with", "year", "company", "results", "news"}


def _matches_query(query: str, item: dict) -> bool:
    """Drop search-engine drift that shares no useful term with the requested subject."""
    terms = {t.casefold() for t in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", query)
             if t.casefold() not in _QUERY_STOP}
    if not terms:
        return True
    title = (item.get("title") or "").casefold()
    body = (item.get("body") or "").casefold()
    # A title match is strong; allow one descriptive query term in the snippet too,
    # since issuer names and tickers are often absent from result titles.
    return any(re.search(rf"\b{re.escape(term)}\b", title) for term in terms) or any(
        re.search(rf"\b{re.escape(term)}\b", body) for term in terms)


def is_primary(url: str) -> bool:
    """A filing, an earnings release, or the company's investor-relations site."""
    host = _host(url)
    if _on(host, SECONDARY_DOMAINS) or _on(host, EXCLUDED_DOMAINS):
        return False
    labels = host.split(".")
    # A company's own newsroom named after itself: nvidianews.nvidia.com.
    own_newsroom = (len(labels) >= 3 and labels[0].startswith(labels[-2])
                    and labels[0].endswith(("news", "ir", "investors")))
    return (_on(host, PRIMARY_DOMAINS) or host.startswith(PRIMARY_HOST_PREFIXES)
            or own_newsroom or bool(PRIMARY_PATH.search(urlparse(url).path or "")))


class SourceLog:
    """Tracks what the model looked at, for the PDF appendix."""

    def __init__(self) -> None:
        self._sources: dict[str, str] = {}
        self._primary_read_urls: set[str] = set()
        self.searches = 0
        self.fetches = 0
        self.primary_fetches = 0   # filings and releases actually read, not just found

    def primary_read(self, url: str) -> None:
        """Count distinct primary documents actually fetched, not repeated URL reads."""
        parsed = urlparse(url)
        key = f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}{parsed.path.rstrip('/')}"
        if parsed.port:
            key = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
        if key not in self._primary_read_urls:
            self._primary_read_urls.add(key)
            self.primary_fetches += 1

    def add(self, url: str, title: str) -> None:
        if url and url not in self._sources:
            self._sources[url] = title or url

    @property
    def sources(self) -> list[dict]:
        return [{"url": u, "title": t} for u, t in self._sources.items()]


def _tavily_search(query: str) -> list[dict]:
    """Search through Tavily, reshaped to the keys ddgs returns (title, href, body)."""
    response = requests.post(
        TAVILY_URL,
        headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"},
        # basic costs 1 credit. Left unset, auto_parameters may pick advanced at 2.
        json={"query": query, "max_results": MAX_RESULTS, "search_depth": "basic",
              "exclude_domains": EXCLUDED_DOMAINS},
        timeout=FETCH_TIMEOUT,
    )
    response.raise_for_status()
    return [
        {"title": item.get("title"), "href": item.get("url"), "body": item.get("content")}
        for item in response.json().get("results") or []
    ]


def web_search(query: str, log_to: SourceLog | None = None) -> str:
    """Run a search and render the results as text for the model.

    Tavily when a key is set; the keyless engines otherwise, and also whenever Tavily
    fails — out of monthly credits, a bad key, an outage — so that degrades the
    research rather than cutting it off.
    """
    if query.strip().startswith(("http://", "https://")):
        # Seen for RTX: a data.sec.gov URL sent as a query, spending a Tavily credit.
        return "That is a URL, not a search query. Read it with fetch_page instead."

    results: list[dict] = []
    if config.TAVILY_API_KEY:
        try:
            results = _tavily_search(query)
        except Exception as exc:
            log.warning("Tavily search failed for %r (%s) — trying keyless engines",
                        query, exc)

    if not results:
        try:
            results = list(DDGS().text(query, max_results=MAX_RESULTS) or [])
        except Exception as exc:
            log.warning("search failed for %r: %s", query, exc)
            return f"Search failed: {exc}. Try a different query."

    # Tavily honours the exclusion itself; the keyless engines do not.
    results = [r for r in results
               if not is_excluded(r.get("href") or r.get("url") or "")
               and _matches_query(query, r)]
    if not results:
        return f"No results for '{query}'. Try different wording."

    if log_to:
        log_to.searches += 1

    lines = [f"Results for '{query}':", ""]
    for n, item in enumerate(results, 1):
        title = (item.get("title") or "").strip()
        url = (item.get("href") or item.get("url") or "").strip()
        body = re.sub(r"\s+", " ", (item.get("body") or "")).strip()
        tag = " [primary source]" if is_primary(url) else ""
        lines.append(f"{n}. {title}{tag}\n   {url}\n   {body}")
        if log_to:
            log_to.add(url, title)
    return "\n".join(lines)


def _is_sec(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "sec.gov" or host.endswith(".sec.gov")


def fetch_page(url: str, log_to: SourceLog | None = None) -> str:
    """Fetch a page and return readable text, truncated."""
    page = _fetch_text(url, log_to)
    return page if isinstance(page, str) else _excerpt(url, *page)


def _fetch_text(url: str, log_to: SourceLog | None = None) -> tuple[str, str] | str:
    """(title, full readable text) of a page, or a message saying why there is none."""
    page = _fetch_html(url)
    if isinstance(page, str):
        return page
    title, text = page_text(page[0])
    title = title or url
    _logged(url, title, log_to)
    return title, text


def _fetch_soup(url: str, log_to: SourceLog | None = None) -> tuple[str, BeautifulSoup] | str:
    """(title, parsed page with scripts and hidden XBRL data removed), or a message saying
    why there is none."""
    page = _fetch_html(url)
    if isinstance(page, str):
        return page
    soup = BeautifulSoup(page[0], "lxml")
    for tag in soup(STRIP_TAGS):
        tag.decompose()
    # Inline-XBRL filings carry ~50k characters of hidden tagging data ahead of the
    # cover page. A reader never sees it, and it was all the model got.
    for tag in soup.find_all(style=HIDDEN):
        tag.decompose()
    title = (soup.title.string if soup.title and soup.title.string else url).strip()
    _logged(url, title, log_to)
    return title, soup


def _logged(url: str, title: str, log_to: SourceLog | None) -> None:
    if log_to:
        log_to.fetches += 1
        if is_primary(url):
            log_to.primary_read(url)
        log_to.add(url, title)


_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "td", "th", "table", "title",
               "h1", "h2", "h3", "h4", "h5", "h6"}


class _TextTarget:
    """lxml parser target: a page's readable text, collected as the parser streams past,
    without STRIP_TAGS or hidden elements. A tree of GS's 10-K took ~90 MB in BeautifulSoup;
    this takes ~20 MB, and two or three such trees ran a free Render instance out of its
    512 MB (2026-10-13)."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.title: list[str] = []
        self._hidden: list[bool] = []
        self._skip = 0
        self._in_title = False

    def start(self, tag, attrib):
        tag = tag.lower()
        hide = tag in STRIP_TAGS or bool(HIDDEN.search(attrib.get("style") or ""))
        self._hidden.append(hide)
        self._skip += hide
        self._in_title = tag == "title"
        if tag in _BLOCK_TAGS:
            self.out.append("\n")

    def end(self, tag):
        if self._hidden:
            self._skip -= self._hidden.pop()
        self._in_title = False
        if tag.lower() in _BLOCK_TAGS:
            self.out.append("\n")

    def data(self, text):
        if self._in_title:
            self.title.append(text)
        elif not self._skip:
            self.out.append(text)

    def close(self):
        return self


def page_text(html: str) -> tuple[str, str]:
    """(title, readable text) of an HTML page, one line per block, streamed: it never builds
    the page's tree."""
    from lxml import etree
    parser = etree.HTMLParser(target=_TextTarget(), recover=True, huge_tree=True)
    step = 1 << 20
    for i in range(0, len(html), step):
        parser.feed(html[i:i + step])
    done = parser.close()
    lines = (re.sub(r"\s+", " ", line).strip() for line in "".join(done.out).split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines if line))
    # "$ 102 million" from inline spans reads as a count, not money.
    text = re.sub(r"([$€£¥])\s+(?=\d)", r"\1", text)
    return " ".join("".join(done.title).split()), text


def _fetch_html(url: str) -> tuple[str] | str:
    """(the page's HTML,), or a message saying why there is none."""
    if not url.startswith(("http://", "https://")):
        return "Invalid URL: must start with http:// or https://"
    if is_excluded(url):
        return (f"Skipped {url}: social posts and document dumps are not citable sources. "
                "Find the filing, the company release or a named publication instead.")

    sec = _is_sec(url)
    if sec and not config.SEC_USER_AGENT:
        return (f"Skipped {url}: sec.gov refuses unidentified clients and SEC_USER_AGENT "
                "is not set. Use the company's earnings release or press coverage instead.")

    try:
        response = requests.get(
            url, timeout=FETCH_TIMEOUT,
            headers={"User-Agent": config.SEC_USER_AGENT if sec else USER_AGENT},
        )
        response.raise_for_status()
    except Exception as exc:
        log.warning("fetch failed for %s: %s", url, exc)
        return f"Could not fetch {url}: {exc}"

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return f"Skipped {url}: unsupported content type ({content_type})."
    return (response.text,)


def _excerpt(url: str, title: str, text: str) -> str:
    """The part of a page the model reads: a filing from its MD&A, anything else its top."""
    limit = PRIMARY_PAGE_CHARS if is_primary(url) else MAX_PAGE_CHARS
    if _is_sec(url):
        limit = SEC_PAGE_CHARS
        # The heading's first mention is in the table of contents; the second starts
        # the section itself.
        heads = [m.start() for m in MDNA_HEADING.finditer(text)]
        if heads:
            start = heads[min(1, len(heads) - 1)]
            results = RESULTS_HEADING.search(text, start, start + RESULTS_SEARCH_CHARS)
            if results:
                text = "[opened at MD&A, Results of Operations]\n" + text[results.start():]
            else:
                text = "[opened at MD&A]\n" + text[start:]
    if len(text) > limit:
        text = text[:limit] + "\n\n[truncated]"
    return f"# {title}\n{url}\n\n{text}"


# --------------------------------------------------------------------------- EDGAR

# The company's latest filings, read by the code before research starts rather than left
# to the model to find. Two NEE pitches of 2026-09-24/26 disagreed on the NEER backlog,
# the Dominion merger's date and its exchange ratio (both got the ratio wrong): every one
# of those figures is in the Q2 earnings release and 10-Q, and neither run used them.
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"
# 20-F and 40-F are left out: their operating review has no heading fetch_page can open
# at, and their first pages are the cover and contents.
PERIODIC_FORMS = ("10-Q", "10-K")
EARNINGS_ITEM = "2.02"   # 8-K item "Results of Operations and Financial Condition"
# A deal's price is set in the notes, which the MD&A excerpt never reaches: NEE's 10-Q
# gave the Dominion exchange ratio (0.8138 NEE shares plus a $360mn cash pool) in Note 5,
# 60k characters before its MD&A. The pricing sentence is found by its wording, and kept
# only when a merger or acquisition is named around it (stock awards use "right to
# receive" too). The cover, contents and risk factors are skipped.
DEAL_PRICE = re.compile(r"right to receive|exchange ratio|aggregate purchase price|"
                        r"purchase price of|per share in cash", re.I)
DEAL_WORDS = re.compile(r"merger|acqui|business combination|divest", re.I)
MERGER_WORDS = re.compile(r"merger|business combination", re.I)
STOCK_TERMS = {"right to receive", "exchange ratio"}
DEAL_SKIP_CHARS = 30000
DEAL_CONTEXT = 1500
DEAL_EXCERPT_CHARS = 3000

_ciks: dict[str, int] | None = None


@dataclass
class Filing:
    label: str      # "earnings release (8-K exhibit 99) filed 2026-07-24"
    url: str
    periodic: bool = False      # a 10-Q or 10-K, whose notes carry any deal terms


def deal_terms(text: str) -> str | None:
    """The notes' paragraph that prices a pending merger or acquisition, if there is one.

    A merger outranks an acquisition: NEE's notes price a closed gas-business purchase
    (Symmetry) ahead of the Dominion merger that moves the valuation.
    """
    best, best_rank = None, 0
    for m in DEAL_PRICE.finditer(text, DEAL_SKIP_CHARS):
        around = text[max(0, m.start() - DEAL_CONTEXT):m.end() + DEAL_CONTEXT]
        if not DEAL_WORDS.search(around):
            continue
        # A merger over an acquisition, then the consideration itself ("right to
        # receive 0.8138 shares") over a purchase price mentioned in passing.
        rank = (2 if MERGER_WORDS.search(around) else 1) * 2 + (m.group(0).lower() in STOCK_TERMS)
        if rank > best_rank:
            best, best_rank = m.start(), rank
    if best is None:
        return None
    # Back to the start of the paragraph's opening sentences, not into the note before.
    start = max(0, best - DEAL_EXCERPT_CHARS // 6)
    return text[start:start + DEAL_EXCERPT_CHARS]


def _sec_get(url: str):
    response = requests.get(url, timeout=FETCH_TIMEOUT,
                            headers={"User-Agent": config.SEC_USER_AGENT})
    response.raise_for_status()
    return response


def cik_for(symbol: str) -> int | None:
    """The SEC's company number for a ticker, or None for a company that does not file."""
    global _ciks
    if _ciks is None:
        rows = _sec_get(SEC_TICKERS_URL).json().values()
        _ciks = {str(r["ticker"]).upper(): int(r["cik_str"]) for r in rows}
    # Yahoo writes BRK-B, the SEC BRK-B too; a listing suffix (UMG.AS) is not a US filer.
    return _ciks.get(symbol.upper())


def _earnings_exhibit(base: str, accession: str) -> str | None:
    """The EX-99 document of an 8-K: the release itself, not the two-line cover form."""
    page = _sec_get(f"{base}{accession}-index.htm").text
    soup = BeautifulSoup(page, "lxml")
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        link = row.find("a", href=True)
        if link and any(c.get_text(strip=True).upper().startswith("EX-99") for c in cells):
            href = link["href"].replace("/ix?doc=", "")
            return href if href.startswith("http") else "https://www.sec.gov" + href
    return None


# A foreign private issuer (SPOT, NOK) files no 8-K or 10-Q: its quarter comes as 6-Ks, one
# whose cover says it furnishes the results (the release or shareholder deck as EX-99) and,
# for some, one that is the interim report itself, its statements tagged in XBRL (EX-101).
# SPOT's of 2026-10-07 found neither, fell back to web research, and its writer typed the
# deck's figures (33.4% gross margin, EUR 4.89 ARPU) as no source the checks could match.
FOREIGN_RELEASE = re.compile(r"results for the (?:\w+ )?(?:quarter|three months|six months|"
                             r"half[- ]year|year)|(?:quarterly|financial|operating) results|"
                             r"reports? (?:\w+ ){0,3}(?:quarter|Q[1-4])|earnings release|"
                             r"\bQ[1-4] \d{4} (?:results|update)\b|"
                             r"reports? (?:adjusted )?(?:operating )?profit|"
                             r"raises? (?:its )?(?:full[- ]year|annual) outlook", re.I)
FOREIGN_SCAN = 12       # the newest 6-Ks examined, one to three requests each
# NVO files a 6-K for most press releases: on 2026-10-11 its 2Q26 report (dated 06-30, filed
# 08-04) was the 13th 6-K, and the pitch read only the 20-F. 6-Ks dated to a quarter end are
# also examined this far back, since they are the interim reports and releases.
FOREIGN_DATED_SCAN = 60
# An interim report is the whole quarter's statements: SPOT's 6-K for 2Q26 is 1.3MB of
# plain HTML, TSM's an EX-99.1 of 4.8MB; their covers and monthly-revenue 6-Ks are under 100kB.
INTERIM_BYTES = 200_000
QUARTER_ENDS = ("03-31", "06-30", "09-30", "12-31")
RELEASE_CHARS = 4000    # how far into an exhibit its headline is looked for


def foreign_quarterlies(cik: int, rows) -> dict[str, tuple[str, str, str]]:
    """The latest results release and interim report among a foreign issuer's 6-Ks, as
    {"release": (filed, accession, url), "interim": (filed, accession, url)}. `rows` are
    (form, filed, accession, primary document, report date), newest first.

    The newest FOREIGN_SCAN are looked at. One dated to a quarter end it was filed after,
    with a document of INTERIM_BYTES or more, is the interim report (SPOT, TSM). A release is a
    6-K whose cover, or for a quarter-dated one its exhibit's opening, says it gives the
    results; the release is its EX-99 (SPOT's shareholder deck), or the 6-K itself when it
    has none (NOK files its results report as the 6-K)."""
    found: dict[str, tuple[str, str, str]] = {}
    sixes = [r for r in rows if r[0] == "6-K"]

    def dated(r) -> bool:
        return (r[4] or "")[5:] in QUARTER_ENDS and r[4] != r[1]


    def text(url: str, chars: int | None = None) -> str:
        try:
            page = BeautifulSoup(_sec_get(url).text, "lxml").get_text(" ")
        except Exception as exc:
            log.warning("could not read %s: %s", url, exc)
            return ""
        return page[:chars] if chars else page

    scan = sixes[:FOREIGN_SCAN] + [r for r in sixes[FOREIGN_SCAN:FOREIGN_DATED_SCAN] if dated(r)]
    for row in scan:
        if len(found) == 2:
            break
        form, filed, accession, document, report = row
        base = SEC_ARCHIVE.format(cik=cik, accession=accession.replace("-", ""))
        try:
            soup = BeautifulSoup(_sec_get(f"{base}{accession}-index.htm").text, "lxml")
        except Exception as exc:
            log.warning("could not read the 6-K index %s: %s", accession, exc)
            continue
        # (description, document, type, size) for each listed document
        docs = [[c.get_text(strip=True) for c in tr.find_all("td")][1:5]
                for tr in soup.find_all("tr")]
        docs = [d for d in docs if len(d) == 4 and d[1]]
        own = [d for d in docs if d[2].upper() == "6-K" or d[2].upper().startswith("EX-99")]
        exhibits = [d for d in own if d[2].upper().startswith("EX-99")]
        biggest = max(own, key=lambda d: int(d[3]) if d[3].isdigit() else 0, default=None)
        report_sized = bool(biggest and biggest[3].isdigit() and int(biggest[3]) >= INTERIM_BYTES)
        if dated(row) and report_sized:
            # An older quarter's report is not a release either: SPOT's 1Q26 report says
            # "results for the three months" too.
            found.setdefault("interim", (filed, accession, base + biggest[1]))
        if "release" in found:
            continue
        release = base + (exhibits[0][1] if exhibits else document)
        if FOREIGN_RELEASE.search(text(base + document)) or (
                exhibits and dated(row) and FOREIGN_RELEASE.search(text(release, RELEASE_CHARS))):
            found["release"] = (filed, accession, release)
    return found


def latest_filings(symbol: str) -> list[Filing]:
    """Latest earnings release and financial reports, including foreign issuer filings."""
    import sec_xbrl
    cik = sec_xbrl.cik_for(symbol)
    if cik is None:
        return []
    recent = _sec_get(SEC_SUBMISSIONS_URL.format(cik=cik)).json()["filings"]["recent"]
    n = len(recent["form"])
    rows = list(zip(recent["form"], recent["filingDate"], recent["accessionNumber"],
                    recent["primaryDocument"], recent.get("items") or [""] * n))
    # Foreign private issuers publish quarterly results on 6-K, while their annual
    # 20-F is also in the recent submissions list. The presence of that annual filing
    # must not suppress the quarter path. Domestic quarterly earnings use 8-K/10-Q.
    if not any(r[0] in ("8-K", "10-Q") for r in rows):
        quarter = foreign_quarterlies(cik, [r[:4] + (d,) for r, d in zip(
            rows, recent.get("reportDate") or [""] * n)])
        out = []
        if "release" in quarter:
            filed, _, url = quarter["release"]
            out.append(Filing(f"latest results release (6-K), filed {filed}", url))
        if "interim" in quarter:
            filed, _, url = quarter["interim"]
            out.append(Filing(f"latest financial report (6-K), filed {filed}", url, periodic=True))
        annual = next((r for r in rows if r[0] in ("20-F", "40-F")), None)
        if annual:
            form, filed, accession, document, _ = annual
            base = SEC_ARCHIVE.format(cik=cik, accession=accession.replace("-", ""))
            out.append(Filing(f"latest annual report ({form}), filed {filed}",
                              base + document, periodic=True))
        return out
    release = periodic = None
    for form, filed, accession, document, items in rows:
        base = SEC_ARCHIVE.format(cik=cik, accession=accession.replace("-", ""))
        if periodic is None and form in PERIODIC_FORMS:
            periodic = Filing(f"latest {form}, filed {filed}", base + document, periodic=True)
        if release is None and form == "8-K" and EARNINGS_ITEM in (items or ""):
            exhibit = _earnings_exhibit(base, accession)
            if exhibit:
                release = Filing(f"latest earnings release (8-K exhibit 99), filed {filed}",
                                 exhibit)
        if release and periodic:
            break
    return [f for f in (release, periodic) if f]


def read_latest_filings(symbol: str, log_to: SourceLog | None = None,
                        with_tables: bool = False) -> list[tuple[Filing, str, str]]:
    """Fetch the latest release and 10-Q/10-K, as (filing, excerpt, tables). The tables
    (filing_tables.py) only `with_tables`, else "". Empty without SEC_USER_AGENT or on any
    failure."""
    if not config.SEC_USER_AGENT:
        return []
    try:
        filings = latest_filings(symbol)
    except Exception as exc:
        log.warning("EDGAR lookup failed for %s: %s", symbol, exc)
        return []
    read = []
    for filing in filings:
        page = _fetch_soup(filing.url, log_to)
        if isinstance(page, str):
            log.warning("could not read %s for %s: %s", filing.label, symbol, page[:160])
            continue
        title, soup = page
        tables = ""
        if with_tables:
            try:
                tables = filing_tables.render(filing_tables.tables(soup),
                                              config.FILING_TABLE_CHARS)
            except Exception as exc:       # a table the reader cannot parse costs the tables only
                log.warning("could not read the tables of %s: %s", filing.label, exc)
        full = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
        text = _excerpt(filing.url, title, full)
        deal = deal_terms(full) if filing.periodic else None
        if deal:
            text += "\n\n[deal terms, from the notes to the financial statements]\n" + deal
        read.append((filing, f"[{filing.label}]\n{text}", tables))
    log.info("EDGAR: read %d filing(s) for %s: %s", len(read), symbol,
             "; ".join(f.label for f, *_ in read) or "none")
    return read


def dispatch(name: str, args: dict, log_to: SourceLog | None = None) -> str:
    """Route a tool call from the model to its implementation."""
    if name == "web_search":
        return web_search(str(args.get("query", "")), log_to)
    if name == "fetch_page":
        return fetch_page(str(args.get("url", "")), log_to)
    return f"Unknown tool: {name}"
