"""Research gathered by code (RESEARCH_MODE=gather): the filings, the 10-K's business and
risk sections, the news, and the SEC's event and insider filings, read in parallel with no
model calls; then one short model call for the searches that fill what is still missing.

The research loop (pitch._research) runs up to 16 model rounds, each resending the whole
conversation: about 20 minutes a pitch against 3-5 a draft. Across the 20 replays saved by
2026-10-05, the EDGAR filings the code read before the loop were 25-40% of the notes; the
loop added mostly news and blog pages (10-50%) and search snippets (up to 60%), and its
investor-relations fetches often came back empty. That second-hand material is what
the facts layer tries to keep out of the figures.

`gather` returns what pitch._research returns, (digest, sources), so the write is unchanged.
News filters and the 8-K and Form 4 readers are ported from Stock Finder V1's research.py
rather than imported across repos.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import json
import logging
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import requests

import config
import research_tools
import sec_xbrl

log = logging.getLogger("stockbot.gather")


class NoFilings(Exception):
    """EDGAR gave nothing to build on: the research loop does better from the web."""


# --------------------------------------------------------------------------- which path

def loop_reason(symbol: str) -> str | None:
    """Why this symbol needs the research loop, or None when gather can run. Companies that
    do not file with the SEC (ESLT, ASML, SPOT file 20-F/6-K or nothing) leave gather with
    no filings to put first."""
    if not config.SEC_USER_AGENT:
        return "SEC_USER_AGENT is not set, so EDGAR cannot be read"
    try:
        if research_tools.cik_for(symbol) is None:
            return f"{symbol} does not file with the SEC"
    except Exception as exc:
        return f"EDGAR is unreachable ({str(exc)[:120]})"
    return None


# --------------------------------------------------------------------------- EDGAR

SUBMISSIONS_DAYS = 180
# 5.02 (officer changes) and 3.01 (listing notices) are left out on purpose: in large caps
# they are mostly routine appointments and listing transfers (Stock Finder V1).
RED_8K = {"4.02": "non-reliance on prior financials (restatement)",
          "2.06": "material impairment", "1.03": "bankruptcy or receivership",
          "4.01": "change of auditor"}
FORM4_MAX = 40
INSIDER_LINES = 8

# Item 1 and 1A of the 10-K, found by their headings. The first mention is the table of
# contents; the section is the longest span from a heading to the next item's.
BUSINESS_HEAD = re.compile(r"^\s*Item\s*1\s*\.?\s*[-–—:]?\s*Business\b", re.I | re.M)
RISKS_HEAD = re.compile(r"^\s*Item\s*1A\s*\.?\s*[-–—:]?\s*Risk\s+Factors\b", re.I | re.M)
AFTER_RISKS = re.compile(r"^\s*Item\s*(?:1B|1C|2)\s*\.?\s*[-–—:]?\s*"
                         r"(?:Unresolved|Cybersecurity|Properties)\b", re.I | re.M)
BUSINESS_CHARS = 9000
RISKS_CHARS = 7000

_sec_lock = threading.Lock()
_sec_last = [0.0]


def _sec_get(url: str):
    """research_tools._sec_get under the SEC's 10 requests a second: Form 4s come 40 at a
    time."""
    with _sec_lock:
        wait = 0.12 - (time.monotonic() - _sec_last[0])
        if wait > 0:
            time.sleep(wait)
        _sec_last[0] = time.monotonic()
    return research_tools._sec_get(url)


def submissions(symbol: str) -> list[dict]:
    """The company's recent filings, newest first: form, date, items, url."""
    import sec_xbrl
    cik = sec_xbrl.cik_for(symbol)
    if cik is None:
        return []
    r = _sec_get(research_tools.SEC_SUBMISSIONS_URL.format(cik=cik)).json()["filings"]["recent"]
    n = len(r["form"])
    items = r.get("items") or [""] * n
    return [{"form": r["form"][i], "date": r["filingDate"][i], "items": items[i] or "",
             "url": research_tools.SEC_ARCHIVE.format(
                 cik=cik, accession=r["accessionNumber"][i].replace("-", ""))
             + r["primaryDocument"][i]}
            for i in range(n)]


def section(text: str, head: re.Pattern, end: re.Pattern) -> str:
    """The longest span from a heading to the next item's heading, or ""."""
    best = ""
    for m in head.finditer(text):
        stop = end.search(text, m.end())
        span = text[m.start():stop.start() if stop else len(text)]
        if len(span) > len(best):
            best = span
    return best.strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n\n[truncated]"


def ten_k_sections(rows: list[dict], log_to=None) -> list[tuple[str, str, str]]:
    """Item 1 (Business: segments, competitors) and Item 1A (Risk Factors: regulation, the
    bear case) of the latest 10-K, as findings; for a foreign issuer, Item 4 (Information on
    the Company) and Item 3.D (Risk Factors) of its 20-F or 40-F. The 10-Q the filings step
    reads has neither."""
    from data_pipeline import research as pipeline

    annual = next((r for r in rows if r["form"] in ("10-K", "20-F", "40-F")), None)
    if annual is None:
        return []
    page = research_tools._fetch_text(annual["url"], log_to)
    if isinstance(page, str):
        log.warning("could not read the %s: %s", annual["form"], page[:160])
        return []
    _, text = page
    form = annual["form"]
    if form == "10-K":
        parts = (("Item 1 Business", BUSINESS_HEAD, RISKS_HEAD, BUSINESS_CHARS),
                 ("Item 1A Risk Factors", RISKS_HEAD, AFTER_RISKS, RISKS_CHARS))
    else:
        parts = (("Item 4 Information on the Company", pipeline.TWENTY_F_BUSINESS,
                  pipeline.TWENTY_F_AFTER_BUSINESS, BUSINESS_CHARS),
                 ("Item 3.D Risk Factors", pipeline.TWENTY_F_RISKS,
                  pipeline.TWENTY_F_AFTER_RISKS, RISKS_CHARS))
    out = []
    for title, head, end, limit in parts:
        found = section(text, head, end)
        if found:
            out.append(("filing_section", f"{form} filed {annual['date']}, {title}",
                        f"{annual['url']}\n\n{_clip(found, limit)}"))
    return out


def red_8k(rows: list[dict], log_to=None) -> list[str]:
    """Serious 8-K items in the last SUBMISSIONS_DAYS: impairment, auditor change,
    restatement, bankruptcy."""
    cutoff = (dt.date.today() - dt.timedelta(days=SUBMISSIONS_DAYS)).isoformat()
    out = []
    for f in rows:
        if f["form"] != "8-K" or f["date"] < cutoff:
            continue
        for code in f["items"].split(","):
            if code.strip() in RED_8K:
                out.append(f"- {f['date']} 8-K item {code.strip()}: {RED_8K[code.strip()]} "
                           f"({f['url']})")
                if log_to:
                    log_to.add(f["url"], f"8-K filed {f['date']}: {RED_8K[code.strip()]}")
    return out


def _x(node, path: str) -> str:
    x = node.find(path)
    return (x.text or "").strip() if x is not None and x.text else ""


def insiders(rows: list[dict], log_to=None) -> str:
    """Open-market buys and sells on Form 4 in the last SUBMISSIONS_DAYS. Grants and option
    exercises are left out: they are pay, not a view on the price."""
    cutoff = (dt.date.today() - dt.timedelta(days=SUBMISSIONS_DAYS)).isoformat()
    forms = [f for f in rows if f["form"] == "4" and f["date"] >= cutoff][:FORM4_MAX]
    if not forms:
        return ""
    buys = sells = unplanned = 0.0
    buyers, sellers = set(), set()
    trades = []
    for f in forms:
        # primaryDocument is the XSL-rendered view; the raw XML is one level up.
        try:
            root = ET.fromstring(_sec_get(re.sub(r"/xslF345X\d+/", "/", f["url"])).content)
        except Exception:
            continue
        who = _x(root, "reportingOwner/reportingOwnerId/rptOwnerName")
        role = _x(root, "reportingOwner/reportingOwnerRelationship/officerTitle")
        plan = _x(root, "aff10b5One") in ("1", "true")
        for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
            code = _x(tx, "transactionCoding/transactionCode")
            if code not in ("P", "S"):
                continue
            try:
                shares = float(_x(tx, "transactionAmounts/transactionShares/value") or 0)
                price = float(_x(tx, "transactionAmounts/transactionPricePerShare/value") or 0)
            except ValueError:
                continue
            usd = shares * price
            if code == "P":
                buys += usd
                buyers.add(who)
            else:
                sells += usd
                sellers.add(who)
                unplanned += 0 if plan else usd
            trades.append((usd, f["date"], who, role, "bought" if code == "P" else "sold",
                           shares, price, plan, f["url"]))
    if not trades:
        return (f"No open-market buys or sells on the {len(forms)} Form 4 filings of the last "
                f"{SUBMISSIONS_DAYS} days.")
    lines = [f"Form 4 open-market transactions, last {SUBMISSIONS_DAYS} days ({len(forms)} "
             f"filings read): buys USD {buys:,.0f} by {len(buyers)} insider(s); sells USD "
             f"{sells:,.0f} by {len(sellers)} insider(s), USD {unplanned:,.0f} of them outside "
             "10b5-1 plans.", "", "Largest:"]
    for usd, date, who, role, verb, shares, price, plan, url in sorted(trades, reverse=True)[
            :INSIDER_LINES]:
        lines.append(f"- {date} {who}{f' ({role})' if role else ''} {verb} {shares:,.0f} "
                     f"shares at USD {price:,.2f} (USD {usd:,.0f})"
                     f"{', 10b5-1 plan' if plan else ''} — {url}")
        if log_to:
            log_to.add(url, f"Form 4 filed {date}: {who} {verb} shares")
    return "\n".join(lines)


# --------------------------------------------------------------------------- news

NEWS_DAYS = 90
NEWS_HEADLINES = 20
NEWS_ARTICLES = 6           # article texts read; more candidates are tried, since some fail
NEWS_CANDIDATES = 12
NEWS_MIN_CHARS = 600        # less is a cookie wall or a paywall stub
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?"
GOOGLE_DECODE = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
BROWSER = {"User-Agent": "Mozilla/5.0"}

# Headlines that say nothing about the business: quote pages, daily price-move write-ups,
# fund 13F holdings notices, chart-pattern spam (Stock Finder V1).
JUNK = re.compile("|".join([
    r"stock price, news, quote", r"latest stock news and headlines",
    r"\b(stock|shares)\s+(acquired|sold|bought|purchased)\s+by\b",       # 13F notices
    r"\b(stake|position|holdings)\s+in\b", r"\binvests \$[\d.,]+ (thousand|million|billion) in\b",
    r"latest sec filings", r"makes new( \$[\d.,]+ \w+)? investment in", r"new position in",
    r"receives (a )?consensus rating", r"\b[\d,]+-share (award|grant)\b",
    r"\breceived [\d,]+ shares\b", r"stock chart", r"stock forecasts?$",
    r"how much you would have made",
    r"\bstock moves\b", r"\b(moved|jumps?|falls?|rises?|drops?|climbs?|dips?|slides?)\b"
    r"( up| down)?( by)? [-+]?\d+(\.\d+)?\s?%",
    r"\b(broader market|than market|compared to competitors)\b",
    r"\b(under|out)performing the\b", r"what you (should|need to) know",
    r"unusually (large|high) options volume", r"trending stock", r"worth watching",
    r"risk zones", r"precision trading", r"trading (report|signals)",
]), re.I)
# Quote and profile pages that slip past the headline filter: in a probe for SPGI on
# 2026-10-05, Forbes's /companies/ page and TradingView's /symbols/ page were in the top four.
QUOTE_PAGE = re.compile(r"/(?:quotes?|symbols?|companies)/", re.I)


def short_name(name: str) -> str:
    return re.sub(r",?\s+(Inc|Corp|Corporation|Company|Co|Ltd|plc|L\.?P|Holdings|S\.A|N\.V)\.?$",
                  "", name, flags=re.I).strip()


def headlines(name: str, symbol: str) -> list[dict]:
    """Google News RSS for the last NEWS_DAYS, in Google's order (relevance), junk and
    repeats removed. Keyless; [] on any failure."""
    q = f'"{short_name(name)}" {symbol} stock when:{NEWS_DAYS}d'
    url = GOOGLE_NEWS_RSS + urllib.parse.urlencode({"q": q, "hl": "en-US", "gl": "US",
                                                    "ceid": "US:en"})
    try:
        r = requests.get(url, timeout=research_tools.FETCH_TIMEOUT, headers=BROWSER)
        r.raise_for_status()
        items = ET.fromstring(r.content).findall("channel/item")
    except Exception as exc:
        log.warning("Google News failed for %s: %s", symbol, exc)
        return []
    out, seen = [], set()
    for it in items:
        source = it.findtext("source") or ""
        title = (it.findtext("title") or "").removesuffix(f" - {source}").strip()
        key = re.sub(r"\W+", " ", title.lower())[:60]
        if not title or JUNK.search(title) or key in seen:
            continue
        seen.add(key)
        try:
            date = email.utils.parsedate_to_datetime(it.findtext("pubDate")).date().isoformat()
        except (TypeError, ValueError):
            date = ""
        site = it.find("source")
        out.append({"date": date, "source": source, "title": title,
                    "link": it.findtext("link") or "",
                    "site": site.get("url", "") if site is not None else ""})
    return out


# Google answers too many decodes with a 429 and a CAPTCHA page: on 2026-10-05 four
# replays gathering at once, six decodes each, were all refused, and so was every decode
# for some minutes after. One refusal stops decoding for this long; headlines are then
# found by searching for their title on the publisher's site.
GOOGLE_BACKOFF_S = 600
_google_blocked_until = [0.0]


def publisher_url(link: str) -> str | None:
    """The article behind a news.google.com/rss/articles/ link. Google no longer redirects
    there: the page carries a signature, and one more request trades it for the URL."""
    if time.time() < _google_blocked_until[0]:
        return None
    try:
        aid = urllib.parse.urlparse(link).path.rsplit("/", 1)[-1]
        response = requests.get(f"https://news.google.com/articles/{aid}", headers=BROWSER,
                                timeout=research_tools.FETCH_TIMEOUT)
        if response.status_code == 429:
            _google_blocked_until[0] = time.time() + GOOGLE_BACKOFF_S
            log.warning("Google News refused a decode (429): searching for headlines instead "
                        "for %.0f minutes", GOOGLE_BACKOFF_S / 60)
            return None
        page = response.text
        sg = re.search(r'data-n-a-sg="([^"]+)"', page).group(1)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page).group(1)
        inner = ["Fbv4je", f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
                           f'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,'
                           f'null,0],"{aid}",{ts},"{sg}"]']
        r = requests.post(GOOGLE_DECODE, data={"f.req": json.dumps([[inner]])},
                          headers=BROWSER | {"Content-Type":
                                             "application/x-www-form-urlencoded;charset=UTF-8"},
                          timeout=research_tools.FETCH_TIMEOUT)
        url = json.loads(json.loads(r.text.split("\n\n")[1])[0][2])[1]
        return url if isinstance(url, str) and url.startswith("http") else None
    except Exception as exc:
        log.info("could not decode a Google News link: %s", str(exc)[:120])
        return None


def searched_url(headline: dict) -> str | None:
    """The article found by searching for its title, kept only on the publisher's own site:
    the fallback when Google will not decode its links."""
    site = research_tools._host(headline.get("site") or "")
    if not site:
        return None
    query = f"{headline['title']} {site}"
    results: list[dict] = []
    if config.TAVILY_API_KEY:
        try:
            results = research_tools._tavily_search(query)
        except Exception as exc:
            log.info("Tavily could not find a headline: %s", str(exc)[:120])
    if not results:
        try:
            results = list(research_tools.DDGS().text(query, max_results=5) or [])
        except Exception as exc:
            log.info("search could not find a headline: %s", str(exc)[:120])
            return None
    for r in results:
        url = r.get("href") or r.get("url") or ""
        host = research_tools._host(url)
        if host == site or host.endswith("." + site):
            return url
    return None


def _resolve(headline: dict) -> str | None:
    return publisher_url(headline["link"]) or searched_url(headline)


def _article(url: str, log_to) -> str | None:
    """An article's excerpt, or None when it could not be read or is too thin to use."""
    page = research_tools._fetch_text(url, log_to)
    if isinstance(page, str) or len(page[1]) < NEWS_MIN_CHARS:
        return None
    return research_tools._excerpt(url, *page)


def news(name: str, symbol: str, log_to=None) -> list[tuple[str, str, str]]:
    """The headline list as snippets, and the text of the top NEWS_ARTICLES articles."""
    found = headlines(name, symbol)
    if not found:
        return []
    dated = sorted(found[:NEWS_HEADLINES], key=lambda h: h["date"], reverse=True)
    out = [("news_headlines", f"Google News, last {NEWS_DAYS} days",
            "\n".join(f"- {h['date']} {h['source']}: {h['title']}" for h in dated))]
    # Three at a time: six from each of several pitches at once is what Google refused.
    with ThreadPoolExecutor(max_workers=3) as pool:
        urls = list(pool.map(_resolve, found[:NEWS_CANDIDATES]))
        urls = [u for u in dict.fromkeys(u for u in urls if u)
                if not research_tools.is_excluded(u) and not QUOTE_PAGE.search(u)]
        texts = list(pool.map(lambda u: _article(u, log_to), urls))
    read = [(u, t) for u, t in zip(urls, texts) if t][:NEWS_ARTICLES]
    out += [("fetch_page", u, t) for u, t in read]
    return out


# --------------------------------------------------------------------------- gap-fill

GAP_SYSTEM = """You plan web searches to fill the gaps in research notes an analyst has \
already gathered for an investment pitch. You see a list of what the notes contain, not \
their text, and the checklist the research must cover. The company's own filings and \
releases cover its reported figures, segments, strategy and risks; what they usually lack \
is addressable market size and market share from named third-party sources, and figures \
for named competitors.

Reply with JSON only, no prose: {"queries": ["...", "..."]}. Make each query specific and \
include the year. Do not search for what the notes already cover."""


def checklist() -> str:
    """The research brief's checklist, so the gap-fill asks after the same things the loop
    was sent for."""
    import pitch as pitch_mod
    lines = pitch_mod.RESEARCH_BRIEF.split("establish:", 1)[-1].split("\n\n", 1)[0]
    return lines.strip()


def contents(findings: list[tuple[str, str, str]]) -> str:
    """What was gathered, as block titles and sizes: enough to judge the gaps, without the
    text, so the call stays short."""
    return "\n".join(f"- {name}: {arg[:140]} ({len(result):,} chars)"
                     for name, arg, result in findings)


def parse_queries(text: str, limit: int) -> list[str]:
    """Up to `limit` search queries from the model's reply; [] for anything unusable."""
    import pitch as pitch_mod
    text = pitch_mod._clean(text or "")
    decoder = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        found = obj.get("queries") if isinstance(obj, dict) else obj
        if not isinstance(found, list):
            continue
        out = []
        for q in found:
            q = q.strip() if isinstance(q, str) else ""
            if q and len(q) <= 200 and not q.startswith(("http://", "https://")) and q not in out:
                out.append(q)
        return out[:limit]
    return []


async def gap_queries(client, data, findings, usage) -> list[str]:
    """One short call down the research models' fallback order. [] when every model fails
    or none replies with usable JSON: the gap-fill is a bonus, never a reason to stop."""
    import pitch as pitch_mod
    n = config.GATHER_GAP_SEARCHES
    messages = [{"role": "system", "content": GAP_SYSTEM},
                {"role": "user", "content": (
                    f"Company: {data.display} ({data.symbol}). Today's date: "
                    f"{dt.date.today().isoformat()}.\n\nThe checklist:\n{checklist()}\n\n"
                    f"The notes so far:\n{contents(findings)}\n\nGive at most {n} queries.")}]
    candidates = pitch_mod._research_candidates()
    for model in candidates:
        usage["model"] = model
        limit = (config.RESEARCH_LAST_RESORT_S if model == candidates[-1]
                 else config.RESEARCH_FIRST_BYTE_S)
        try:
            text, _ = await asyncio.wait_for(
                pitch_mod._one_stream(client, messages, None, None, usage), timeout=limit)
        except Exception as exc:
            pitch_mod._note_quota(model, exc)
            log.warning("gap-fill call on %s failed (%s: %s)", model, type(exc).__name__,
                        str(exc)[:160])
            continue
        queries = parse_queries(text, n)
        if queries:
            return queries
        log.warning("gap-fill call on %s gave no usable queries: %r", model, text[:200])
    return []


RESULT_URL = re.compile(r"^\s+(https?://\S+)\s*$", re.M)


async def _timed_thread(fn, *args, timeout: float | None = None):
    """fn(*args) on a thread under TOOL_TIMEOUT_S; None when it runs over or fails. A hung
    keyless search held a whole research round for 11 minutes on 2026-09-23."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args),
                                      timeout=timeout or config.TOOL_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.warning("%s timed out after %.0fs", getattr(fn, "__name__", fn),
                    timeout or config.TOOL_TIMEOUT_S)
    except Exception as exc:
        log.warning("%s failed: %s", getattr(fn, "__name__", fn), str(exc)[:160])
    return None


async def gap_fill(queries: list[str], sources, fetched: set[str]) -> list[tuple[str, str, str]]:
    """Run the queries in one batch and read the top usable result of each."""
    results = await asyncio.gather(*(_timed_thread(research_tools.web_search, q, sources)
                                     for q in queries))
    findings, picks = [], []
    for q, result in zip(queries, results):
        if not result:
            continue
        findings.append(("web_search", q, result))
        url = next((u for u in RESULT_URL.findall(result)
                    if u not in fetched and not research_tools.is_excluded(u)), None)
        if url:
            fetched.add(url)
            picks.append(url)
    pages = await asyncio.gather(*(_timed_thread(research_tools.fetch_page, u, sources)
                                   for u in picks))
    findings += [("fetch_page", u, p) for u, p in zip(picks, pages)
                 if p and p.startswith("# ")]
    return findings


# --------------------------------------------------------------------------- gather

async def gather(client, data, on_progress, usage) -> tuple[str, list[dict]]:
    """The notes, built by code. Raises NoFilings when EDGAR had nothing to read, so the
    caller can fall back to the research loop."""
    import pitch as pitch_mod
    sources = research_tools.SourceLog()
    started = time.monotonic()

    def edgar_extras() -> tuple[list, list[str], str]:
        rows = submissions(data.symbol)
        # Run here in turn rather than in parallel: they share the SEC's rate limit.
        return ten_k_sections(rows, sources), red_8k(rows, sources), insiders(rows, sources)

    # SEC reads are slow from a cloud host; they get GATHER_SEC_TIMEOUT_S, not the tool limit.
    filings, xbrl, extras, found_news = await asyncio.gather(
        _timed_thread(research_tools.read_latest_filings, data.symbol, sources,
                      config.FACTS_MODE, timeout=config.GATHER_SEC_TIMEOUT_S),
        _timed_thread(sec_xbrl.read, data.symbol) if config.FACTS_MODE else asyncio.sleep(0),
        _timed_thread(edgar_extras, timeout=config.GATHER_SEC_TIMEOUT_S),
        _timed_thread(news, data.name or data.symbol, data.symbol, sources))
    if not filings:
        raise NoFilings(f"EDGAR returned no filings for {data.symbol}")

    findings: list[tuple[str, str, str]] = []
    fetched: set[str] = set()
    for filing, text, tables in filings:
        findings.append(("fetch_page", filing.url, text))
        if tables:
            findings.append(("filing_tables", filing.label, tables))
        fetched.add(filing.url)
    if xbrl:
        findings.append(("sec_xbrl", "company facts", xbrl))
    sections, flags, insider = extras or ([], [], "")
    findings += sections
    events = []
    if flags:
        events.append(f"Serious 8-K items, last {SUBMISSIONS_DAYS} days:\n" + "\n".join(flags))
    else:
        events.append(f"No 8-K reporting an impairment, auditor change, restatement or "
                      f"bankruptcy in the last {SUBMISSIONS_DAYS} days.")
    if insider:
        events.append(insider)
    if extras:
        findings.append(("sec_events", "8-K items and Form 4 insider trades", "\n\n".join(events)))
    for name, arg, result in found_news or []:
        findings.append((name, arg, result))
        fetched.add(arg)
    log.info("gather: %d blocks for %s in %.0fs", len(findings), data.symbol,
             time.monotonic() - started)

    if config.GATHER_GAP_SEARCHES > 0:
        queries = await gap_queries(client, data, findings, usage)
        log.info("gap-fill queries for %s: %s", data.symbol, queries or "none")
        if queries:
            findings += await gap_fill(queries, sources, fetched)
    if on_progress:
        await on_progress("searching", sources.searches)

    digest = pitch_mod._build_digest(findings)
    primary = sum(len(b) for b in re.split(r"\n\n(?=## \[)", digest)
                  if b.startswith("## [PRIMARY]"))
    log.info("gather done for %s: %d searches, %d fetches (%d primary), %d sources; notes "
             "%d chars, %.0f%% PRIMARY", data.symbol, sources.searches, sources.fetches,
             sources.primary_fetches, len(sources.sources), len(digest),
             100 * primary / max(1, len(digest)))
    usage["primary_sources_read"] = sources.primary_fetches
    return digest, sources.sources
