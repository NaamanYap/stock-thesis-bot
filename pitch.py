"""Generate the pitch document with a model on NVIDIA NIM, grounded in live research.

Two phases, because they need different context:

  1. Research — a lean system prompt and the tools. The model searches and reads. It
     does not need the style exemplar or the full financial statements to run a search,
     so neither is sent. Old tool results are truncated behind a sliding window.
  2. Write — one fresh call carrying the full analyst prompt, the exemplar, the market
     data and a digest of everything phase 1 found. The research transcript is sent
     once here rather than re-sent on every round. With TWO_PASS_WRITE the model first
     sends its `model` inputs alone; the code values them, and the prose is written in
     the same conversation with the computed numbers in hand (in facts mode the outline
     comes with the inputs). A second model reviews the first draft, and one re-ask
     carries its points and the code checks' problems together.

Every call after research waits its turn in a free-tier queue, so the write phase is
built for the fewest calls in a row: a plan, a draft, a review, at most one redraft.

The naive single-conversation version cost 307k prompt tokens for one pitch, ~70% of it
re-reading the same fetched pages. This shape cuts that by roughly half.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Awaitable, Callable

import httpx2  # the HTTP layer the openai SDK is built on
import openai

import claude_code
import conglomerate
import model_rules
import revenue_build
import config
import consistency
from data_pipeline import brief as structured_brief
from data_pipeline import research as structured_research
import facts
import gather
import research_tools
import sec_xbrl
import valuation
import valuation_route
import market_data
import figure_sheet
import outline
import provenance
import replay
from market_data import MarketData

log = logging.getLogger("stockbot.pitch")

ProgressFn = Callable[[str, int], Awaitable[None]]

SYSTEM_PROMPT = (config.PROMPTS_DIR / "analyst_system.md").read_text(encoding="utf-8")
METHOD_EXCESS_RETURNS = (config.PROMPTS_DIR / "method_excess_returns.md").read_text(encoding="utf-8")
METHOD_UTILITY_REIT = (config.PROMPTS_DIR / "method_utility_reit.md").read_text(encoding="utf-8")
EXEMPLAR = (config.PROMPTS_DIR / "exemplar.md").read_text(encoding="utf-8")
FACTS_RULES = (config.PROMPTS_DIR / "facts_rules.md").read_text(encoding="utf-8")
# Facts mode: the analyst prompt rewritten for a writer that cites every figure by name.
SYSTEM_PROMPT_FACTS = (config.PROMPTS_DIR / "analyst_system_facts.md").read_text(encoding="utf-8")

JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*", re.DOTALL)
# Any {{...}} at all, so a name the code does not know is reported rather than printed
# raw: the NFLX pitch of 2026-09-23 shipped "{{drivers.Global Subscribers:FY2030}}"
# because the old pattern only matched [a-z_]+. The unit the model typed around a value
# that already carries one ("USD {{tv}} million", "{{peer_pe}}x") is captured too, so
# it can be dropped instead of printing "USD USD 413.8bn million" or "9.4xx".
PLACEHOLDER = re.compile(
    r"(?P<pre>(?:US\$|\$|\b(?:USD|EUR|GBP|JPY|TWD|HKD|CNY|SGD|KRW|INR|CHF|CAD|AUD)\s?)\s*)?"
    r"\{\{\s*(?P<name>[^{}:]+?)\s*(?::\s*(?P<year>[^{}]+?))?\s*\}\}"
    r"(?P<post>x(?![A-Za-z])|%|\s?(?:million|billion|trillion|mn|bn|tn)\b|(?-i:[MBK])(?![A-Za-z&]))?",
    re.I)
HAS_CURRENCY = re.compile(r"^-?[A-Z]{3} ")
HAS_SCALE = re.compile(r"(?:mn|bn|tn)$")
H2 = re.compile(r"^##\s+(.+?)\s*$", re.M)
H3 = re.compile(r"^###\s+(.+?)\s*$", re.M)
GENERATED_TABLES = ("financial projections", "dcf build", "intrinsic valuation",
                    "sensitivity", "peer cross-check", "peer comparison", "peer multiples",
                    "valuation range", "market and volume", "financials", "condensed income",
                    "key metrics", "scenarios", "what the price", "returns on capital",
                    "leverage", "what would change", "thresholds")
# The writer's Definitions heading; the code's "1b. Definitions" replaces it.
DEFINITIONS_HEAD = re.compile(r"^#{3,4}\s*(?:1b\.\s*)?definitions\s*$\n?", re.I | re.M)
VERSUS_CONSENSUS = re.compile(r"versus consensus|vs\.? consensus", re.I)
RETURNS_LEAD = re.compile(r"\*\*\s*Returns on (?:invested )?capital\b", re.I)
LEVERAGE_LEAD = re.compile(r"\*\*\s*(?:Leverage|Balance sheet and leverage)\b", re.I)
RISKS_HEADING = "risks and downside case"
CONCLUSIONS_HEADING = "conclusions"
RATINGS = ("BUY", "HOLD", "SELL")
THINK_TAGS = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
# The write turn has no tools. A model that still wants to search writes its calls out as
# text instead of the pitch — seen for RTX: 'tool_code\nweb_search\n{"query": ...}'.
TOOL_CALL_TEXT = re.compile(r'\b(?:web_search|fetch_page)\s*(?:\(\s*)?\{?\s*"?(?:query|url)\b')

RESEARCH_BRIEF = """You are an equity research analyst gathering material for an \
investment pitch. You are NOT writing the pitch yet.

Use `web_search` and `fetch_page` to establish:
- the most recent reported quarter and its headline numbers, read from the company's own
  filing (10-Q/10-K, or the interim/annual report outside the US) or earnings release
- the business mix and revenue segments
- addressable market sizing from named third-party sources
- named competitors and relative market share
- management strategy, guidance and recent strategic moves
- the regulatory backdrop and the main bear arguments
- the operating measures that drive revenue (users, subscribers, units, price or revenue
  per unit, churn, retention, engagement, backlog) for the latest quarter and a year earlier,
  and management's stated long-term targets (investor day, shareholder letter)
- forward evidence, about as much effort as the financials: the market's size and growth
  by region with the company's share; an analogue further along (a mature market, a peer
  that already raised prices or reached scale margins) with its figures; what suppliers,
  customers and competitors are doing (their filings, renewals, lawsuits, pricing); and
  the bear case on each point, searched for deliberately
- material events inside the forecast horizon and their aftermath: for drug companies,
  product-level patent and regulatory-exclusivity expiry dates, pending challenges or
  generic entry, successor products, and expected changes to price, volume and margin.
  Prefer the company's filing and the relevant regulator's register. If the SEC filing
  lookup returns only an index or no filing for a non-US issuer, search the issuer's own
  investor-relations site and fetch the actual annual/interim report and latest results
  release; an index page does not substitute for reading those documents.
- if the company has a finance arm that lends to or leases for its customers (a Financial
  Products or Financial Services segment), that arm's total debt at the latest balance
  sheet date, from the filing's segment or supplemental data

Batch independent queries into a single turn — parallel calls are much faster than one
at a time. Fetch the page behind any number you would want to cite; a search snippet is
not a source. Do not fetch the same URL twice. Results marked [primary source] are
the company's own filings and releases: take reported figures from those, not from
sites that quote them. Social posts and document dumps are not citable.

When you have enough to build a driver-by-driver forecast and a DCF, stop calling tools and \
reply with the single word DONE. Do not write the pitch."""


# A bad request or a dead model will fail identically on every retry; load-shedding,
# rate limits and dropped connections will not. openai.APIError is the catch-all base:
# NIM raises it bare when it sheds load part-way through a stream.
FATAL_API_ERRORS = (
    openai.BadRequestError,
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.NotFoundError,
    *claude_code.FATAL_ERRORS,
)
TRANSIENT_API_ERRORS = (
    openai.RateLimitError,
    openai.InternalServerError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.APIError,
    # The SDK wraps a timeout while waiting for headers, but one that fires between
    # stream chunks reaches us as the raw httpx2 error.
    httpx2.TimeoutException,
    # Likewise a connection dropped mid-stream: a ReadError killed a live NVDA write on
    # 2026-09-23 after 25 minutes of research, because only timeouts were retried.
    httpx2.NetworkError,
    httpx2.RemoteProtocolError,
    *claude_code.TRANSIENT_ERRORS,
)


class PitchError(Exception):
    """Generation failed in a way worth showing the user."""


@dataclass
class Pitch:
    headline: dict
    body: str
    sources: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    valuation: valuation.Valuation | None = None
    # Problems the write turn was asked to fix and did not. Shown on the cover, so a
    # pitch that is still inconsistent is never presented as clean.
    checks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # High-severity reviewer issues still open, shown as Reviewer notes in the PDF.
    review: list[dict] = field(default_factory=list)
    review_status: str = "not run"
    # The review issues were raised on the first draft and the delivered one was asked to
    # address them, not left open.
    review_addressed: bool = False
    writer: str = ""                # the model that wrote the delivered text
    replay: dict = field(default_factory=dict)   # replay.record(): inputs and raw writes
    # provenance.audit of the body and of the cover, for the repair before delivery.
    flags: list = field(default_factory=list)
    cover_flags: list = field(default_factory=list)
    # What the repair did: sentences deleted for a wrong figure, (sentence, why), and how
    # many figures it marked with a dagger as found in no source.
    removed: list = field(default_factory=list)
    unverified: int = 0
    open_count: int = 0             # problems before the repair, to judge a revision by
    # What the audit read: the prose with placeholders filled, the cover, and the figures
    # they could cite. Kept for the replay ledger, which lists every figure's outcome.
    prose: str = ""
    cover: str = ""
    pool: object = None
    # Facts mode: every fact the text cites, by id, for the reviewer and the replay ledger.
    cited: dict = field(default_factory=dict)
    # Problems the code repairs in the draft that ships, by deleting the sentence: a typed
    # or misattributed figure. They are worth one re-ask, not a third draft.
    repairable: list = field(default_factory=list)
    # Earlier runs of the same ticker (history.py), shown on the cover.
    history: list = field(default_factory=list)
    # Checked argument map and research provenance, published as reader-facing appendices.
    outline: dict = field(default_factory=dict)
    source_conflicts: list[str] = field(default_factory=list)
    source_exclusions: list[str] = field(default_factory=list)
    source_gaps: list[str] = field(default_factory=list)
    data_reconciliations: str = ""
    source_check_available: bool = False
    # Why no rating is published, when the review left it unpublishable.
    withheld: str = ""
    # One entry per review round: what was found, by code, and how it was repaired.
    review_log: list = field(default_factory=list)

    def get(self, key: str, default=None):
        return self.headline.get(key, default)


def _masked_exemplar() -> str:
    """The exemplar with every figure shown as {{…}}: its register, structure and density
    of argument, without teaching a writer that cites by name to type numbers. Years stay."""
    out = EXEMPLAR
    for f in sorted(provenance.figures(EXEMPLAR), key=lambda f: f.start, reverse=True):
        out = out[:f.start] + "{{…}}" + out[f.end:]
    return out


def _write_system_message(cite_facts: bool = False) -> str:
    """Full analyst instructions plus the style exemplar — write phase only."""
    if cite_facts:
        return (
            SYSTEM_PROMPT_FACTS + FACTS_RULES
            + "\n\n# Style exemplar\n\n"
            "The document below is a pitch written to the standard you are being held to. "
            "Match its register, density, and structure. Do not reuse its facts, its "
            "industry, or its phrasing — only its craft. Its figures are shown as {{…}}: "
            "every figure of yours is a name from the figure sheet, a placeholder of your "
            "model or one of your assumptions.\n\n---\n\n"
            + _masked_exemplar()
        )
    return (
        SYSTEM_PROMPT
        + "\n\n# Style exemplar\n\n"
        "The document below is a pitch written to the standard you are being held to. "
        "Match its register, density, and structure. Do not reuse its facts, its "
        "industry, or its phrasing — only its craft. It was written before the model "
        "was computed in code, so it types its valuation figures and tables by hand: "
        "you use placeholders and the `model` object instead"
        + (", and it types the figures it takes from its sources, which you cite by id"
           if cite_facts else "") + ".\n\n---\n\n"
        + EXEMPLAR
    )


def _research_user_message(data: MarketData) -> str:
    """Just enough identity to search on. The statements come later."""
    # Without the date, models search for the quarter their training data ends in:
    # probed in September 2026, two of them asked for "2024 Q1" and "Q3 2025" results.
    lines = [
        f"Research {data.display} for an investment pitch.",
        "",
        f"Today's date: {date.today().isoformat()}. Search for the latest figures as of "
        "this date, not the latest ones you remember.",
        f"Ticker: {data.symbol}",
        f"Current price: {data.price:,.2f} {data.currency}",
    ]
    if data.market_cap:
        lines.append(f"Market cap: {data.market_cap:,.0f} {data.currency}")
    return "\n".join(lines)


def street_view(data: MarketData) -> list[tuple[str, str]]:
    """The Street's targets as (who, what) pairs: the average, then each named broker."""
    def vs_price(target: float) -> str:
        return valuation.pct((target / data.price - 1) * 100) if data.price else ""

    rows: list[tuple[str, str]] = []
    if data.analyst_target:
        count = f", {data.analyst_count} analysts" if data.analyst_count else ""
        rows.append(("Street average",
                     f"{data.currency} {data.analyst_target:,.2f} ({vs_price(data.analyst_target)}{count})"))
    if data.firm_targets is None:
        if config.STREET_FIRMS:
            rows.append((", ".join(config.STREET_FIRMS), "targets unavailable from the data source"))
        return rows
    covered = {market_data._firm_key(f.firm) for f in data.firm_targets}
    for f in data.firm_targets:
        grade = f", {f.grade}" if f.grade else ""
        value = (f"{data.currency} {f.target:,.2f} ({vs_price(f.target)}{grade}, {f.date})"
                 if f.target else f"no target{grade}, {f.date}")
        rows.append((f.firm, value))
    for firm in config.STREET_FIRMS:
        if market_data._firm_key(firm) not in covered:
            rows.append((firm, "no coverage in the last 12 months"))
    return rows


def _consensus_line(data: MarketData) -> str:
    if not data.analyst_target:
        head = "No sell-side consensus is available for this stock."
    else:
        rating = (data.analyst_rating or "n.a.").replace("_", " ")
        count = f" from {data.analyst_count} analysts" if data.analyst_count else ""
        gap = (data.analyst_target / data.price - 1) * 100 if data.price else 0
        head = (f"Sell-side consensus{count}: mean price target {data.analyst_target:,.2f} "
                f"{data.currency} ({valuation.pct(gap)} from the current price), rating '{rating}'.")
    firms = [f"{who}: {what}" for who, what in street_view(data) if who != "Street average"]
    return head + ("\nNamed brokers: " + "; ".join(firms) + "." if firms else "")


def _discount_rate_line(data: MarketData) -> str:
    """The house WACC, stated before the model builds anything around it."""
    route = valuation_route.route(data)
    equity = route.method == "equity"
    method_text = (METHOD_UTILITY_REIT if {"utility", "reit"} & set(route.flags)
                   else METHOD_EXCESS_RETURNS)
    if not config.HOUSE_WACC:
        if equity:
            return ("Discount rate: no discount rate is set for you. Give `cost_of_equity` "
                    "yourself, derived in `wacc_rationale`.\n\n" + method_text + "\n\n")
        return ("Discount rate: no discount rate is set for you. Give `wacc` yourself, "
                "derived in `wacc_rationale`.\n\n")
    cost = valuation.cost_of_capital(data)
    if equity:
        return (f"Discount rate: the code discounts at the cost of equity, "
                f"{cost.cost_of_equity:.1%} ({cost.risk_free:.1%} risk-free + {cost.beta:.2f} "
                f"beta x {cost.erp:.1%} ERP). Do not set `wacc` or `cost_of_equity`. If this "
                "company carries a risk its beta misses, set `wacc_premium` (at most "
                f"{config.WACC_PREMIUM_MAX:.0%}) and name the risk in `wacc_rationale`. Cite "
                "the rate as {{cost_of_equity}}.\n\n" + method_text + "\n\n")
    flags = valuation.distress_flags(data)
    distress = (f"This company is flagged {' and '.join(f.replace('_', '-') for f in flags)}: "
                "give `model.failure` and `model.nol` as the model fields describe.\n\n"
                if flags else "")
    return distress + (f"Discount rate: the code discounts at {cost.wacc:.1%} ({cost.describe()}). Do "
            "not set `wacc`. If this company carries a risk its beta misses, set "
            f"`wacc_premium` (at most {config.WACC_PREMIUM_MAX:.0%}) and name the risk in "
            "`wacc_rationale`; otherwise leave both out. Cite the rate as {{wacc}} and its "
            "build-up as {{wacc_build}}.\n\n")


def _write_user_message(data: MarketData, digest: str, market_block: str | None = None) -> str:
    route = valuation_route.route(data)
    valuation_limit = None if route.supported else route.reasons[0]
    return (
        f"Write the investment pitch for {data.display}.\n\n"
        + (f"Valuation limitation: {valuation_limit}. Write the business and investment "
           "brief, but do not invent a DCF value, price target, or rating. State clearly "
           "that valuation is unavailable in this version.\n\n" if valuation_limit else "")
        + f"The market data block is your authoritative source for reported figures and "
        f"the current share price of {data.price:,.2f} {data.currency}. The research "
        f"notes are what you gathered from the web — cite them, but the market data "
        f"block wins on any reported figure. Notes marked PRIMARY are the company's own "
        f"filings and releases; take reported figures from them before any SECONDARY "
        f"source. Notes labelled \"latest earnings release\" or \"latest 10-Q/10-K\" were "
        f"read from SEC EDGAR by the code: for everything they cover (reported results, "
        f"backlog, guidance, deal terms and dates) they override every other note, and a "
        f"SECONDARY figure that disagrees with them is wrong. Use a SECONDARY figure only "
        f"for what the company's own documents do not cover, and name its source.\n\n"
        f"{_consensus_line(data)}\n\n"
        f"{_discount_rate_line(data)}"
        f"{market_block if market_block is not None else data.prompt_block}\n\n"
        f"# Research notes\n\n{digest}"
    )


def _facts_user_message(data: MarketData, fx: facts.Facts) -> str:
    """Facts mode: the figure sheet in place of the market data block and the fact
    database, then the research notes as context."""
    route = valuation_route.route(data)
    valuation_limit = None if route.supported else route.reasons[0]
    return (
        f"Write the investment pitch for {data.display}.\n\n"
        + (f"Valuation limitation: {valuation_limit}. Write the business and investment "
           "brief, but do not invent a DCF value, price target, or rating. State clearly "
           "that valuation is unavailable in this version.\n\n" if valuation_limit else "")
        + "The figure sheet below holds every figure from the company's filings and the "
        f"market data you may state, the share price of {data.price:,.2f} {data.currency} "
        "among them, each under a name. The research notes after it are what was gathered "
        "from the web: use them for what they say, in words, except a figure the company "
        "states in a PRIMARY note, which you may type as it wrote it. Where the notes and the sheet "
        "disagree on a reported figure, the sheet is right.\n\n"
        f"{_consensus_line(data)}\n\n"
        f"{_discount_rate_line(data)}"
        f"{fx.sheet.render() if fx.sheet else fx.database}\n\n"
        f"# Research notes\n\n{fx.digest}"
    )


def _timed(usage: dict, stage: str, started: float) -> None:
    """How long a stage took, in the log and in the replay file. A pitch took over 15
    minutes and there was no record of where the time went (2026-10-05)."""
    seconds = time.monotonic() - started
    usage.setdefault("timings", []).append([stage, round(seconds)])
    log.info("%s took %.0fs", stage, seconds)


def _clean(text: str) -> str:
    """Drop reasoning-model scaffolding that leaks into the visible response."""
    if "\ufffd" in text:
        log.warning("model response contained Unicode replacement characters; rendering them "
                    "as em dashes")
        text = text.replace("\ufffd", "—")
    return THINK_TAGS.sub("", text).strip()


# Write calls, of which at most DRAFTS parse: the last is only for a draft that could not
# be parsed at all.
WRITE_ATTEMPTS = 4
# One draft, then one re-ask carrying the code's problems and the reviewer's points. On
# 2026-09-23/24 one re-ask took NFLX from 7 problems to 2 and AVGO from 9 to 4. Of the
# saved pitches that took a third draft by 2026-10-05, it changed nothing in five (NKE,
# RKLB, both SPGIs of 10-01/02, VST) and was worse in SPGI's of 10-05; only NOK's
# improved, by three deleted sentences. Each draft is three to five minutes.
# Since 2026-10-12 each repair is reviewed again, as the stock-thesis skill does (one per
# review round, config.REVIEW_ROUNDS); those NIM-era drafts were never re-read.
DRAFTS = 3


def _dump_bad_response(symbol: str, attempt: int, raw: str, usage: dict, exc: Exception) -> None:
    """Persist a response we could not parse.

    Without this the only trace of a failed 20-minute run was a one-line error in the
    user's chat: no way to tell a truncated reply from one that ignored the format.
    """
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.OUT_DIR / f"{symbol}_unparsed_attempt{attempt}.txt"
    try:
        path.write_text(raw, encoding="utf-8")
    except OSError:
        path = None
    log.error(
        "Unparsable write response for %s (attempt %d): %s | finish_reason=%s "
        "completion_tokens=%s chars=%d | saved to %s"
        "\nfirst 400 chars: %r",
        symbol, attempt, exc, usage.get("finish_reason", "?"),
        usage.get("completion_tokens"), len(raw), path, raw[:400],
    )


def _split_headline(text: str) -> tuple[dict, str]:
    """Peel the leading JSON block off the response."""
    match = JSON_FENCE.match(text)
    if match:
        try:
            headline = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise PitchError(f"The model's JSON header was malformed: {exc}") from exc
        body = text[match.end():]
    else:
        # Some models drop the fence or chat before it. The header nests (`model`), so
        # a regex cannot find its end; decode from each opening brace instead.
        headline, body = _loose_header(text)
    if not isinstance(headline, dict):
        raise PitchError("The model's JSON header was not an object.")

    body = body.strip()
    if body.startswith("```"):
        body = body.lstrip("`").lstrip()
    if not body:
        raise PitchError("The model returned a header but no pitch body.")
    return headline, body


def _loose_header(text: str) -> tuple[dict, str]:
    decoder = json.JSONDecoder()
    for start in (m.start() for m in re.finditer(r"\{", text)):
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("ticker" in obj or "model" in obj):
            return obj, text[end:].lstrip().removeprefix("```")
    raise PitchError("The model did not return the expected JSON header block.")


def _placeholder_key(name: str, year: str | None) -> str:
    name = name.strip().lower()
    for plural, singular in (("segments.", "segment."), ("drivers.", "driver.")):
        if name.startswith(plural):
            name = singular + name[len(plural):]
    return f"{name}:{valuation._norm_year(year)}" if year else name


# A count after an amount: SPOT's "{{segment_base.Ad-Supported}} million ad-supported MAUs"
# of 2026-10-11 printed "EUR 1.5bn ad-supported MAUs", the segment's revenue as its users.
COUNTED = re.compile(r"\s*(?:(?:million|billion|mn|bn)\s+)?"
                     r"(?!(?:from|of|per|across|in|by|to|for|with|among|and|or|at)\b)"
                     r"(?:[\w-]+\s+){0,2}?"
                     r"(?P<noun>MAUs?|DAUs?|users?|subscribers?|customers?|accounts?|"
                     r"members?|listeners?|employees?|households?|vehicles?|devices?)\b"
                     r"(?!\s+(?:revenues?|sales|ARPU|fees?|income|spend))", re.I)
MISSING = "⁇n.a."     # an unfillable placeholder, until its sentence is taken out
MISSING_WHY = "a figure it cites has no value in this model"


def _drop_missing(text: str) -> tuple[str, list[tuple[str, str]]]:
    """`text` without the prose sentences that cite a figure the model does not have, and
    those sentences with the reason. "capex at n.a. of revenue" and "ROIC to n.a." reached
    SPOT's PDF of 2026-10-11 when its rewrite was rejected. A heading or a table cell
    prints "n.a."."""
    removed = []
    for line in text.split("\n"):
        if MISSING not in line or line.lstrip().startswith(("#", "|")):
            continue
        for sentence in facts.SENTENCE_END.split(line):
            if MISSING in sentence:
                label = facts.LABEL.match(sentence)
                cut = sentence[label.end():] if label and label.group(1).strip() else sentence
                text, leaned = facts.drop_sentence(text, cut.strip())
                removed += [(cut.strip().replace(MISSING, "n.a."), MISSING_WHY)]
                removed += [(s, facts.LEANED_WHY) for s in leaned]
    return text.replace(MISSING, "n.a."), removed


def _fill_placeholders(body: str, values: dict[str, str] | None, missing: str = "n.a.",
                       misused: list[str] | None = None,
                       operands: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Swap {{name}} for computed values. Returns the body and any unknown names; a
    placeholder that has no value prints `missing`. With `misused`, an amount the words
    after it count as people or things is listed there and prints `missing` too."""
    lookup: dict[str, str] = {}
    for key, value in (values or {}).items():
        name, _, year = key.partition(":")
        lookup[_placeholder_key(name, year or None)] = value
    unknown: list[str] = []

    def computed(op: str, pair: str) -> str | None:
        """{{share:a/b}}, {{ratio:a/b}} or {{change:a/b}} over two of the model's values, as
        the fact sheet allows over two facts: SPOT's of 2026-10-11 wrote
        {{change:revenue:FY2026/revenue:FY2027}} six ways and each printed "n.a."."""
        parts = pair.split("/")
        if len(parts) != 2:
            return None
        found = []
        for part in parts:
            name, _, year = part.strip().partition(":")
            value = (lookup.get(_placeholder_key(name, year or None))
                     or (operands or {}).get(part.strip().lower()))
            figs = provenance.figures(value) if value and not value.startswith("-") else []
            if len(figs) != 1:
                return None
            found.append(figs[0])
        a, b = found
        if a.kind != b.kind or a.kind not in ("money", "count") or not a.value or not b.value:
            return None
        if op == "share":
            return facts._pct(a.value / b.value * 100)
        if op == "ratio":
            return f"{a.value / b.value:.1f}x"
        change = (b.value / a.value - 1) * 100
        return ("+" if change > 0 else "") + facts._pct(change)

    def swap(m: re.Match) -> str:
        pre, post = m.group("pre") or "", m.group("post") or ""
        key = _placeholder_key(m.group("name"), m.group("year"))
        placeholder = m.group(0)[len(pre):len(m.group(0)) - len(post)]
        op = m.group("name").strip().lower()
        if key not in lookup and op in ("share", "ratio", "change") and m.group("year"):
            value = computed(op, m.group("year"))
            if value is not None:
                lookup[key] = value
        if key in lookup:
            value = lookup[key]
            # A driver's value carries its unit ("50.0 GW"); prose that writes the unit
            # after the placeholder gets it once, not twice.
            number, _, word = value.partition(" ")
            if (key.startswith("driver.") and word and not HAS_CURRENCY.match(value)
                    and re.match(r"\s*" + re.escape(word) + r"\b", m.string[m.end():], re.I)):
                value = number
            counted = COUNTED.match(m.string[m.end():]) if misused is not None else None
            if counted and HAS_CURRENCY.match(value):
                misused.append(f"{placeholder} is an amount ({value}), but the words after it "
                               f"count {counted.group('noun')}: an amount cannot be a count. "
                               "Cite a driver for it, or describe it as revenue")
                value = missing
        else:
            unknown.append(placeholder)
            value = missing
        if value == missing:
            pre = post = ""
            return value
        if pre and (HAS_CURRENCY.match(value) or value == "n.a."):
            pre = ""
        unit = post.strip().lower()
        if (unit == "x" and (value.endswith("x") or value == "n.a.")
                or unit == "%" and (value.endswith("%") or value == "n.a.")
                or unit not in ("", "x", "%") and (HAS_SCALE.search(value) or value == "n.a.")):
            post = ""
        return pre + value + post

    return PLACEHOLDER.sub(swap, body), unknown


# Documented names the code leaves out when there is nothing to put in them. A model
# told only "unknown placeholder {{peer_pe}}" keeps it, because the instructions list
# it: NEE shipped on 2026-09-23 with the same five flags after its rewrite.
PER_YEAR_NAMES = {"revenue", "ebit", "ebitda", "ebit_margin", "fcf", "fcf_margin"}


def _missing_reason(key: str, v: valuation.Valuation, has_target: bool) -> str | None:
    """Why a documented placeholder has no value here, or None for a name that is not one."""
    name, _, year = key.partition(":")
    if name in PER_YEAR_NAMES or name.startswith(("segment.", "driver.")):
        if not year:
            return "it needs a forecast year, as in {{%s:%s}}" % (name, v.rows[-1].label)
        if name.startswith("segment."):
            known = ", ".join(v.segments) or "none"
            if not any(s.lower() == name[8:] for s in v.segments):
                return f"the model has no segment of that name (its segments: {known})"
        if name.startswith("driver."):
            known = ", ".join(v.drivers) or "none"
            if not any(d.lower() == name[7:] for d in v.drivers):
                return f"the model has no driver of that name (its drivers: {known})"
        return (f"{year} is not a forecast year; use one of "
                + ", ".join(r.label for r in v.rows))
    if name in ("peer_pe", "peer_ev_ebitda", "peer_value", "peer_upside"):
        if not v.peers:
            return "no peer multiples could be computed, so there is no peer cross-check"
        if name == "peer_pe":
            return "no peer has a usable P/E (losses or missing earnings)"
        if name == "peer_ev_ebitda":
            return "no peer has a usable EV/EBITDA"
        return "no peer-multiple value could be computed"
    if name in ("cross_check", "cross_check_upside"):
        return ("the model gives only one terminal method; a cross-check needs both "
                "`terminal.growth` and an exit multiple (`terminal.exit_ev_ebitda` or "
                "`terminal.exit_ev_ebit`)")
    if name in ("downside", "downside_upside"):
        return "`model.downside` is missing or could not be computed"
    if name == "tv_ev_ebitda":
        return "final-year EBITDA is not positive, so there is no multiple to state"
    if name == "tv_implied_growth":
        return ("it is the growth an exit multiple implies; the terminal value here is a "
                "perpetuity, so cite {{g}}" if v.method == "perpetuity"
                else "final-year free cash flow is not positive, so no growth rate is implied")
    if name == "ev_ebitda":
        return "market data has no positive trailing EBITDA"
    if name == "g":
        return "the terminal value uses an exit multiple and has no growth rate"
    if name in ("roic", "roic_ex_goodwill", "invested_capital"):
        if v.returns is None:
            return "no ROIC is computed here (no balance sheet, or a bank or insurer)"
        have = valuation.placeholders(v)
        years = [y.label for y in v.returns.years if f"{name}:{y.label}" in have]
        asked = v.returns.year(year) if year else None
        if name == "roic" and ((asked and asked.capital_near_zero) or (
                not years and any(y.capital_near_zero for y in v.returns.years))):
            return ((f"{year}'s " if asked else "") + "ROIC is not meaningful: invested capital "
                    "(equity plus debt less cash) is near zero against NOPAT, so the ratio "
                    "says nothing about what capital earns. Say that in words, citing "
                    "{{invested_capital:YEAR}}" + (", or use one of " + ", ".join(years)
                                                   if years else ""))
        if not years:
            return ("goodwill and intangibles are not in the market data, so there is no ROIC "
                    "excluding them" if name == "roic_ex_goodwill"
                    else "no year has positive invested capital")
        return ((f"{year} has none; use one of " if year else "it needs a year, one of ")
                + ", ".join(years))
    if name == "new_capital_return":
        return ("the terminal value assumes no growth, so no return on new capital is needed; "
                "cite {{terminal_roic}} if it is shown")
    if name in ("terminal_roic", "terminal_reinvestment"):
        return ("the terminal value assumes no growth, or reinvests nothing, so it implies no "
                "return on new capital" if v.returns else
                "no ROIC is computed here (no balance sheet, or a bank or insurer)")
    if name == "goodwill":
        return "the market data has no goodwill and intangibles line"
    if name == "minority_interest":
        return "the company reports no minority interests, so the bridge takes none off"
    if name == "net_cash" and v.leverage is not None:
        return "the company has more debt than cash: cite `{{net_debt}}`"
    if name in ("net_debt", "net_debt_to_ebitda", "interest_expense", "interest_coverage",
                "cost_of_debt", "earnings_yield", "buyback_amount", "buyback_eps_change"):
        if v.leverage is None:
            return "cash and debt are unavailable from market data"
        if name == "net_debt_to_ebitda":
            return "market data has no positive trailing EBITDA"
        if name in ("interest_expense", "interest_coverage"):
            return "the market data has no interest expense for the last four quarters"
        if name == "earnings_yield":
            return "market data has no trailing net income"
        return ("the company has no positive trailing net income or EBITDA, so borrowing "
                "to buy back stock has no EPS effect to state")
    if name in ("wacc_build", "beta", "cost_of_equity", "risk_free"):
        return "the discount rate here is the model's own `wacc`, with no house build-up"
    if name == "exit_multiple":
        return "the model sets no exit multiple"
    if name in ("consensus_target", "consensus_upside", "vs_consensus") and not has_target:
        return "market data has no sell-side target"
    return None


def _placeholder_problem(placeholder: str, v: valuation.Valuation | None,
                         has_target: bool) -> str:
    m = PLACEHOLDER.fullmatch(placeholder)
    key = _placeholder_key(m.group("name"), m.group("year")) if m else placeholder
    reason = _missing_reason(key, v, has_target) if v else None
    if reason:
        return (f"{placeholder} has no value in this pitch: {reason}. Remove the sentence "
                "or say the figure is not available; do not type a number in its place")
    return (f"unknown placeholder {placeholder}: it is not one of the names in the "
            "instructions. Use a listed name, or state an input you chose as a number")


def _insert_tables(body: str, tables: str, definitions: str = "",
                   thresholds: str = "") -> str:
    """Put the generated tables in Model Outputs, replacing any typed ones.

    The writer's own Definitions prose goes under the code's 1b heading, after the driver
    definitions, followed by `definitions` (the assumptions table); `thresholds` fills
    section 7.
    """
    def slotted(writer: str) -> str:
        writer = DEFINITIONS_HEAD.sub("", writer).strip()
        extra = "\n\n".join(x.strip() for x in (writer, definitions) if x.strip())
        if valuation.DEFINITIONS_SLOT not in tables:
            return "\n\n".join(x for x in (extra, tables) if x)
        return (tables.replace(valuation.DEFINITIONS_SLOT, extra)
                .replace(valuation.THRESHOLDS_SLOT, thresholds.strip()))

    heads = list(H2.finditer(body))
    section = next((h for h in heads if h.group(1).strip().lower() == "model outputs"), None)
    if section is None:
        return body.rstrip() + "\n\n## Model Outputs\n\n" + slotted("") + "\n"
    after = [h.start() for h in heads if h.start() > section.start()]
    end = after[0] if after else len(body)
    content = body[section.end():end]

    # A model that typed its own tables anyway: drop those subsections. The headings
    # vary ("Sensitivity Analysis", "Sensitivity (Intrinsic Price vs WACC & Exit
    # Multiple)"), so match on how they start.
    subs = list(H3.finditer(content))
    kept, cursor = [], 0
    for i, sub in enumerate(subs):
        stop = subs[i + 1].start() if i + 1 < len(subs) else len(content)
        if sub.group(1).strip().lower().startswith(GENERATED_TABLES):
            kept.append(content[cursor:sub.start()])
            cursor = stop
    kept.append(content[cursor:])
    # No table the model typed here survives, under whatever heading: ANF's of
    # 2026-09-25 was two pages of bare pipes, NEE's a grid of dashes.
    content = "\n".join(line for line in "".join(kept).splitlines()
                        if not line.lstrip().startswith("|")).rstrip()
    content = re.sub(r"\n{3,}", "\n\n", content)
    rest = body[end:].lstrip()
    return (body[:section.end()] + "\n\n" + slotted(content)
            + ("\n\n" + rest if rest else "")).rstrip() + "\n"


async def _peers_for(headline: dict, cache: dict, data: MarketData) -> list:
    """Multiples for the peers the model named, fetched once per list of tickers.

    A rewrite usually names the same peers, so the cache saves refetching them. Any
    failure or a slow Yahoo leaves the pitch without a peer check rather than without
    a pitch.
    """
    model = headline.get("model")
    key = peer_key(model)
    if not key:
        return []
    if key not in cache:
        tickers, stakes = _peer_tickers(model), _stake_tickers(model)

        def fetch() -> list:
            found = market_data.peer_multiples(tickers) if tickers else []
            for ticker in stakes:
                try:
                    quote = market_data.stake_quote(ticker, data.reporting_currency)
                except Exception as exc:
                    log.warning("listed stake %s unpriced: %s", ticker, exc)
                    quote = None
                if quote:
                    found.append(quote)
            return found
        try:
            cache[key] = await asyncio.wait_for(asyncio.to_thread(fetch),
                                                timeout=config.PEER_TIMEOUT_S)
        except Exception as exc:
            log.warning("peer multiples unavailable for %s: %s", ", ".join(key), exc)
            cache[key] = []
        log.info("peers %s: %d with usable multiples", ", ".join(key), len(cache[key]))
    return cache[key]


def _peer_tickers(model) -> list[str]:
    tickers = model.get("peers") if isinstance(model, dict) else None
    return ([str(t).strip().upper() for t in tickers if str(t).strip()]
            if isinstance(tickers, list) else [])


def _stake_tickers(model) -> list[str]:
    stakes = model.get("listed_stakes") if isinstance(model, dict) else None
    return ([str(s["ticker"]).strip().upper() for s in stakes
             if isinstance(s, dict) and str(s.get("ticker") or "").strip()]
            if isinstance(stakes, list) else [])


def peer_key(model) -> tuple:
    """The cache and replay key for a model's peers and listed stakes: the peer tickers,
    then "stakes:" and the stake tickers when there are any, so a replay without stakes
    keeps the key it was saved under."""
    stakes = _stake_tickers(model)
    return tuple(_peer_tickers(model)) + (("stakes:",) + tuple(stakes) if stakes else ())


def _house_cost(headline: dict, data: MarketData) -> valuation.CostOfCapital | None:
    """The code's cost of capital for this company, with any premium the model argues."""
    if not config.HOUSE_WACC:
        return None
    model = headline.get("model")
    return valuation.cost_of_capital(data, model if isinstance(model, dict) else None)


def _placeholder_values(v: valuation.Valuation, data: MarketData) -> dict[str, str]:
    """Every {{name}} the prose may cite, the consensus ones included."""
    values = valuation.placeholders(v)
    if data.analyst_target:
        values["consensus_target"] = f"{data.currency} {data.analyst_target:,.2f}"
        values["vs_consensus"] = valuation.pct((v.recommendation_value / data.analyst_target - 1) * 100)
        values["consensus_upside"] = valuation.pct((data.analyst_target / data.price - 1) * 100)
    return values


def _provenance_pool(headline: dict, v: valuation.Valuation, data: MarketData,
                     digest: str) -> provenance.Pool:
    """Every figure the writer was shown or the code computed from its model."""
    model = headline.get("model") if isinstance(headline.get("model"), dict) else {}
    values = _placeholder_values(v, data)
    trusted = ["\n".join(values.values()), _computed_summary(v, data), "\n".join(v.notes),
               _discount_rate_line(data)]
    notes = [digest, data.prompt_block, _consensus_line(data)]
    return provenance.build_pool(trusted, notes, v, data, model)


# A finance arm that lends to customers, named in the research notes.
FINANCE_ARM = re.compile(r"Financial Products segment|Financial Services segment|captive financ|"
                         r"Cat Financial|John Deere Financial|PACCAR Financial|GM Financial|"
                         r"Ford Credit|finance receivables|financing receivables", re.I)


def _finance_arm_problems(model: dict, v: valuation.Valuation, data: MarketData,
                          digest: str | None) -> list[str]:
    """A finance arm's debt, asked for when the notes show one and checked against them.

    The CAT pitch of 2026-10-01 took all $45.1bn of consolidated debt off its value; most
    of it funds Cat Financial's loans to customers, whose interest is already an operating
    cost.
    """
    if digest is None or v.family != "fcff":     # no enterprise-value bridge to fix
        return []
    arm = FINANCE_ARM.search(digest)
    if model.get("captive_finance_debt") is None:
        if not arm:
            return []
        return [f'the research notes describe a finance arm ("{arm.group(0)}"), and the bridge '
                f"takes all {data.reporting_currency} {data.debt:,.0f}mn of consolidated debt "
                "off enterprise value. The arm's debt funds its loans to customers and its "
                "interest is already in operating costs. Set `model.captive_finance_debt` to "
                "the arm's debt from the filing (reporting currency, millions) with "
                "`captive_finance_source` naming the filing and line, or to 0 if the notes "
                "show the arm has none of its own"]
    if not v.finance_debt:
        return []
    found = any(f.kind == "money"
                and abs(f.value - v.finance_debt * 1e6) <= max(0.01 * v.finance_debt * 1e6, f.slack)
                for f in provenance.figures(digest))
    if found:
        return []
    return [f"`captive_finance_debt` of {v.finance_debt:,.0f} is not a figure in the research "
            "notes. Use the finance arm's debt exactly as the filing gives it, or set it to 0 "
            "and say in the text that the split is not available"]


# Backlog this many years of trailing revenue makes a sudden slowdown a claim to argue.
BACKLOG_COVER_YEARS = 2.0


def _backlog_problems(v: valuation.Valuation, data: MarketData, fx) -> list[str]:
    """A forecast whose growth halves after its first year, below half the latest
    reported growth, while the company's order backlog covers years of revenue. ESLT's of
    2026-10-07 went from 15.9% to 7.6% growth in FY2027 and under 4% by FY2030, with a
    backlog of 3.7 years' revenue and 16.3% growth the year before; that one input was
    most of the gap to another analyst's value."""
    if fx is None or len(v.rows) < 2 or not data.ttm_revenue or v.family != "fcff":
        return []
    backlog = max((s.figure.value / 1e6 for s in fx.statements
                   if s.figure.kind == "money" and re.search(r"\bbacklog\b", s.context, re.I)),
                  default=0.0)
    cover = backlog / data.ttm_revenue
    if not BACKLOG_COVER_YEARS <= cover <= 15:
        return []
    reported = market_data.reported_years(data.prompt_block)
    if len(reported) < 2 or not reported[1].get("revenue") or not reported[0].get("revenue"):
        return []
    last = reported[0]["revenue"] / reported[1]["revenue"] - 1
    first = v.rows[0].revenue / v.base_revenue - 1
    second = v.rows[1].revenue / v.rows[0].revenue - 1
    if not (first > 0 and second < first / 2 and second < last / 2):
        return []
    return [f"model input: revenue growth halves from {first:.1%} in {v.rows[0].label} to "
            f"{second:.1%} in {v.rows[1].label}, against {last:.1%} in {reported[0]['label']}, "
            f"while the order backlog of {data.reporting_currency} {backlog:,.0f}mn covers "
            f"{cover:.1f} years of trailing revenue. Reassess whether the base-case growth path "
            "is the most evidence-supported outcome. Backlog is not guaranteed revenue and does "
            "not by itself establish a growth rate: explain with evidence either why conversion "
            "slows (such as delivery schedules, capacity constraints or cancellations) or why "
            "the forecast conversion pace is supported. Do not assume a sharp slowdown or a "
            "backlog-driven growth rate without evidence"]


def _returns_problems(v: valuation.Valuation, text: str) -> list[str]:
    """The Returns on capital paragraph, and the Leverage one when net debt is high."""
    problems = []
    if (v.returns and any(y.roic is not None for y in v.returns.years)
            and not RETURNS_LEAD.search(text)):
        problems.append("the Intrinsic Valuation section has no **Returns on capital.** "
                        "paragraph: the ROIC the business has earned ({{roic:YEAR}}) against "
                        "the WACC, where your model takes it and why that is credible, and the "
                        "return on new capital the terminal value assumes ({{terminal_roic}})")
    if v.leverage and v.leverage.needs_paragraph and not LEVERAGE_LEAD.search(text):
        have = valuation.placeholders(v)
        cost = ", ".join("{{%s}}" % k for k in ("interest_coverage", "cost_of_debt") if k in have)
        eps = " ({{buyback_eps_change}})" if "buyback_eps_change" in have else ""
        problems.append(
            f"net debt is {v.leverage.net_debt_to_ebitda:.1f}x trailing EBITDA, above "
            f"{valuation.LEVERAGE_PARAGRAPH:.1f}x, and the Intrinsic Valuation section has no "
            f"**Leverage.** paragraph: say what the debt costs ({cost}), how earnings move as "
            f"borrowing changes{eps}, and what the debt does to the equity in the downside case")
    return problems


def _typed_beside_problems(text: str, values: dict[str, str]) -> list[str]:
    """A figure the writer typed beside the placeholder for the same figure, disagreeing.

    "FY2030 FCF margin ~14% ({{fcf_margin:FY2030}})" reached VST's PDF as "~14% (22.5%)".
    """
    problems = []
    for m in consistency.TYPED_BESIDE_RE.finditer(text):
        value, missing = _fill_placeholders(m.group("ph"), values)
        if missing or consistency.agrees_beside(m.group("typed"), value):
            continue
        problems.append(
            f'the text types {m.group("typed").strip()} beside {m.group("ph")}, which is '
            f"{value}: the two disagree. Delete the typed figure and let the placeholder carry "
            "the number, then fix any sentence built on the typed one")
    return list(dict.fromkeys(problems))


COMPARED = re.compile(
    r"(?P<a>\{\{[^{}]+\}\})(?P<mid>(?:(?!\b(?:but|while|whereas|and|than)\b)[^.;{}]){0,40}?)"
    r"\b(?P<word>(?:far |well |comfortably )?(?:above|exceeds?|exceeding|higher than|greater "
    r"than|below|under|lower than|short of))\b(?P<gap>[^.;{}]{0,20}?)(?P<b>\{\{[^{}]+\}\})", re.I)


def _comparison_problems(text: str, values: dict[str, str]) -> list[str]:
    """Two model figures compared the wrong way round. SPOT's of 2026-10-07 printed "requires
    the business to compound at 22.1% on new capital, far above the 25.0% the model
    allows"."""
    problems = []
    for m in COMPARED.finditer(text):
        (a_text, a_missing), (b_text, b_missing) = (_fill_placeholders(m.group(k), values)
                                                    for k in ("a", "b"))
        a, b = provenance.figures(a_text), provenance.figures(b_text)
        if a_missing or b_missing or len(a) != 1 or len(b) != 1 or a[0].kind != b[0].kind:
            continue
        higher = not re.search(r"below|under|lower|short", m.group("word"), re.I)
        if (a[0].value > b[0].value) != higher and a[0].value != b[0].value:
            problems.append(f'the text says {m.group("a")} ({a_text}) is "{m.group("word")}" '
                            f'{m.group("b")} ({b_text}), which the numbers contradict: fix '
                            "the comparison or the figure it rests on")
    return list(dict.fromkeys(problems))


def _fill_cover(headline: dict, values: dict[str, str] | None) -> list[str]:
    """Fill the placeholders in the cover's text, in place. Returns any unknown names.

    The cover is written like the body and cites the same placeholders: CEG's one-liner
    of 2026-09-29 reached the PDF as "asymmetric downside to {{intrinsic}} intrinsic value".
    """
    unknown: list[str] = []
    if isinstance(headline.get("one_liner"), str):
        headline["one_liner"], missing = _fill_placeholders(headline["one_liner"], values)
        unknown += missing
    pillars = headline.get("thesis_pillars")
    if isinstance(pillars, list):
        filled = []
        for pillar in pillars:
            if isinstance(pillar, str):
                pillar, missing = _fill_placeholders(pillar, values)
                unknown += missing
            filled.append(pillar)
        headline["thesis_pillars"] = filled
    return unknown


def _finalise(headline: dict, body: str, data: MarketData,
              peers: list | None = None, digest: str | None = None,
              fx: facts.Facts | None = None) -> tuple[Pitch, list[str]]:
    """Compute the valuation and bind it into the document.

    Returns the pitch and the problems worth a rewrite: a model that cannot be computed,
    inputs outside the sanity bounds, placeholders that name nothing, and prose that
    contradicts the computed numbers.
    """
    headline = dict(headline)
    # These are computed, never taken from the model. A header that still carries them
    # (an old habit, or the exemplar's) must not reach the cover.
    for key in ("current_price", "intrinsic_price", "upside_pct", "recommendation"):
        headline.pop(key, None)
    headline["currency"] = data.currency
    headline["current_price"] = data.price

    try:
        v = valuation.compute(headline.get("model"), data, peers, _house_cost(headline, data))
    except valuation.ModelInputError as exc:
        filled, _ = _fill_placeholders(body, None)
        filled += ("\n\n## Valuation status\n\n"
                   f"No intrinsic value or rating is provided: {exc}. The business and "
                   "thesis analysis is still provided above.")
        _fill_cover(headline, None)
        problem = f"the valuation could not be computed: {exc}"
        return Pitch(headline=headline, body=filled), [problem]

    if v.stops:
        # A failed data gate: no value, upside or rating is published on a misread figure.
        headline.update(recommendation=v.published_rating)
    else:
        headline.update(intrinsic_price=round(v.recommendation_value, 2),
                        upside_pct=round(v.recommendation_upside), recommendation=v.rating)
    if v.conviction:
        headline["conviction"] = v.conviction.capitalize()
    values = _placeholder_values(v, data)
    # "{{da_pct_revenue[4]}}": the writer indexing the model's years like a list.
    labels = [r.label for r in v.rows]
    body = INDEXED.sub(lambda m: "{{" + m.group(1) + ":" + labels[int(m.group(2))] + "}}"
                       if int(m.group(2)) < len(labels) else m.group(0), body)
    fact_problems, typed_out, assumptions, cited = [], [], {}, {}
    if fx is not None:
        body, fact_problems, typed_out, assumptions = _cite_facts(headline, body, fx, cited,
                                                                  values)
    pillars = headline.get("thesis_pillars")
    raw_cover = "\n".join(str(x) for x in [headline.get("one_liner") or "",
                                           *(pillars if isinstance(pillars, list) else [])])
    beside = _typed_beside_problems(body + "\n" + raw_cover, values)
    if beside:
        log.info("%s: %d typed figure(s) beside a placeholder dropped", data.symbol, len(beside))
    body = consistency.drop_repeated_leads(
        consistency.drop_orphan_placeholders(_drop_typed_beside(body)))
    if v.weighted_per_share is not None or v.reporting_currency != v.currency:
        body = _base_case_cites(body, converted=v.reporting_currency != v.currency)
    body = _net_cash_cites(body, "net_cash" in values)
    compared = _comparison_problems(body + "\n" + raw_cover, values)
    misused: list[str] = []
    # A share or change between a model value and a named fact ("revenue_FY2025").
    operands = ({x.id.lower(): x.figure.text for x in fx.by_id.values()
                 if not facts.FACT_ID.fullmatch(x.id)} if fx is not None else None)
    filled, unknown = _fill_placeholders(body, values, missing=MISSING, misused=misused,
                                         operands=operands)
    filled, gone = _drop_missing(filled)
    typed_out += gone
    unknown += _fill_cover(headline, values)
    prose = filled      # what the writer wrote; the tables below are the code's
    filled = _insert_tables(filled, valuation.model_outputs_markdown(v),
                            _assumptions_table(assumptions),
                            "\n\n".join(x for x in (
                                _thresholds_table(headline.get("model"), fx, values),
                                model_rules.quality_table(headline.get("model")),
                                model_rules.revisions_table(headline.get("model"))) if x))

    problems = [f"model input: {w}" for w in v.warnings] + fact_problems + compared + misused
    problems += _stale_upcoming_event_problems(filled)
    problems += _conclusion_signpost_problems(filled)
    problems += _guidance_reconciliation_problems(body, v, data, fx, digest)
    problems += [_placeholder_problem(u, v, bool(data.analyst_target))
                 for u in dict.fromkeys(unknown)]
    # Tables are appended after the prose was filled. Assumption rationales can themselves
    # contain model placeholders, which used to reach the PDF literally (RTX, 2026-10-06).
    filled, table_unknown = _fill_placeholders(filled, values)
    problems += [f"{u} appears in a generated PDF section and has no value; it was marked "
                 "unavailable in the report" for u in dict.fromkeys(table_unknown)]
    # The one-liner goes on the cover, so its figures are checked with the body's. One line
    # each: run together, ESLT's "beyond 2027" pillar bound 2027 to the next one's margin.
    pillars = headline.get("thesis_pillars")
    cover = "\n".join(str(x) for x in [headline.get("one_liner") or "",
                                       *(pillars if isinstance(pillars, list) else [])])
    problems += consistency.check(filled, v, data.analyst_target, cover=cover)
    flags, cover_flags = [], []
    if digest is not None and fx is None:
        pool = _provenance_pool(headline, v, data, digest)
        flags, cover_flags = provenance.audit(prose, pool), provenance.audit(cover, pool)
        untraced = [fl.figure.text for fl in flags + cover_flags if not fl.wrong]
        if untraced:
            log.info("%s: %d untraced figure(s): %s", data.symbol, len(untraced),
                     ", ".join(untraced))
        # Wrong figures go back to the writer; untraced ones are marked in the PDF. Asked
        # to source them, NFLX's writer relabelled them "my estimate" instead (2026-10-01).
        problems += provenance.describe(flags + cover_flags)
    if data.analyst_target and not VERSUS_CONSENSUS.search(filled):
        problems.append("the Intrinsic Valuation section has no **Versus consensus.** "
                        "paragraph comparing your value with the sell-side target")
    problems += _returns_problems(v, filled)
    problems += _backlog_problems(v, data, fx)
    problems += _finance_arm_problems(headline["model"], v, data, digest)
    model = headline["model"]
    has_bear = isinstance(model.get("bear"), dict) or isinstance(model.get("downside"), dict)
    problems += _scenario_probability_problems(model)
    problems += _model_rule_problems(model, None, v)
    if not isinstance(model.get("bull"), dict) or not has_bear:
        problems.append("the model needs distinct `bull` and `bear` scenario input objects "
                        "(legacy `downside` is accepted as the bear case), so the code can "
                        "compute the weighted recommendation")
    if not isinstance(model.get("scenario_probabilities"), dict):
        problems.append("the model needs `scenario_probabilities` for bull, base, and bear; "
                        "use evidence-based weights that sum to 1")
    if not any(h.group(1).strip().lower() == RISKS_HEADING for h in H2.finditer(filled)):
        problems.append("the pitch has no `## Risks and Downside Case` section")
    if not any(h.group(1).strip().lower() == CONCLUSIONS_HEADING for h in H2.finditer(filled)):
        problems.append("the pitch has no `## Conclusions` section with the operating view, "
                        "the valuation view and the main swing factor")
    # The prose is written before the code computes the value, so it argues the rating
    # the model expects. AVGO's of 2026-09-24 argued a mispriced AI franchise, "High"
    # conviction, and its own inputs came out at -29%, SELL.
    view = str(headline.get("view") or "").strip().upper()
    if view in RATINGS and view != v.rating and not v.stops:
        problems.append(
            f"your `view` is {view} and the text argues it, but your own model inputs compute "
            f"{data.currency} {v.recommendation_value:,.2f} "
            f"({valuation.pct(v.recommendation_upside)}), which is "
            f"{v.rating}. Do not tune inputs to reach a rating: keep the inputs the evidence "
            "supports and rewrite the thesis, the one_liner, the conviction and every "
            f"comparison to argue {v.rating}, with `view` set to match; change an input only "
            "where the research notes show it was wrong, and say which")
    return Pitch(headline=headline, body=filled, valuation=v, notes=list(v.notes),
                 flags=flags, cover_flags=cover_flags, prose=prose, cover=cover,
                 pool=pool if digest is not None and fx is None else None,
                 removed=typed_out, cited=cited,
                 unverified=filled.count("†") + cover.count("†"),
                 repairable=fact_problems + provenance.describe(flags + cover_flags)), problems


INDEXED = re.compile(r"\{\{\s*([\w.]+)\s*\[\s*(\d)\s*\]\s*\}\}")
RECOMMENDATION_CITE = re.compile(r"\{\{\s*(intrinsic|upside)\s*\}\}")
BRIDGE_CITE = re.compile(r"\{\{\s*(?:equity|shares)\s*\}\}")
BASE_CASE_WORDS = re.compile(r"\bDCF\b|\bbase[- ]case\b", re.I)
WEIGHTED_WORDS = re.compile(r"weight|probabilit|scenario|recommend", re.I)
CLAUSE_END = re.compile(rf"(?<=[.!?]){provenance.NOT_AN_END}\s+(?=[A-Z*({{])|(?<=;)\s+")


NET_SIGN_NOTE = re.compile(r"\s*\((?:a\s+)?negative(?:\s+figure)?\s*(?:=|is|means|indicates|denotes)"
                           r"\s*(?:a\s+)?net cash(?:\s+position)?\)", re.I)
NET_DEBT_CITE = re.compile(r"\b(net)\s+(?:cash|debt)(?:\s+(?:position|balance))?(?:\s+of)?\s*"
                           r"\{\{\s*net_debt\s*\}\}", re.I)


def _net_cash_cites(body: str, net_cash: bool) -> str:
    """A company with more cash than debt has net cash, written as a positive amount:
    ESLT's of 2026-10-07 printed "Net cash of -USD 111mn (negative = net cash)" from
    "Net cash of {{net_debt}}", and the prompt's own "(negative is net cash)" with it."""
    body = NET_SIGN_NOTE.sub("", body)
    if net_cash:
        body = NET_DEBT_CITE.sub(lambda m: m.group(1) + " cash of {{net_cash}}", body)
    return body


def _base_case_cites(body: str, converted: bool = False) -> str:
    """{{intrinsic}} is the probability-weighted value. A clause that is about the base-case
    DCF on its own cites {{base_intrinsic}}: SPOT's of 2026-10-07 wrote "equity USD 60.8bn /
    205.6mn shares = {{intrinsic}}", which printed USD 285.66 where the division gives
    295.97, and "DCF ... yields intrinsic value {{intrinsic}}" beside a table whose base case
    was 295.97. The bridge's equity over shares, and a DCF or base case not said to be
    weighted, are the base case. With `converted`, the statements are in another currency
    than the shares, and a bridge that divides equity by shares names the rate it converts at.
    """
    def clause(text: str) -> str:
        bridge = bool(BRIDGE_CITE.search(text))
        if bridge or (BASE_CASE_WORDS.search(text) and not WEIGHTED_WORDS.search(text)):
            text = RECOMMENDATION_CITE.sub(lambda m: "{{base_" + m.group(1) + "}}", text)
        if bridge and converted and "fx_rate" not in text:
            text = re.sub(r"\{\{\s*base_intrinsic\s*\}\}", "{{base_intrinsic}} (at {{fx_rate}})",
                          text, count=1)
        return text

    out = []
    for line in body.split("\n"):
        # The cover line ("Intrinsic Price: {{intrinsic}}") is the recommendation.
        if line.lstrip().startswith(("|", "#")) or "Current Price:" in line:
            out.append(line)
            continue
        parts = CLAUSE_END.split(line)
        out.append(" ".join(clause(p) for p in parts) if len(parts) > 1 else clause(line))
    return "\n".join(out)


def _cite_facts(headline: dict, body: str, fx: facts.Facts, cited: dict,
                values: dict[str, str] | None = None):
    """Facts mode: take out every typed figure, then print each cited one with its source.

    Typed figures are taken out on every draft, so whichever draft ships is clean, and
    each is sent back for the writer to cite. Returns the body, the problems, the removed
    sentences as (sentence, why) and the declared assumptions; the cover is changed in
    `headline`.
    """
    assumptions, problems = facts.assumptions_from(headline)
    assumptions, as_fact, misdeclared = facts.reconcile_assumptions(assumptions, fx, values)
    problems += misdeclared
    body = facts.cite_instead(body, as_fact)
    for key in ("one_liner",):
        if headline.get(key):
            headline[key] = facts.cite_instead(str(headline[key]), as_fact)
    if isinstance(headline.get("thesis_pillars"), list):
        headline["thesis_pillars"] = [facts.cite_instead(str(p), as_fact)
                                      for p in headline["thesis_pillars"]]
    body = facts.adopt_tags(body, fx)
    # A declared assumption by name, so a typed figure beside it is read as citing it.
    known = {**(values or {}), **{f"assume:{k}": a.text for k, a in assumptions.items()}}
    body = facts.adopt_typed(body, fx, known)
    body, own = facts.mark_own(body, values, fx.fy_end_month)
    # A missing note citation is a traceability problem, not proof the number is wrong.
    # Keep the writer's sentence visible, mark its figures, and feed the citation problem
    # back for revision instead of deleting useful (potentially accurate) analysis.
    body, removed = facts.strip_typed(body, preserve=True)
    # A labelled fact cited for another period, or guidance cited as a result: SPGI's of
    # 2026-10-02 gave the fourth quarter's segment growth as the second's.
    model = headline.get("model") if isinstance(headline.get("model"), dict) else {}
    segments = list(model.get("segments") or {}) if isinstance(model.get("segments"), dict) else []
    body, misused = facts.strip_misused(body, fx, segments, assumptions)
    if any(why != facts.LEANED_WHY for _, why in misused):
        problems.append("these figures are cited for something their source does not say: "
                        + "; ".join(f"{why} — in \"{' '.join(s.split())[:100]}\""
                                    for s, why in [m for m in misused if m[1] != facts.LEANED_WHY][:8])
                        + ". Cite the fact for the right period, or say what it is; in the "
                        "final draft these sentences are deleted")
    # Check cover figures too; an unmatched number there stays visible with a dagger.
    cover_unverified = []
    def cover_pass(text: str) -> str:
        """The cover gets the body's matching and marking before its typed figures count:
        SPGI's fifth rewrite lost a pillar that cited the model's terminal multiple."""
        text = facts.adopt_typed(facts.adopt_tags(text, fx), fx, values)
        text = facts.mark_own(text, values, fx.fy_end_month)[0]
        text, found = facts.strip_typed(text, preserve=True)
        cover_unverified.extend(found)
        return text

    if headline.get("one_liner"):
        headline["one_liner"] = cover_pass(str(headline["one_liner"]))
    if isinstance(headline.get("thesis_pillars"), list):
        headline["thesis_pillars"] = [cover_pass(str(p)) for p in headline["thesis_pillars"]]
    problems += facts.describe_typed(removed + cover_unverified, fx=fx, values=values)
    body, unknown = facts.fill(body, fx, assumptions, cited)
    for key in ("one_liner",):
        if headline.get(key):
            headline[key], more = facts.fill(str(headline[key]), fx, assumptions, cited)
            unknown += more
    if isinstance(headline.get("thesis_pillars"), list):
        filled = []
        for p in headline["thesis_pillars"]:
            text, more = facts.fill(str(p), fx, assumptions, cited)
            filled.append(text)
            unknown += more
        headline["thesis_pillars"] = filled
    problems += list(dict.fromkeys(unknown))
    # Marked estimates go in the Assumptions table too, each with the sentence it is in.
    for n, sentence in enumerate(own, 1):
        found = provenance.figures(facts.ANY_PLACEHOLDER.sub(" ", sentence))
        if found:
            text = ", ".join(f.text for f in found)
            # Its cited facts filled in: a company figure adopted in it printed "n.a." in the
            # Assumptions table, whose filler knows only the model (ESLT, 2026-10-07).
            basis = facts.fill(facts.untag(sentence), fx, assumptions)[0]
            # Without its claim markers and bold, cut at a word: NVO's of 2026-10-11 printed
            # "**Semaglutide patent expiry ... modelled [#D" in the table.
            basis = " ".join(re.sub(r"\s*\[#[^\]\s]*\]?", "", basis).replace("**", "").split())
            if len(basis) > 160:
                basis = basis[:160].rsplit(" ", 1)[0] + "…"
            assumptions[f"stated in the text {n}"] = facts.Assumption(
                f"stated in the text {n}", found[0], basis, text)
    # Listed with their figures filled in, as the reader would have seen them: ESLT's of
    # 2026-10-07 listed "approximately S52 attributable to orders outside Israel".
    # Typed figures remain in the report with † and their citation warning in `problems`;
    # only sentences actually removed by the misused-fact checks belong in Removed-by-checks.
    why = [(" ".join(facts.fill(s, fx, assumptions)[0].split()), reason)
            for s, reason in misused]
    return body, problems, why, assumptions


def _assumptions_table(assumptions: dict) -> str:
    """The writer's own estimates, each with the reason it gave."""
    if not assumptions:
        return ""
    rows = "\n".join(f"| {a.id.replace('_', ' ')} | {a.text or a.figure.text} | "
                      f"{a.basis.replace('|', '/')} |" for a in assumptions.values())
    return ("#### Assumptions\n\nThe writer's own estimates, not taken from a source.\n\n"
            "| Assumption | Value | Basis |\n| --- | --- | --- |\n" + rows)


def _thresholds_table(model, fx, values: dict) -> str:
    """What would prove the thesis wrong, from `model.thresholds`. The latest reading is
    the fact or model figure the writer names, never a typed number; n.a. when it is
    neither."""
    items = model.get("thresholds") if isinstance(model, dict) else None
    if not isinstance(items, list) or not items:
        return "*Thresholds: n.a., the model gives none.*"
    cell = lambda x: " ".join(str(x or "").replace("|", "/").split()) or valuation.NA
    rows = []
    for t in items:
        if not isinstance(t, dict):
            continue
        name = str(t.get("latest") or "").strip().strip("{}").strip()
        hit = facts.find(fx.by_id, name) if fx is not None and name else None
        latest = hit.figure.text if hit is not None else values.get(name, valuation.NA)
        claims = t.get("claims")
        if isinstance(claims, list):
            claims = " and ".join(str(c) for c in claims)
        rows.append(f"| {cell(t.get('measure'))} | {cell(latest)} | {cell(t.get('wrong_if'))} "
                    f"| {cell(t.get('when'))} | {cell(claims)} |")
    return ("| Thresholds | Latest | I am wrong if | When | Claim it refutes |\n"
            "| --- | --- | --- | --- | --- |\n" + "\n".join(rows))


def _drop_typed_beside(text: str) -> str:
    """"~14% ({{fcf_margin:FY2030}})" becomes "{{fcf_margin:FY2030}}": the code's figure,
    once. VST's of 2026-09-29 printed "~14% (22.5%)", ESLT's "10.5% (10.5%)"."""
    return consistency.TYPED_BESIDE_RE.sub(lambda m: m.group("ph"), text)


PROVENANCE_PROBLEMS = ("these figures are given for a forecast year",
                       "these worked figures do not come out",
                       "these multiples of this valuation")
EMPTY_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])?\s*(?:\*\*[^*]*\*\*)?\s*$")


def _repair(result: Pitch) -> None:
    """Fix in code what the last write left wrong, rather than ship it.

    A sentence with a figure that contradicts the model, a sum that does not come out or a
    multiple the code computed differently is deleted, and listed at the end of the PDF
    with the reason. A figure found in no source gets a dagger. Every pitch of 2026-10-01
    reached the reader with its wrong figures in the text and a list of them on the cover.
    """
    body, removed = result.body, []
    for fl in [f for f in result.flags if f.wrong]:
        sentence = fl.sentence.strip()
        # A bold paragraph label ("**Pricing/ARPU.** ARPU: $13.9 ...") stays.
        label = re.match(r"^(?:[-*+]\s+)?\*\*[^*]+\*\*\s*", sentence)
        cut = sentence[label.end():] if label else sentence
        if cut and cut in body:
            body, leaned = facts.drop_sentence(body, cut)
            if cut in result.prose:
                result.prose, _ = facts.drop_sentence(result.prose, cut)
            removed += [(cut, _why(fl))] + [(s, facts.LEANED_WHY) for s in leaned]
    # A price, rating or base case the model contradicts goes the same way, and its check
    # with it: the header is mended to the computed figures, other sentences deleted.
    v = result.valuation
    if v is not None:
        def contradictions():
            return consistency.headline_contradictions(consistency.own_text(body), v)
        found = {problem for _, problem in contradictions()}
        body = consistency.fix_header(body, v)
        result.prose = consistency.fix_header(result.prose, v)
        for sentence, problem in contradictions():
            cut = sentence.strip()
            if cut and cut in body:
                body, leaned = facts.drop_sentence(body, cut)
                if cut in result.prose:
                    result.prose, _ = facts.drop_sentence(result.prose, cut)
                removed += [(cut, problem)] + [(s, facts.LEANED_WHY) for s in leaned]
        resolved = found - {problem for _, problem in contradictions()}
        result.checks = [c for c in result.checks if c not in resolved]
    body = _clean_text_artifacts(_drop_repeats(body))
    marked = 0
    by_sentence: dict[str, list] = {}
    for fl in result.flags:
        if not fl.wrong:
            by_sentence.setdefault(fl.sentence, []).append(fl.figure)
    for sentence, figs in by_sentence.items():
        at = body.find(sentence)
        if at < 0:
            continue
        for f in sorted(figs, key=lambda f: f.end, reverse=True):
            end = at + f.end
            if body[end:end + 1] != "†":
                body = body[:end] + "†" + body[end:]
                marked += 1
    # Only lines the removal emptied: a bullet or a bold label with nothing left after it.
    before = set(result.body.split("\n"))
    lines = [l for l in body.split("\n")
             if not (EMPTY_LINE.match(l) and l.strip() and l not in before)]
    body = re.sub(r"[ \t]{2,}", " ", "\n".join(lines))
    result.body = re.sub(r"\n{3,}", "\n\n", body)
    result.removed, result.unverified = result.removed + removed, result.unverified + marked
    if removed:
        log.info("removed %d sentence(s) with a wrong figure: %s", len(removed),
                 "; ".join(why for _, why in removed))
    cover_wrong = [f for f in result.cover_flags if f.wrong]
    # Facts mode already took out the sentences these name, and lists them at the end;
    # on the cover they were 20 lines of instructions to the writer, ids and all.
    result.checks = ([c for c in result.checks if not c.startswith(PROVENANCE_PROBLEMS)
                      and not c.rstrip(". ").endswith("these sentences are deleted")]
                     + provenance.describe(cover_wrong))


def _prepare_delivery(result: Pitch, problems: list[str], outline_map: dict | None = None,
                      primary_sources_read: int | None = None) -> Pitch:
    """Apply the same post-validation repairs and issue reporting in live and replay paths."""
    result.checks = list(dict.fromkeys(problems))
    result.open_count = len(problems)
    result.outline = outline_map or {}
    if result.outline:
        result.body = outline.attach_obvious_markers(result.body, result.outline)
        result.body = outline.drop_repeated_markers(result.body)
        result.checks.extend(p for p in outline.marker_problems(result.body, result.outline)
                             if p not in result.checks)
    if (isinstance(primary_sources_read, int)
            and primary_sources_read < config.MIN_PRIMARY_FETCHES):
        result.checks.append(
            f"research coverage: only {primary_sources_read} company primary-source page(s) "
            f"were read; the configured minimum is {config.MIN_PRIMARY_FETCHES}. Treat "
            "reported claims as insufficiently verified until the latest company filings, "
            "results release, or investor materials have been reviewed")
    if result.valuation is not None:
        result.checks.extend(f"data gate: {stop}; the rating is withheld"
                             for stop in result.valuation.stops)
    _repair(result)
    if result.outline:
        result.checks.extend(p for p in outline.marker_problems(result.body, result.outline)
                             if p not in result.checks)
    # A flawed terminal assumption is still useful to show in a draft: the warning is
    # included in `checks`, the report is marked DRAFT, and conviction is capped. Do not
    # discard the research and thesis after a long run just because a model input remains
    # unresolved; hard data gates already withhold the rating where the underlying data
    # cannot support one.
    _cap_conviction(result, len(result.checks))
    return result


def _cap_conviction(result: Pitch, open_points: int) -> None:
    """Conviction is low while checks or reviewer points stay open. The computed level
    measures only how the rating survives the WACC and terminal grid; SPOT's DRAFT of
    2026-10-11 printed "High" over 19 unresolved checks and 3 reviewer points."""
    v = result.valuation
    if v is None or not v.conviction or not open_points or v.conviction == "low":
        return
    was = v.conviction
    reason = f"{open_points} problem(s) the checks or the reviewer left open"
    v.conviction = "low"
    v.conviction_reasons.append(reason)
    result.headline["conviction"] = "Low"
    # The checks paragraph of Model Outputs: "**Conviction:** high (reasons)."
    result.body = re.sub(r"(\*\*Conviction:\*\* )" + was + r" \(([^()\n]*)\)",
                         lambda m: m.group(1) + "low (" + m.group(2) + "; " + reason + ")",
                         result.body)


REPEAT = re.compile(r"\s\((?P<fig>[^()]{1,24})\)")


def _drop_repeats(text: str) -> str:
    """"a 14.8% CAGR (14.8%)", "to 34.5% (FY2030) (34.5%)": a figure the writer typed and
    then cited again as a placeholder a few words later prints twice. The bracketed copy
    goes when the same figure stands in the 60 characters before it. Table rows are left
    alone: GS on 2026-10-13 lost its FY2028 operating expenses "(43,065)" as a repeat of
    the "(42,787)" beside it."""
    return "\n".join(line if line.lstrip().startswith("|") else _drop_repeats_line(line)
                     for line in text.split("\n"))


def _drop_repeats_line(text: str) -> str:
    def drop(m: re.Match) -> str:
        inner = provenance.figures(m.group("fig"))
        if len(inner) != 1 or inner[0].text != m.group("fig").strip().lstrip("~"):
            return m.group(0)
        window = re.split(r"[.;]\s|\n", text[max(0, m.start() - 60):m.start()])[-1]
        same = [f for f in provenance.figures(window)
                if f.kind == inner[0].kind and abs(f.value - inner[0].value) <= max(
                    f.slack, inner[0].slack, abs(inner[0].value) * 0.005)]
        return "" if same else m.group(0)
    return REPEAT.sub(drop, text)


def _clean_text_artifacts(text: str) -> str:
    """Remove empty signposts and repair skipped numbering after sentence removal."""
    text = re.sub(r"(?i)(?:\*\*)?What would tell\.(?:\*\*)?(?=\s*(?:\n|$))", "", text)
    text = re.sub(r"(?im)^\s*(?:[-*+]\s*)?(?:\*\*)?What would tell\.(?:\*\*)?\s*$", "", text)
    # Only renumber the opening summary paragraph, where these parenthesized numbers
    # refer to the thesis pillars. Other body numbering and parenthetical figures remain.
    split = re.search(r"\n\s*\n", text)
    end = split.start() if split else min(len(text), 1200)
    opening = text[:end]
    labels = list(re.finditer(r"\((\d+)\)", opening))
    nums = [int(m.group(1)) for m in labels]
    if len(nums) >= 2 and nums[0] == 1 and nums != list(range(1, len(nums) + 1)):
        for index, match in reversed(list(enumerate(labels, 1))):
            opening = opening[:match.start()] + f"({index})" + opening[match.end():]
        text = opening + text[end:]
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _why(fl) -> str:
    if fl.kind == "year":
        return f"{fl.figure.text} for FY{fl.year}: {fl.detail or 'not the model figure'}"
    if fl.kind == "arithmetic":
        return f"{fl.figure.text}: the sum comes to {fl.detail}"
    if fl.kind == "growth":
        return f"{fl.figure.text} growth: {fl.detail}"
    return f"{fl.figure.text}: {fl.detail}"


def _computed_summary(v: valuation.Valuation, data: MarketData) -> str:
    """What the code made of the model's inputs, in a few sentences."""
    text = (f"Probability-weighted implied share price {v.currency} "
            f"{v.recommendation_value:,.2f} vs current {v.price:,.2f} "
            f"({valuation.pct(v.recommendation_upside)}, {v.rating}); base case "
            f"{v.per_share:,.2f} ({valuation.pct(v.upside_pct)}). WACC {v.wacc:.1%}"
            + (f" (set by the code: {v.cost.describe()})" if v.cost else "")
            + f"; terminal value is {v.terminal_share:.0%} of enterprise value.")
    if v.peers and v.peers.value is not None:
        text += (f" At the peers' median multiples the value is {v.currency} "
                 f"{v.peers.value:,.2f} ({valuation.pct((v.peers.value / v.price - 1) * 100)}), "
                 f"peers {', '.join(p.symbol for p in v.peers.peers)}; the DCF is "
                 f"{'above' if v.per_share > v.peers.value else 'below'} that.")
    if data.analyst_target:
        text += (f" The DCF is {valuation.pct((v.per_share / data.analyst_target - 1) * 100)} "
                 f"from the consensus target of {data.analyst_target:,.2f}, so the assumptions "
                 "named for the gap must be ones that make it "
                 f"{'higher' if v.per_share > data.analyst_target else 'lower'} than the Street's.")
    if v.current_ev_ebitda:
        text += (f" The market pays {v.current_ev_ebitda:.1f}x trailing EBITDA today"
                 + (f" (Yahoo quotes {v.quoted_ev_ebitda:.1f}x on its own EV and EBITDA; the "
                    "peers are on the code's basis)"
                    if v.quoted_ev_ebitda and abs(v.quoted_ev_ebitda / v.current_ev_ebitda - 1) > 0.1
                    else "") + ".")
    if v.terminal_ev_ebitda:
        text += (f" The terminal value pays {v.terminal_ev_ebitda:.1f}x "
                 f"{v.rows[-1].label} EBITDA.")
    if v.method == "exit_multiple" and v.implied_growth is not None:
        text += (f" That exit multiple implies {v.implied_growth:.1%} growth forever on "
                 f"{v.rows[-1].label} free cash flow"
                 + (f", against {v.growth:.1%} in the perpetuity cross-check"
                    if v.growth is not None else "") + ".")
    if v.downside_per_share is not None:
        text += (f" Downside case {v.currency} {v.downside_per_share:,.2f} "
                 f"({valuation.pct(v.downside_pct)}): {'; '.join(v.downside_changes)}.")
    if v.bull_per_share is not None:
        text += (f" Bull case {v.currency} {v.bull_per_share:,.2f} "
                 f"({valuation.pct(v.bull_pct)}): {'; '.join(v.bull_changes)}.")
    return text + _returns_summary(v)


def _returns_summary(v: valuation.Valuation) -> str:
    """ROIC against the WACC, and leverage, in a sentence or two each."""
    text = ""
    rt = v.returns
    reported = [y for y in rt.years if y.reported and y.roic is not None] if rt else []
    final = rt.year(v.rows[-1].label) if rt else None
    if reported and final and final.roic is not None:
        last = reported[-1]
        text += (f" ROIC (operating income after tax over equity plus debt less cash) was "
                 f"{last.roic:.1%} in {last.label}"
                 + (f" ({last.roic_ex_goodwill:.1%} excluding goodwill and intangibles)"
                    if last.roic_ex_goodwill is not None else "")
                 + f"; the model takes it to {final.roic:.1%} by {final.label}, against the "
                 f"{v.wacc:.1%} WACC.")
        if rt.terminal_roic is not None:
            text += f" The terminal value assumes new capital earns {rt.terminal_roic:.1%}."
    lv = v.leverage
    if lv:
        text += (f" Net debt is {lv.net_debt_to_ebitda:.1f}x trailing EBITDA"
                 if lv.net_debt_to_ebitda is not None else
                 " Net debt is " + valuation._money(lv.net_debt, v.reporting_currency))
        if lv.interest_coverage is not None:
            text += f", with interest covered {lv.interest_coverage:.1f}x"
        text += "."
        if lv.buyback_eps_change is not None:
            text += (f" Borrowing {valuation.BUYBACK_TURNS:.1f}x EBITDA to buy back stock would "
                     f"change EPS by {lv.buyback_eps_change * 100:+.1f}% (earnings yield "
                     f"{lv.earnings_yield:.1%} against a {lv.cost_of_debt:.1%} after-tax cost "
                     "of debt).")
    return text


def _issue_label(issue: dict) -> str:
    code = issue.get("code")
    return f"{code} ({issue.get('status') or 'PARTIAL'}) " if code else ""


def _fix_request(problems: list[str], wrote_calls: bool = False, computed: str = "",
                 issues: list[dict] | None = None, cite_facts: bool = False) -> str:
    """The re-ask: the code's problems and the reviewer's high-severity issues, in one
    request. Until 2026-10-05 the reviewer's issues had a revision and a re-review of their
    own after the code's re-asks: eight calls in a row after research, each minutes in
    NIM's queue."""
    no_tools = (
        "Research is finished and this step has no tools, so do not write out "
        "web_search or fetch_page calls. Write from the research notes you were "
        "given; where they do not cover something, say so in the text. "
    ) if wrote_calls else ""
    listed = ("That response has problems:\n" + "\n".join(f"- {p}" for p in problems)
              + "\n\n") if problems else ""
    reviewed = ""
    if issues:
        reviewed = (
            "A reviewing portfolio manager read your pitch and raised these issues:\n\n"
            + "\n".join(f"{n}. {_issue_label(i)}[{i['section'] or 'general'}] {i['claim']}\n"
                        f"   Problem: {i['problem']}"
                        + (f"\n   Suggested fix: {i['fix']}" if i["fix"] else "")
                        + (f"\n   How to repair: {_repair_hint(i.get('code', ''))}"
                           if _repair_hint(i.get("code", "")) else "")
                        for n, i in enumerate(issues, 1))
            + "\n\nFor each, either change the assumption in the `model` object, add the "
            "evidence from the research notes that supports it, or state plainly in the text "
            "that the evidence is not available and lower conviction accordingly. Do not "
            "invent evidence the notes do not contain. Keep everything the reviewer did not "
            "raise. Close a point with better data, better evidence, narrower wording or a "
            "method shown to be wrong; never by deleting a true claim that is inconvenient "
            "or by moving an input toward a result. The repaired pitch is reviewed again.\n\n")
    outcome = (f"What the code computed from your model inputs: {computed} Every "
               "comparison in the text must agree with these; if you change an input, "
               "they move.\n\n") if computed else ""
    cite = (" Cite every other figure by its name on the figure sheet or as a declared "
            "assumption ({{assume:id}}), as before: a sentence that types a number is deleted."
            if cite_facts else "")
    return (
        listed + reviewed + outcome + no_tools
        + "Send the complete pitch again with them fixed." + cite + " Cite computed values through "
        "placeholders such as {{intrinsic}}, {{upside}} and {{fcf:FY2030}} rather than "
        "typing numbers the code computes; where an input is out of bounds, change it or "
        "explain in the text why it must be. The very first character of your reply must "
        "be a backtick opening a ```json fence containing the header object, with no "
        "preamble and no reasoning before it, then the markdown pitch."
    )


def _patch_request(problems: list[str], computed: str = "",
                   issues: list[dict] | None = None) -> str:
    """Ask for localized paragraph replacements so a repair keeps the checked draft intact."""
    context = [*(f"- {p}" for p in problems)]
    context.extend(f"- Reviewer: {_issue_label(i)}[{i.get('section') or 'general'}] "
                   f"{i.get('problem')}"
                   + (f" How to repair: {_repair_hint(i.get('code', ''))}"
                      if _repair_hint(i.get("code", "")) else "")
                   for i in issues or [])
    return (
        "Repair only the paragraphs implicated by these issues. Return JSON only, with no "
        "markdown fence or commentary, in this shape: {\"patches\":[{\"section\":\"exact "
        "## heading text\",\"starts_with\":\"first words of the paragraph to replace\","
        "\"replacement\":\"the complete replacement paragraph\"}]}. Use one entry per "
        "paragraph. `section` must be the exact level-two heading; `starts_with` must match "
        "the paragraph's opening words so code can locate it. Keep unrelated paragraphs and "
        "the JSON header unchanged. Do not return the full pitch. Do not type computed figures; "
        "use the model placeholders. Cite facts to sources in the notes.\n\n"
        + ("Computed valuation: " + computed + "\n\n" if computed else "")
        + "Issues:\n" + "\n".join(context)
    )


def _apply_patches(original: str, response: str) -> str | None:
    """Merge keyed paragraph patches into a fenced full draft, or return None if ambiguous."""
    fence = JSON_FENCE.match(original)
    if not fence:
        return None
    decoder = json.JSONDecoder()
    patch_data = None
    for match in re.finditer(r"\{", response):
        try:
            candidate, _ = decoder.raw_decode(response, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("patches"), list):
            patch_data = candidate
            break
    if patch_data is None or not patch_data["patches"]:
        return None
    body = original[fence.end():].strip()
    for item in patch_data["patches"]:
        if not isinstance(item, dict):
            return None
        section = str(item.get("section") or "").strip().lstrip("# ")
        starts = " ".join(str(item.get("starts_with") or "").split()).casefold()
        replacement = str(item.get("replacement") or "").strip()
        if not section or not starts or not replacement:
            return None
        headings = list(re.finditer(r"^##\s+(.+?)\s*$", body, re.M))
        matching = [i for i, h in enumerate(headings)
                    if h.group(1).strip().casefold() == section.casefold()]
        if len(matching) != 1:
            return None
        idx = matching[0]
        left = headings[idx].end()
        right = headings[idx + 1].start() if idx + 1 < len(headings) else len(body)
        section_body = body[left:right]
        paragraphs = list(re.finditer(r"(?s)(?:\A|\n\s*\n)(.+?)(?=\n\s*\n|\Z)",
                                      section_body))
        hits = [p for p in paragraphs if " ".join(p.group(1).split()).casefold().startswith(starts)]
        if len(hits) != 1:
            return None
        target = hits[0]
        paragraph_start, paragraph_end = target.start(1), target.end(1)
        section_body = section_body[:paragraph_start] + replacement + section_body[paragraph_end:]
        body = body[:left] + section_body + body[right:]
    return original[:fence.end()] + "\n\n" + body


def _requires_full_rewrite(problems: list[str], result: Pitch) -> bool:
    """Issues changing the rating or the model need the complete header and argument back."""
    if result.valuation is None:
        return True
    full = ("your `view`", "no `## conclusions`", "required section", "one_liner",
            "thesis_pillars", "scenario_probabilities", "model needs distinct",
            "cover contradict", "recommendation")
    return any(any(term in p.lower() for term in full) for p in problems)


def _retryable_problems(result: Pitch, problems: list[str]) -> list[str]:
    """Do not spend a model call on issues already repaired or explicitly flagged by code."""
    if not config.AUTO_REPAIR_NO_REASK:
        return problems
    # Citation gaps are deliberately left visible and marked by _cite_facts; they still
    # need a rewrite so the writer can cite, qualify, or remove them. Other repairable
    # issues have already been changed in code and should not cause a redundant call.
    return [p for p in problems if (p not in result.repairable
            or p.startswith("these figures are typed, not cited:"))
            and (not p.lower().startswith("model input:") or
                 valuation.is_bound_warning(p.removeprefix("model input: ")))
            and not any(p.startswith(prefix) for prefix in PROVENANCE_PROBLEMS)]


def _apply_window(messages: list[dict]) -> None:
    """Truncate older tool results in place, newest kept whole.

    Everything a tool returned is preserved in the digest for the write phase, so
    trimming here costs the model recall during research, not evidence at write time.
    """
    budget = config.RESEARCH_WINDOW_CHARS
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        content = message["content"]
        if budget <= 0:
            if len(content) > 300:
                message["content"] = content[:300] + "\n[trimmed — retained in notes]"
        elif len(content) > budget:
            message["content"] = content[:budget] + "\n[trimmed — retained in notes]"
            budget = 0
        else:
            budget -= len(content)


DIGEST_ORDER = {"PRIMARY": 0, "SECONDARY": 1, "SNIPPETS": 2}
# Blocks read from the company's filings by code, not fetched pages: the tables, the XBRL
# facts, and in gather mode the 10-K's sections and the 8-K and Form 4 summary.
PRIMARY_BLOCKS = ("filing_tables", "sec_xbrl", "filing_section", "sec_events")


def _build_digest(findings: list[tuple[str, str, str]]) -> str:
    """What research turned up, under a hard cap: filings first, snippets last.

    In the order found, the filings came last (research reads them when nudged at the
    end) and were the first thing the cap cut.
    """
    tagged = []
    for name, arg, result in findings:
        if name == "fetch_page":
            kind = "PRIMARY" if research_tools.is_primary(arg) else "SECONDARY"
        elif name in PRIMARY_BLOCKS:
            kind = "PRIMARY"
        else:
            kind = "SNIPPETS"
        tagged.append((kind, f"## [{kind}] {name}: {arg}\n\n{result.strip()}"))
    tagged.sort(key=lambda item: DIGEST_ORDER[item[0]])   # stable: found order kept within

    blocks: list[str] = []
    total = 0
    for _, block in tagged:
        if total + len(block) > config.DIGEST_MAX_CHARS:
            remaining = config.DIGEST_MAX_CHARS - total
            if remaining > 500:
                blocks.append(block[:remaining] + "\n[truncated]")
            break
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks) if blocks else "No research was gathered."


# --------------------------------------------------------------------------- free-tier quota

# Gemini's free tier allows 20 requests a day per model (2026-09-23: a live NVDA run hit
# "GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit: 20" nineteen searches in).
# Once a model's daily quota is gone, every further call is a wasted round trip, so it is
# skipped until the quota resets at midnight Pacific time.
_quota_exhausted: dict[str, float] = {}   # model -> epoch seconds when it may be tried again


def _next_quota_reset() -> float:
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        return midnight.timestamp()
    except Exception:   # no time zone database: wait a full day to be safe
        return time.time() + 24 * 3600


def _note_quota(model: str, exc: Exception) -> None:
    """Remember a model whose daily quota is spent. A per-minute limit is left alone."""
    if isinstance(exc, openai.RateLimitError) and "PerDay" in str(exc):
        _quota_exhausted[model] = _next_quota_reset()
        log.warning("%s has used its daily free-tier quota; skipping it until the reset", model)


def _available(model: str) -> bool:
    if model.startswith("gemini") and not config.GEMINI_API_KEY:
        return False
    if model.startswith("openrouter/") and not config.OPENROUTER_API_KEY:
        return False
    if (not model.startswith(("gemini", "openrouter/")) and not claude_code.is_claude(model)
            and not config.NVIDIA_API_KEY):
        return False
    return _quota_exhausted.get(model, 0) <= time.time()


def _research_candidates() -> list[str]:
    """Who may drive research, in order: RESEARCH_MODEL, MODEL, then FALLBACK_MODEL."""
    out: list[str] = []
    for model in (config.RESEARCH_MODEL, config.MODEL, config.FALLBACK_MODEL):
        if model and model not in out and _available(model):
            out.append(model)
    return out or [config.MODEL]


async def _stream_turn(client, messages, tools, on_progress, usage):
    """One research request, retried on transient failures. Returns (content, tool_calls).

    NIM's free tier sheds load with 503s, and it can do so *mid-stream* — after the
    request succeeded, part-way through the response. The SDK's own retries do not
    cover that case, so the whole turn is replayed here.

    A failure moves down the research candidates rather than queueing for the same
    model again, and the switch is recorded in usage["model"] so later rounds stay on
    the model that answered. Only the last candidate is retried in place.
    """
    delay = config.RETRY_BASE_DELAY_S
    for attempt in range(config.MAX_RETRIES + 1):
        candidates = _research_candidates()
        model = usage.get("model") or candidates[0]
        if model not in candidates:
            model = usage["model"] = candidates[0]
        following = candidates[candidates.index(model) + 1:]
        try:
            return await _one_stream(client, messages, tools, on_progress, usage)
        except (*FATAL_API_ERRORS, *TRANSIENT_API_ERRORS) as exc:
            _note_quota(model, exc)
            fatal = isinstance(exc, FATAL_API_ERRORS)
            if fatal and not following:
                raise PitchError(f"NIM rejected the request: {exc}") from exc
            if attempt == config.MAX_RETRIES:
                raise PitchError(
                    f"The research models kept failing after {config.MAX_RETRIES + 1} "
                    f"attempts ({exc}). The free tiers are likely overloaded — try again "
                    "shortly.") from exc
            if following:
                # A different model has its own queue, so there is nothing to wait out.
                log.warning("research turn on %s failed (%s) — switching to %s",
                            model, str(exc)[:160], following[0])
                usage["model"] = following[0]
                continue
            log.warning("research turn on %s failed (%s), retry %d in %.0fs",
                        model, str(exc)[:160], attempt + 1, delay)
            if on_progress:
                await on_progress("retrying", attempt + 1)
            await asyncio.sleep(delay)
            delay *= 2


async def _prepend_chunk(first, iterator):
    if first is not None:
        yield first
    async for chunk in iterator:
        yield chunk


async def _one_stream(client, messages, tools, on_progress, usage):
    """A single streamed request. Returns (content, tool_calls)."""
    model = usage.get("model", config.MODEL)
    if claude_code.is_claude(model):
        # No tools reach here: Claude's research runs as one session in _research.
        effort = config.CLAUDE_RESEARCH_EFFORT if tools else config.CLAUDE_WRITE_EFFORT
        return await claude_code.complete(model, messages, effort, on_progress, usage), []
    kwargs = dict(
        model=model.removeprefix("openrouter/"),
        messages=messages,
        # Research explores; the write and the review should land in the same place
        # from the same notes.
        temperature=config.TEMPERATURE if tools else config.WRITE_TEMPERATURE,
        top_p=config.TOP_P,
        max_tokens=config.MAX_TOKENS if tools else config.WRITE_MAX_TOKENS,
        stream=True,
        stream_options={"include_usage": True},
    )
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
        # A research turn is short once it starts; minutes in the queue are the whole
        # cost. The last candidate gets longer, having no one to hand over to, but it is
        # still capped: on 2026-09-23 gpt-oss-20b answered each retry with a 504 only
        # after five minutes.
        last = _research_candidates()[-1]
        kwargs["timeout"] = (config.RESEARCH_LAST_RESORT_S if model == last
                             else config.RESEARCH_FIRST_BYTE_S)

    content_parts: list[str] = []
    calls: dict[int, dict] = {}
    # NIM reports usage once, on the last chunk; Gemini repeats a running total on every
    # chunk. Keeping the latest and adding it once is right for both.
    seen = {"prompt": 0, "completion": 0}

    first_timeout = usage.get("first_byte_timeout")
    create = _client_for(client, model).chat.completions.create(**kwargs)
    # NIM answers the request itself only once the model starts generating, so a queued
    # request sits in create(): the first-byte limit has to cover it too. On 2026-10-13
    # GS waited the full 300s three times on a reviewer logged "no first byte in 60s".
    stream = await (asyncio.wait_for(create, first_timeout) if first_timeout else create)
    try:
        iterator = stream.__aiter__()
        if first_timeout:
            try:
                first_chunk = await asyncio.wait_for(iterator.__anext__(), first_timeout)
            except StopAsyncIteration:
                first_chunk = None
            chunks = _prepend_chunk(first_chunk, iterator)
        else:
            chunks = iterator
        async for chunk in chunks:
            if getattr(chunk, "usage", None):
                seen["prompt"] = chunk.usage.prompt_tokens or seen["prompt"]
                seen["completion"] = chunk.usage.completion_tokens or seen["completion"]
            if not chunk.choices:
                continue
            if chunk.choices[0].finish_reason:
                usage["finish_reason"] = chunk.choices[0].finish_reason
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            if getattr(delta, "content", None):
                content_parts.append(delta.content)
                if on_progress:
                    await on_progress("streaming", len(delta.content))
            elif getattr(delta, "reasoning_content", None) and on_progress:
                await on_progress("thinking", 0)

            for call in getattr(delta, "tool_calls", None) or []:
                # NIM streams a call in fragments tagged with its index. Gemini sends each
                # call whole, with no index: read an index-less piece that names a call as
                # a new one, and anything else as the rest of the last.
                index = call.index
                if index is None:
                    starts = call.id or (call.function and call.function.name)
                    index = len(calls) if starts or not calls else max(calls)
                slot = calls.setdefault(index, {"id": "", "name": "", "args": ""})
                if call.id:
                    slot["id"] = call.id
                if call.function and call.function.name:
                    slot["name"] = call.function.name
                if call.function and call.function.arguments:
                    slot["args"] += call.function.arguments
    finally:
        usage["prompt_tokens"] += seen["prompt"]
        usage["completion_tokens"] += seen["completion"]
        # The SDK closes a stream read to the end, but not one abandoned part-way by a
        # timeout or a load-shed between chunks. Left to the garbage collector, that
        # logs a traceback from httpcore's async generator on Python 3.14.
        await stream.close()

    return "".join(content_parts), [calls[i] for i in sorted(calls)]


# The Gemini fallbacks live on another provider, so they need their own client. Built
# on first use; a test can put a fake here. The same for OpenRouter's models.
_gemini_client = None
_openrouter_client = None


def _client_for(client, model: str):
    """The client that serves this model: Google's endpoint for Gemini, OpenRouter for
    "openrouter/..." models, NIM otherwise."""
    global _gemini_client, _openrouter_client
    if model.startswith("openrouter/"):
        if _openrouter_client is None:
            _openrouter_client = openai.AsyncOpenAI(
                base_url=config.OPENROUTER_BASE_URL, api_key=config.OPENROUTER_API_KEY,
                timeout=config.REQUEST_TIMEOUT_S, max_retries=0)
        return _openrouter_client
    if not model.startswith("gemini"):
        return client
    if _gemini_client is None:
        _gemini_client = openai.AsyncOpenAI(
            base_url=config.GEMINI_BASE_URL, api_key=config.GEMINI_API_KEY,
            timeout=config.REQUEST_TIMEOUT_S, max_retries=0)
    return _gemini_client


async def close_clients() -> None:
    """Close the Gemini and OpenRouter pools on shutdown, as bot.py does for NIM's."""
    global _gemini_client, _openrouter_client
    if _gemini_client is not None:
        await _gemini_client.close()
        _gemini_client = None
    if _openrouter_client is not None:
        await _openrouter_client.close()
        _openrouter_client = None


def _write_candidates() -> list[str]:
    """Who may write, in order: the primary (with retries), Gemini, then NIM's fallback."""
    models = [config.MODEL] * (1 + config.WRITE_PRIMARY_RETRIES)
    models += [m for m in config.WRITE_FALLBACK_MODELS if m != config.MODEL and _available(m)]
    if (config.FALLBACK_MODEL and config.FALLBACK_MODEL not in models
            and _available(config.FALLBACK_MODEL)):
        models.append(config.FALLBACK_MODEL)
    return models


async def _write_turn(client, messages, on_progress, usage) -> str:
    """A write or revision call. Returns the text; records the writer in usage["model"].

    Unlike a research turn, a write does not drop to the small fallback on the first
    failure. It gives the primary a few tries with a pause between them, then moves down
    the candidate list. An error that would repeat on the same model (a bad request, a
    retired model) moves on at once instead of being retried.
    """
    delay = config.RETRY_BASE_DELAY_S
    candidates = _write_candidates()
    last: Exception | None = None
    for i, model in enumerate(candidates):
        usage["model"] = model
        try:
            content, _ = await _one_stream(client, messages, None, on_progress, usage)
            return content
        except (*FATAL_API_ERRORS, *TRANSIENT_API_ERRORS) as exc:
            last = exc
            _note_quota(model, exc)
            following = candidates[i + 1] if i + 1 < len(candidates) else None
            if following is None:
                break
            log.warning("write on %s failed (%s: %s) — %s", model, type(exc).__name__,
                        str(exc)[:160], f"trying {following}")
            if on_progress:
                await on_progress("retrying", i + 1)
            if following == model and not isinstance(exc, FATAL_API_ERRORS):
                await asyncio.sleep(delay)
                delay *= 2
    raise PitchError(
        f"Every model that can write the pitch failed ({', '.join(dict.fromkeys(candidates))}); "
        f"last error: {last}. The providers are likely overloaded — try again shortly.")


# How often a model that says DONE short of MIN_SEARCHES is sent back. One that still
# stops after this is not going to search more, and asking again only costs turns.
MAX_RESEARCH_NUDGES = 2


def _research_gaps(sources: research_tools.SourceLog) -> list[str]:
    gaps = []
    if sources.searches < config.MIN_SEARCHES:
        gaps.append("searches")
    if sources.primary_fetches < config.MIN_PRIMARY_FETCHES:
        gaps.append("primary")
    return gaps


def _nudge(sources: research_tools.SourceLog, queries: list[str], gaps: list[str]) -> str:
    """Send a model that stopped early back to the gaps in its research."""
    ran = "\n".join(f"- {q}" for q in queries) or "- (none)"
    parts = []
    if "primary" in gaps:
        parts.append(
            f"You have read {sources.primary_fetches} of the company's own documents; read "
            f"at least {config.MIN_PRIMARY_FETCHES}. Find and fetch_page its latest quarterly "
            "or annual filing (10-Q/10-K on sec.gov, or the interim/annual report outside the "
            "US), its latest earnings release on the investor-relations site or a wire "
            "service, and its latest investor presentation or transcript. Reported figures "
            "and deal terms must come from there, not from articles that quote them.")
    if "searches" in gaps:
        parts.append(
            f"You have {sources.searches} successful searches; run at least "
            f"{config.MIN_SEARCHES} before stopping. Check your queries against the list — "
            "latest quarter, segments, market sizing, competitors and share, guidance and "
            "strategy, regulation and bear case — and search whatever they do not cover.")
    return (
        "\n\n".join(parts) + f"\n\nQueries so far:\n{ran}\n\nBatch the calls into one "
        "turn. Reply DONE only when the gaps are filled."
    )


async def _research_claude(messages, run, findings, sources, on_progress, usage) -> bool:
    """Research as one Claude Code session with the same tools. True when it finished;
    False when it failed and the loop below should carry on with the next model, from
    what was found so far."""
    model = usage.get("model") or _research_candidates()[0]
    usage["model"] = model
    nudges = 0

    async def run_tool(name: str, args: dict) -> str:
        arg, result = await run({"name": name, "args": json.dumps(args)})
        findings.append((name, arg, result))
        if on_progress:
            await on_progress("searching", sources.searches)
        return result

    def follow_up(reply: str) -> str | None:
        nonlocal nudges
        gaps = _research_gaps(sources)
        if not gaps or nudges == MAX_RESEARCH_NUDGES:
            return None
        nudges += 1
        log.info("research stopped at %d searches, %d primary fetches — asking for "
                 "more (%d/%d)", sources.searches, sources.primary_fetches, nudges,
                 MAX_RESEARCH_NUDGES)
        queries = [arg for name, arg, _ in findings if name == "web_search"]
        return _nudge(sources, queries, gaps)

    try:
        await claude_code.research(
            model, messages, research_tools.TOOL_SCHEMAS, run_tool, follow_up,
            config.CLAUDE_RESEARCH_EFFORT, config.MAX_TOOL_ROUNDS + 1, on_progress, usage)
        return True
    except (*FATAL_API_ERRORS, *TRANSIENT_API_ERRORS) as exc:
        following = [m for m in _research_candidates() if m != model]
        if not following:
            if findings:
                log.warning("research on %s failed (%s) — writing from what was found",
                            model, str(exc)[:160])
                return True
            raise PitchError(f"Research on {model} failed: {exc}") from exc
        log.warning("research on %s failed (%s) — switching to %s", model, str(exc)[:160],
                    following[0])
        usage["model"] = following[0]
        return False


async def _research(client, data, on_progress, usage) -> tuple[str, list[dict]]:
    """Phase 1: search and read. Returns (digest, sources)."""
    brief = RESEARCH_BRIEF
    if config.MIN_SEARCHES:
        brief += f"\n\nRun at least {config.MIN_SEARCHES} searches before you reply DONE."
    messages: list[dict] = [
        {"role": "system", "content": brief},
        {"role": "user", "content": _research_user_message(data)},
    ]
    sources = research_tools.SourceLog()
    findings: list[tuple[str, str, str]] = []
    fetched: set[str] = set()

    # The latest release and 10-Q/10-K come from EDGAR before the model searches: the
    # reported figures should not depend on whether it finds them. They count toward the
    # primary-source floor.
    # The XBRL facts are read at the same time, not after.
    xbrl_task = (asyncio.create_task(asyncio.to_thread(sec_xbrl.read, data.symbol))
                 if config.FACTS_MODE else None)
    try:
        filings = await asyncio.wait_for(
            asyncio.to_thread(research_tools.read_latest_filings, data.symbol, sources,
                              config.FACTS_MODE),
            timeout=config.TOOL_TIMEOUT_S * 2)
    except asyncio.TimeoutError:
        log.warning("EDGAR lookup for %s timed out — research finds the filings itself",
                    data.symbol)
        filings = []
    for filing, text, tables in filings:
        findings.append(("fetch_page", filing.url, text))
        if tables:
            findings.append(("filing_tables", filing.label, tables))
        fetched.add(filing.url)
    if xbrl_task is not None:
        xbrl = await xbrl_task
        if xbrl:
            findings.append(("sec_xbrl", "company facts", xbrl))
    if filings:
        messages[1]["content"] += (
            "\n\nAlready read for you from SEC EDGAR, and in your notes for the write-up:\n"
            + "\n".join(f"- {f.label}: {f.url}" for f, *_ in filings)
            + "\nDo not fetch these again. Spend your searches on what they do not cover.")

    attempts = [0]

    async def run(call):
        if call["name"] == "web_search":
            attempts[0] += 1
            if attempts[0] > config.MAX_SEARCHES:
                return "", (f"Search budget of {config.MAX_SEARCHES} is spent. "
                            "Write from what you have.")
        try:
            args = json.loads(call["args"]) if call["args"].strip() else {}
        except json.JSONDecodeError:
            return "", f"Could not parse arguments: {call['args'][:200]}"
        arg = str(args.get("query") or args.get("url") or "")
        if call["name"] == "fetch_page" and research_tools._is_sec(arg):
            # A guard, not a requirement: when the SEC's ticker list cannot be read
            # (no User-Agent, a 403 from a cloud host), let the fetch decide instead.
            try:
                canonical = (research_tools.cik_for(data.symbol)
                             if config.SEC_USER_AGENT else None)
            except Exception as exc:
                log.warning("SEC CIK lookup for %s failed: %s", data.symbol, exc)
                canonical = None
            match = re.search(r"/(?:data/)?0*(\d{5,10})(?:/|$)|CIK[=/]0*(\d{5,10})",
                              arg, re.I)
            url_cik = next((int(group) for group in match.groups() if group), None) \
                if match else None
            if canonical is not None and url_cik is not None and url_cik != canonical:
                return arg, (f"Blocked this SEC URL: CIK {url_cik} does not match "
                             f"{data.symbol}'s SEC CIK {canonical}. Use the issuer's "
                             "canonical SEC filing links instead.")
        if call["name"] == "fetch_page" and arg in fetched:
            return arg, f"Already fetched {arg} — see earlier notes."
        if call["name"] == "fetch_page":
            fetched.add(arg)
        log.info("tool %s %s", call["name"], arg[:110])
        # A hard cap on the wall clock. On 2026-09-23 a keyless search hung for 11
        # minutes inside Brave's engine, holding the whole round. The thread is left
        # to finish on its own; research moves on without it.
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(research_tools.dispatch, call["name"], args, sources),
                timeout=config.TOOL_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("tool %s timed out after %.0fs: %s", call["name"],
                        config.TOOL_TIMEOUT_S, arg[:110])
            result = (f"{call['name']} timed out after {config.TOOL_TIMEOUT_S:.0f}s. "
                      "Try different wording or another source.")
        return arg, result

    rounds = nudges = 0
    if claude_code.is_claude(usage.get("model") or _research_candidates()[0]):
        done = await _research_claude(messages, run, findings, sources, on_progress, usage)
        rounds = config.MAX_TOOL_ROUNDS if done else 0
    while rounds < config.MAX_TOOL_ROUNDS:
        try:
            text, calls = await _stream_turn(
                client, messages, research_tools.TOOL_SCHEMAS, on_progress, usage
            )
        except PitchError as exc:
            # The same live run had nineteen searches done when every research model
            # failed, and the pitch was thrown away with them. Write from what is there.
            if not findings:
                raise
            log.warning("research models unavailable after %d searches and %d fetches "
                        "(%s) — writing from what was found", sources.searches,
                        sources.fetches, exc)
            break
        if not calls:
            # Small models call it done after a handful of searches. Send them back a
            # bounded number of times; a turn without tool calls does not use a round.
            gaps = _research_gaps(sources)
            if not gaps or nudges == MAX_RESEARCH_NUDGES:
                break
            nudges += 1
            log.info("research stopped at %d searches, %d primary fetches — asking for "
                     "more (%d/%d)", sources.searches, sources.primary_fetches, nudges,
                     MAX_RESEARCH_NUDGES)
            queries = [arg for name, arg, _ in findings if name == "web_search"]
            messages.append({"role": "assistant", "content": text or "DONE"})
            messages.append({"role": "user", "content": _nudge(sources, queries, gaps)})
            continue
        rounds += 1

        results = await asyncio.gather(*(run(c) for c in calls))

        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": c["id"] or f"call_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                for i, c in enumerate(calls)
            ],
        })
        for i, (call, (arg, result)) in enumerate(zip(calls, results)):
            findings.append((call["name"], arg, result))
            messages.append({
                "role": "tool", "tool_call_id": call["id"] or f"call_{i}",
                "name": call["name"], "content": result,
            })

        _apply_window(messages)
        if on_progress:
            await on_progress("searching", sources.searches)
        if attempts[0] >= config.MAX_SEARCHES:
            log.info("research hit the search budget of %d", config.MAX_SEARCHES)
            break

    log.info("research done: %d searches, %d fetches (%d primary), %d sources",
             sources.searches, sources.fetches, sources.primary_fetches,
             len(sources.sources))
    usage["primary_sources_read"] = sources.primary_fetches
    return _build_digest(findings), sources.sources


# --------------------------------------------------------------------------- two-pass write

# The model alone is a fraction of the pitch, so a re-ask here is cheap next to one for
# the whole document: one re-ask, then the prose is written from what there is.
PLAN_ATTEMPTS = 2
# The placeholders the prose request shows filled in: the ones that carry the direction
# of the argument. Per-year figures are in the tables and stay placeholders.
SHOWN_PLACEHOLDERS = ("intrinsic", "upside", "base_intrinsic", "base_upside", "rating",
                      "bull", "bull_upside",
                      "bear", "bear_upside", "bull_probability", "base_probability",
                      "bear_probability", "weighted_intrinsic", "weighted_upside", "wacc", "wacc_build",
                      "consensus_target", "vs_consensus",
                      "peer_value", "peer_upside", "cross_check", "cross_check_upside",
                      "ev_ebitda", "tv_ev_ebitda", "tv_implied_growth", "downside",
                      "downside_upside", "implied_cagr", "model_cagr", "street_cagr",
                      "terminal_roic", "terminal_reinvestment", "new_capital_return",
                      "net_debt", "net_cash", "net_debt_to_ebitda",
                      "interest_coverage", "cost_of_debt", "earnings_yield",
                      "buyback_eps_change")
# Shown by year: the ROIC path the Returns on capital paragraph argues.
SHOWN_PREFIXES = ("roic:", "roic_ex_goodwill:")

PLAN_REQUEST = (
    "\n\n# Step 1 of 2: the model\n\n"
    "Do not write the pitch yet. Send only the ```json header block described in your "
    "instructions — every field, with the complete `model` object and `view` set to the "
    "rating you expect your inputs to produce — and nothing after it. The code will value "
    "your `model` and show you the result, and you will then write the pitch knowing where "
    "your value lands. Choose each input from the evidence in the research notes, not "
    "for the rating it gives."
)


# A dated period, read as the day it ends: "2026-03-31", "H1 2026", "1H26", "Q2 2026", "2Q26",
# "mid-2026", "early 2026" or a bare "2026". NVO's of 2026-10-11 gave "CagriSema Phase 3
# readouts H1 2026" as its Conclusions signpost in October 2026, and a bare-year reading of
# 31 December let it through as upcoming.
PERIOD = re.compile(
    r"\b(?P<iso>(?P<iy>20\d{2})-(?P<im>\d{2})-(?P<id>\d{2}))\b"
    r"|\b(?:(?:H(?P<h1>[12])|(?P<h2>[12])H)\s?(?:FY)?\s?(?P<hy>(?:20)?\d{2}))\b"
    r"|\b(?:(?:Q(?P<q1>[1-4])|(?P<q2>[1-4])Q)\s?(?:FY)?\s?(?P<qy>(?:20)?\d{2}))\b"
    r"|\b(?P<part>early|mid|late|end)[- ](?:of )?(?P<py>20\d{2})\b"
    r"|\b(?P<y>20\d{2})(?!-\d{2}-)\b", re.I)
_PART_END = {"early": 4, "mid": 8, "late": 12, "end": 12}


def _period_end(m: re.Match) -> date | None:
    """The last day of the period PERIOD matched, or None."""
    import calendar

    def year(text: str) -> int:
        return int(text) + 2000 if len(text) == 2 else int(text)

    def month_end(y: int, mo: int) -> date:
        return date(y, mo, calendar.monthrange(y, mo)[1])
    try:
        if m.group("iso"):
            return date(int(m.group("iy")), int(m.group("im")), int(m.group("id")))
        if m.group("hy"):
            return month_end(year(m.group("hy")), 6 * int(m.group("h1") or m.group("h2")))
        if m.group("qy"):
            return month_end(year(m.group("qy")), 3 * int(m.group("q1") or m.group("q2")))
        if m.group("py"):
            return month_end(int(m.group("py")), _PART_END[m.group("part").lower()])
        if m.group("y"):
            return date(int(m.group("y")), 12, 31)
    except ValueError:
        return None
    return None


def _stale_upcoming_event_problems(text: str) -> list[str]:
    """Catch forward-looking event language attached to an already-past date. A signpost or
    catalyst is forward-looking by definition; a range ("2026-27", "through 2027") is
    upcoming while its last year is."""
    upcoming = re.compile(r"\b(?:will|expected|scheduled|plans? to|set to|upcoming|"
                          r"reads? out|due to|forecast to|should|signposts?|next catalyst|"
                          r"catalysts?|next test|awaits?|pending)\b", re.I)
    event = re.compile(r"\b(?:read.?outs?|trials?|results|earnings|approvals?|launch(?:es)?|"
                       r"decisions?|guidance|event|catalyst|report|data|filing|"
                       r"announcement|endpoints?)\b", re.I)
    past_tense = re.compile(r"\b(?:was|were|reported|announced|read out in|delivered|"
                            r"published|showed|met|missed|failed|launched in|approved in|"
                            r"reached|grew|rose|fell|declined|increased|decreased)\b",
                            re.I)
    problems = []
    today = date.today()
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if (not upcoming.search(sentence) or not event.search(sentence)
                or past_tense.search(sentence)):
            continue
        ends = [(m.group(0), _period_end(m)) for m in PERIOD.finditer(sentence)]
        ends = [(t, e) for t, e in ends if e]
        # "2026-27", "2026 to 2027", "through 2027": judged by the range's end.
        for rng in re.finditer(r"\b(20\d{2})\s?(?:-|–|to|through)\s?((?:20)?\d{2})\b", sentence):
            last = rng.group(2)
            last_year = int(last) + 2000 if len(last) == 2 else int(last)
            ends = [(t, e) for t, e in ends if t not in (rng.group(1), rng.group(0))]
            ends.append((rng.group(0), date(last_year, 12, 31)))
        if ends and all(e < today for _, e in ends):
            stale = [t for t, _ in ends]
            problems.append("the text describes a dated event as upcoming even though its "
                            f"date has passed ({', '.join(dict.fromkeys(stale))}): "
                            + " ".join(sentence.split())[:220]
                            + ". Say what happened from a recent source, or name the next "
                              "dated event instead")
    return list(dict.fromkeys(problems))


def _conclusion_signpost_problems(text: str) -> list[str]:
    """Require a dated next test in conclusions, or disclose that none is scheduled."""
    match = re.search(r"(?im)^#{1,3}\s*conclusions?\s*$", text)
    if not match:
        return []  # The required section is checked separately.
    tail = text[match.end():]
    next_heading = re.search(r"(?m)^#{1,3}\s+", tail)
    conclusion = tail[:next_heading.start()] if next_heading else tail
    if re.search(r"no (?:publicly )?(?:dated )?(?:event|catalyst|date).*"
                 r"(?:disclosed|scheduled|announced)|none (?:is )?scheduled",
                 conclusion, re.I | re.S):
        return []
    for m in PERIOD.finditer(conclusion):
        when = _period_end(m)
        if when and when > date.today():
            return []
    return ["the Conclusions section needs a dated future event that will test the key "
            "variable, or an explicit statement that no event date is publicly scheduled"]


def _guidance_reconciliation_problems(body: str, v: valuation.Valuation,
                                     data: MarketData,
                                     fx: facts.Facts | None,
                                     digest: str | None = None) -> list[str]:
    """Require an explanation when first-year sales growth departs from sourced guidance."""
    if not v.rows or not data.revenue or v.family != "fcff":
        return []
    row = v.rows[0]
    years = re.findall(r"20\d{2}", row.label)
    if not years:
        return []
    year = years[-1]
    guidance: list[tuple[float, str, str]] = []
    if fx is not None:
        guidance = [(f.figure.value, f.id, f.source) for f in fx.by_id.values()
                    if f.data_type == "guidance" and f.figure.kind == "%"
                    and re.search(r"(?:revenue|sales).*growth|growth.*(?:revenue|sales)",
                                  f"{f.metric} {f.table}", re.I)
                    and year in f"{f.table} {f.period} {f.metric}"]
    elif digest:
        # Structured records also exist when full facts mode is off. Their row label,
        # period, guidance tag and source are retained in the research digest.
        for line in digest.splitlines():
            columns = [part.strip() for part in line.strip().strip("|").split("|")]
            if len(columns) < 4 or "[guidance]" not in columns[2].lower():
                continue
            if (not re.search(r"(?:revenue|sales).*growth|growth.*(?:revenue|sales)",
                              columns[0], re.I) or year not in columns[1]):
                continue
            guidance.extend((f.value, "", columns[3])
                            for f in provenance.figures(columns[2]) if f.kind == "%")
    if not guidance:
        return []
    model_growth = row.revenue / data.revenue - 1
    model_pp = model_growth * 100
    guidance_low = min(item[0] for item in guidance)
    guidance_high = max(item[0] for item in guidance)
    distance = (guidance_low - model_pp if model_pp < guidance_low
                else model_pp - guidance_high if model_pp > guidance_high else 0)
    if distance <= 5.0:
        return []
    justified = False
    for sentence in re.split(r"(?<=[.!?])\s+", body):
        cites_guidance = any(
            (bool(fid) and bool(re.search(r"\{\{\s*" + re.escape(fid) + r"\s*\}\}",
                                          sentence, re.I)))
            or (bool(source) and all(word.casefold() in sentence.casefold()
                                     for word in re.findall(r"[A-Za-z]{4,}", source)[:3]))
            for _, fid, source in guidance)
        explains = re.search(r"because|reflects?|due to|driven by|offset by|explained by",
                             sentence, re.I)
        if cites_guidance and explains:
            justified = True
            break
    if justified:
        return []
    citations = ", ".join(f"{{{{{fid}}}}}" if fid else source
                           for _, fid, source in guidance)
    return [f"FY{year} revenue growth in the first forecast year is {model_pp:.1f}%, outside "
            f"management's {guidance_low:.1f}%–{guidance_high:.1f}% guidance by "
            f"{distance:.1f} percentage points. Reconcile the forecast or explain the "
            f"difference in a sentence citing the guidance and a sourced reason ({citations})"]


def _parse_plan(text: str) -> dict:
    """The header of a model-only reply. Anything written after it is ignored."""
    match = JSON_FENCE.match(text)
    if match:
        try:
            headline = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise PitchError(f"The model's JSON header was malformed: {exc}") from exc
    else:
        headline, _ = _loose_header(text)
    if not isinstance(headline, dict):
        raise PitchError("The model's JSON header was not an object.")
    return headline


def _plan_fix_request(problems: list[str], computed: str,
                      placeholders: list[str] | tuple[str, ...] = ()) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    outcome = f"What the code computed from these inputs: {computed}\n\n" if computed else ""
    valid_names = ("Computed placeholders available for this model: "
                   + ", ".join(f"`{name}`" for name in placeholders) + ".\n\n"
                   if placeholders else "")
    return (
        "That model has problems:\n" + listed + "\n\n" + outcome
        + valid_names
        + "Send the ```json header block again with them fixed, and nothing after it. Where "
        "an input is out of bounds, change it or keep it and give the evidence in its "
        "rationale; do not change inputs only to move the rating."
    )


def _valuation_brief(headline: dict, v: valuation.Valuation, data: MarketData,
                     open_problems: list[str]) -> str:
    """What the code computed from the model, the placeholders' values, the checks still
    open, and the rating the text must argue."""
    values = _placeholder_values(v, data)
    keys = [k for k in SHOWN_PLACEHOLDERS if k in values]
    keys += [k for k in values if k.startswith(SHOWN_PREFIXES)]
    shown = "\n".join(f"- `{{{{{k}}}}}` = {values[k]}" for k in keys)
    view = str(headline.get("view") or "").strip().upper()
    turn = (f"You expected {view}; your inputs compute {v.rating}. Keep the inputs the "
            f"evidence supports and argue {v.rating}: the opening, the one_liner, the "
            "conviction and the thesis pillars all make that call. "
            if view in RATINGS and view != v.rating else "")
    still = ("These checks are still open; fix them in this reply:\n"
             + "\n".join(f"- {p}" for p in open_problems) + "\n\n") if open_problems else ""
    return (f"The code valued your model. {_computed_summary(v, data)}\n\n"
            f"The placeholders will fill in as:\n{shown}\n\n" + still + turn)


def _prose_request(headline: dict, v: valuation.Valuation, data: MarketData,
                   open_problems: list[str]) -> str:
    """Step 2: the computed valuation, and the instruction to write the pitch around it."""
    return (
        "# Step 2 of 2: the pitch\n\n"
        + _valuation_brief(headline, v, data, open_problems)
        + f"Now write the complete pitch for a {v.rating}: the ```json header again, with the "
        f"same `model` and `view` set to {v.rating}, then the markdown. Every comparison in "
        "the text — against the price, the peers, the consensus target and the cross-check — "
        "must point the way these numbers do, and the reasons you give for the gap to the "
        "Street must push your value in the direction of that gap. Keep citing computed "
        "values through placeholders. Change a model input only where the research notes "
        "show it is wrong, never to reach a rating, and say which in the text; the numbers "
        "above then move. The very first character of your reply must be the backtick "
        "opening the ```json fence."
    )


def _outline_problems(headline: dict, v: valuation.Valuation, data: MarketData,
                      fx: facts.Facts | None, digest: str) -> tuple[list[str], dict | None]:
    """The problems with the outline in a planning header, and the outline (None when the
    header has none to use)."""
    if not isinstance(headline.get("outline"), list):
        return ["the header has no `outline` list"], None
    plan_out = {"outline": headline["outline"], "assumptions": headline.get("assumptions") or []}
    values = _placeholder_values(v, data)
    lookup = {_placeholder_key(*(k.split(":", 1) + [None])[:2]) for k in values}

    def is_model(name: str) -> bool:
        return _placeholder_key(*(name.split(":", 1) + [None])[:2]) in lookup

    model = headline.get("model") if isinstance(headline.get("model"), dict) else {}
    segments = list(model.get("segments") or {}) if isinstance(model.get("segments"), dict) else []
    if fx is None:
        problems = outline.validate_structure(plan_out, is_model, digest,
                                              placeholder_names=list(values))
    else:
        problems, _ = outline.validate(plan_out, fx, is_model, values, segments, digest)
    return problems, plan_out


def _outline_ids(headline: dict) -> set[str] | None:
    sections = headline.get("outline")
    if not isinstance(sections, list):
        return None
    return {str(p.get("id")) for _, p in outline._points({"outline": sections}) if p.get("id")}


def _model_rule_problems(model: dict, point_ids: set[str] | None = None,
                         v: valuation.Valuation | None = None) -> list[str]:
    """The skill's rules on the model that the valuation does not enforce: the four
    quality assessments, a quarter never standing for a year, and revenue built from
    volume and price (a DCF's; a bank's or a REIT's earnings build has its own)."""
    if not isinstance(model, dict):
        return []
    built = (revenue_build.missing_problems(model) + conglomerate.problems(model, v)
             if v is not None and v.family != "equity" else [])
    return [f"model input: {p}" for p in model_rules.quality_problems(model, point_ids)
            + model_rules.period_problems(model) + built]


def _scenario_probability_problems(model: dict) -> list[str]:
    if not isinstance(model, dict):
        return []
    probs = model.get("scenario_probabilities")
    rationale = str(model.get("scenario_probabilities_rationale") or "").strip()
    if not isinstance(probs, dict):
        return []
    if len(rationale.split()) >= 8:
        return []
    try:
        equal_example = all(abs(float(probs.get(k, -1)) - value) < 1e-9
                            for k, value in (("bull", .25), ("base", .50), ("bear", .25)))
    except (TypeError, ValueError):
        equal_example = False
    if equal_example:
        return ["the scenario weights copy the 25/50/25 prompt example and have no "
                "`scenario_probabilities_rationale`; give evidence-based weights or explain "
                "with evidence why equal bull/base/bear weights are appropriate"]
    return ["`scenario_probabilities_rationale` is required: give one sentence tying the "
            "bull, base and bear probabilities to evidence in the research notes"]


def _cap_outline_problems(problems: list[str], limit: int = 15) -> list[str]:
    if len(problems) <= limit:
        return problems
    return problems[:limit] + [f"{len(problems) - limit} additional outline problem(s) omitted"]


async def _plan(client, base_messages: list[dict], data: MarketData, on_progress,
                usage: dict, peer_cache: dict, plan: dict | None = None,
                fx: facts.Facts | None = None, digest: str = "") -> list[dict] | None:
    """Step 1 of the two-pass write: get the model alone and value it.

    Returns the conversation the pitch is then written in, ending with the computed
    valuation, or None when no computable model came back, in which case the pitch is
    written in one pass as before. With `plan`, the chosen model is put in it as
    "headline", "valuation" and "problems", and the checked outline as "outline".

    The argument map shares the model pass, so it costs no extra model turn. In facts mode
    its citations are checked against the named fact sheet; otherwise source anchors are
    checked against the research notes and value names against computed model placeholders.
    Problems are re-asked with the model's and otherwise passed to the prose request.
    """
    with_outline = config.OUTLINE_STEP
    field = (outline.FIELD if fx is not None else outline.plain_field()) if with_outline else ""
    timeline = ("\n\n# Dated recent events from the research notes (last 18 months)\n\n"
                + outline.recent_events(digest) + "\n\n"
                "Use this timeline before selecting catalysts or forecasts. A passed event "
                "is history, not an upcoming signpost. Cite its source in the outline.\n")
    request = base_messages[1]["content"] + timeline + PLAN_REQUEST + field
    messages = [base_messages[0], {"role": "user", "content": request}]
    first_request = list(messages)
    best = None     # (problems, text, headline, valuation, outline problems, outline)
    previous_model: dict | None = None     # the last attempt's, for method revisions
    for attempt in range(1, PLAN_ATTEMPTS + 1):
        if on_progress:
            await on_progress("planning", 0)
        started = time.monotonic()
        text = _clean(await _write_turn(client, messages, on_progress, usage))
        _timed(usage, f"plan {attempt}", started)
        computed, drafted, outline_names = "", [], []
        try:
            headline = _parse_plan(text)
            peers = await _peers_for(headline, peer_cache, data)
            v = valuation.compute(headline.get("model"), data, peers,
                                  _house_cost(headline, data))
        except (PitchError, valuation.ModelInputError) as exc:
            if isinstance(exc, PitchError):
                # BABA and MSFT of 2026-10-12 lost both plans to this with nothing kept.
                _dump_bad_response(f"{data.symbol}_plan", attempt, text, usage, exc)
            problems = [f"the valuation could not be computed: {exc}"]
        else:
            model_now = headline.get("model") or {}
            problems = _scenario_probability_problems(model_now)
            problems += _model_rule_problems(model_now, _outline_ids(headline), v)
            if previous_model is not None:
                problems += [f"model input: {p}" for p in
                             model_rules.revision_problems(previous_model, model_now)]
            previous_model = model_now
            problems += [f"model input: {w}" for w in v.warnings
                         if valuation.is_bound_warning(w) or config.PLAN_REASK_WARNINGS]
            computed = _computed_summary(v, data)
            outline_names = list(_placeholder_values(v, data))
            drafted, plan_out = (_outline_problems(headline, v, data, fx, digest) if with_outline
                                 else ([], None))
            # The model's problems first, then the outline's: an outline problem left here
            # cannot be fixed later, since the prose revision does not rewrite the outline.
            # NVO's of 2026-10-11 shipped eight of them from a first plan never re-asked.
            if best is None or (len(problems), len(drafted)) <= (len(best[0]), len(best[4])):
                best = (problems, text, headline, v, drafted, plan_out)
        if (not problems and not drafted) or attempt == PLAN_ATTEMPTS:
            break
        log.warning("Model attempt %d has %d problem(s) and %d outline problem(s) — "
                    "re-asking: %s", attempt, len(problems), len(drafted),
                    "; ".join(problems + drafted))
        messages = first_request + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": _plan_fix_request(
                problems + _cap_outline_problems(drafted), computed, outline_names)},
        ]

    if best is None:
        log.warning("%s: no computable model from the planning pass (%s) — writing in "
                    "one pass", data.symbol, "; ".join(problems))
        return None
    problems, text, headline, v, drafted, plan_out = best
    prose_problems = problems + [f"model input: {w}" for w in v.warnings]
    log.info("%s model valued at %s %.2f (%s)%s", data.symbol, data.currency,
             v.recommendation_value,
             v.rating, f" with {len(problems)} open problem(s)" if problems else "")
    if plan is not None:
        plan.update(headline=headline, valuation=v, problems=problems, outline=plan_out,
                    outline_problems=drafted)
    if plan_out is None:
        if with_outline:
            log.warning("%s: no usable outline (%s) — writing the prose without one",
                        data.symbol, "; ".join(drafted))
        prose = _prose_request(headline, v, data, prose_problems)
    else:
        log.info("%s outline: %d sections, %d points%s", data.symbol, len(plan_out["outline"]),
                 sum(1 for _ in outline._points(plan_out)),
                 f", {len(drafted)} problem(s) left" if drafted else "")
        template = outline.PROSE if fx is not None else outline.PROSE_STANDARD
        prose = (_valuation_brief(headline, v, data, prose_problems)
                 + template.format(outline=outline.render(plan_out), rating=v.rating))
        if drafted:
            prose += ("\n\nThe code found these in the outline; fix them in the pitch:\n"
                      + "\n".join(f"- {p}" for p in _cap_outline_problems(drafted)))
    return first_request + [
        {"role": "assistant", "content": text},
        {"role": "user", "content": prose},
    ]


# How long into research the refused quote summary is asked for again.
REFRESH_DELAY_S = 120.0


async def _fetch_after(symbol: str, delay: float,
                       now: asyncio.Event | None = None) -> MarketData | None:
    """Fetch the market data again after `delay` seconds, or as soon as `now` is set."""
    if delay > 0:
        try:
            await asyncio.wait_for((now or asyncio.Event()).wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
    try:
        return await asyncio.wait_for(asyncio.to_thread(market_data.fetch, symbol),
                                      timeout=config.PEER_TIMEOUT_S * 2)
    except Exception as exc:
        log.warning("market data refresh for %s failed: %s", symbol, exc)
        return None


def _apply_refresh(data: MarketData, fresh: MarketData | None) -> None:
    """Take a refreshed fetch into the data the bot holds, in place, unless it was refused
    again."""
    if fresh is None:
        return
    if fresh.summary_refused:
        log.warning("Yahoo still refuses the quote summary for %s after research",
                    data.symbol)
        return
    log.info("Yahoo quote summary for %s answered after research: target %s, beta %s",
             data.symbol, fresh.analyst_target, fresh.beta)
    data.__dict__.update(fresh.__dict__)


async def _refresh_market_data(data: MarketData) -> None:
    """Ask Yahoo for the refused quote summary again, now that research has taken minutes.

    Updated in place, so the bot's cover and card get the Street target too. A second
    refusal leaves the data as it was.
    """
    _apply_refresh(data, await _fetch_after(data.symbol, 0))


def _release_memory() -> None:
    """Collect garbage and hand freed heap back to Linux. glibc keeps what a parse freed, so
    a GS pitch held ~400 MB after its filings were read and died at 512 MB on Render
    (2026-10-13). malloc_trim is glibc-only; elsewhere this is just gc.collect()."""
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


async def _gather_or_research(client, data, on_progress, usage) -> tuple[str, list[dict]]:
    """The notes from gather.py under RESEARCH_MODE=gather, from the research loop otherwise
    or when EDGAR has nothing for the symbol. The path taken goes in usage["research_path"]
    and the replay file."""
    reason = f"RESEARCH_MODE={config.RESEARCH_MODE}"
    if config.RESEARCH_MODE == "gather":
        reason = await asyncio.to_thread(gather.loop_reason, data.symbol)
        if reason is None:
            try:
                found = await gather.gather(client, data, on_progress, usage)
                usage["research_path"] = "gather"
                log.info("research for %s: gathered by code", data.symbol)
                return found
            except gather.NoFilings as exc:
                reason = str(exc)
        log.warning("research for %s: falling back to the model loop (%s)", data.symbol,
                    reason)
    else:
        log.info("research for %s: model loop (%s)", data.symbol, reason)
    usage["research_path"] = f"loop ({reason})"
    return await _research(client, data, on_progress, usage)


async def generate(
    data: MarketData,
    client: openai.AsyncOpenAI,
    on_progress: ProgressFn | None = None,
) -> Pitch:
    """Research the company, then write the pitch from what was found."""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "model": config.RESEARCH_MODEL}

    # A refused quote summary is asked for again while research runs, after Yahoo has had
    # REFRESH_DELAY_S to stop throttling, rather than after research, in series.
    research_done = asyncio.Event()
    refresh = (asyncio.create_task(_fetch_after(data.symbol, REFRESH_DELAY_S, research_done))
               if data.summary_refused else None)
    started = time.monotonic()
    try:
        digest, sources = await _gather_or_research(client, data, on_progress, usage)
        _timed(usage, "research", started)
    except BaseException:
        if refresh:
            refresh.cancel()
        raise
    if refresh:
        research_done.set()     # research ended early: ask now rather than wait out the delay
        _apply_refresh(data, await refresh)
    if config.STRUCTURED_FACTS:
        if on_progress:
            await on_progress("reconciling financial data", 0)
        started = time.monotonic()
        # After research, not beside it: both parse the same 10-K and 10-Q, and together
        # they pushed an SPGI pitch past the 512 MB of a free Render instance (2026-10-07).
        _release_memory()
        try:
            structured = await asyncio.to_thread(
                structured_research.build, data.symbol, data, news=False)
            structured_primary = {n.url for n in structured.notes
                                  if n.primary and n.url.startswith(("http://", "https://"))}
            usage["primary_sources_read"] = max(
                int(usage.get("primary_sources_read") or 0), len(structured_primary))
            digest += "\n\n" + _structured_facts_digest(structured)
            sources = _merge_sources(sources, structured)
            _release_memory()
            usage["structured_facts"] = {
                "facts": len(structured.db.facts),
                "conflicts": len(structured.db.conflicts),
                "excluded": sum(not f.usable for f in structured.db.facts.values()),
                "gaps": len(structured.db.gaps),
                "timings": structured.timings,
            }
        except Exception as exc:
            log.warning("structured SEC fact pass for %s failed; keeping the original research: %s",
                        data.symbol, exc)
            usage["structured_facts"] = {"error": str(exc)}
        _timed(usage, "structured fact reconciliation", started)
    return await write(data, digest, sources, client, on_progress, usage)


def _structured_facts_digest(run) -> str:
    """Render V2's period-aware fact database into V1's source-tagged research format."""
    db = run.db
    groups = structured_brief.select(db)
    selected = list(dict.fromkeys(fid for ids in groups.values() for fid in ids))
    sec = [fid for fid in selected if fid in db.facts and
           db.facts[fid].source_kind in ("sec_inline", "sec_company_facts") and
           db.facts[fid].data_type != "calculated"]
    filing = [fid for fid in selected if fid in db.facts and
              db.facts[fid].source_kind == "filing_table" and
              db.facts[fid].data_type != "calculated"]
    calculated = [fid for fid in selected if fid in db.facts and
                  db.facts[fid].data_type == "calculated" and
                  db.facts[fid].source_kind != "market"]
    def rows(ids: list[str]) -> str:
        out = ["What | Period | Value | Source"]
        for fid in ids:
            fact = db.facts[fid]
            if fact.usable:
                label = fact.label + (f" — {fact.segment}" if fact.segment else "")
                kind = f" [{fact.data_type}]" if fact.data_type != "reported" else ""
                out.append(f"{label} | {fact.period.label} | {fact.text()}{kind} | "
                           f"{fact.citation()}")
        return "\n".join(out)
    blocks = [
        "## [PRIMARY] sec_xbrl: Reconciled figures from SEC company facts and filing tags\n"
        + rows(sec),
        "## [PRIMARY] filing_tables: Reconciled earnings release and filing tables\n"
        + rows(filing),
        "## [PRIMARY] derived_facts: Calculations over reconciled reported figures\n"
        + rows(calculated),
    ]
    blocks = [b for b in blocks if b.split("\n", 1)[1].strip()]
    notes = ["## Structured database guidance\n"
             "These are period-reconciled records from SEC company facts, inline filing tags "
             "and earnings-release tables "
             "calculations. Use only listed usable rows and preserve the period, unit and "
             "source shown for each. Items under conflicts or exclusions are warnings, not "
             "figures to cite."]
    if db.conflicts:
        notes.append("## Structured data conflicts (excluded or resolved by source authority)\n" +
                     "\n".join(f"- {c.fact_id}: {c.note}" for c in db.conflicts[:30]))
    excluded = [f for f in db.facts.values() if not f.usable]
    if excluded:
        notes.append("## Structured data excluded from use\n" +
                     "\n".join(f"- {f.id}: {f.excluded}" for f in excluded[:30]))
    if db.gaps:
        notes.append("## Structured research gaps\n" + "\n".join(f"- {g}" for g in db.gaps))
    return "\n\n".join(blocks + notes)


def _digest_section(digest: str, heading: str) -> list[str]:
    """The bullet items under a structured-data note heading, if present."""
    match = re.search(r"^## " + re.escape(heading) + r"\s*\n(.*?)(?=^## |\Z)",
                      digest, re.M | re.S)
    if not match:
        return []
    return [line.strip().removeprefix("- ").strip()
            for line in match.group(1).splitlines() if line.strip()]


def _data_reconciliation_table(prompt_block: str) -> str:
    """The filing-versus-aggregator table added when annual statements are reconciled."""
    match = re.search(r"^## Statements matched to the company's filings\s*\n"
                      r"(.*?)(?=^## |\Z)", prompt_block, re.M | re.S)
    return match.group(1).strip() if match else ""


def _merge_sources(sources: list[dict], run) -> list[dict]:
    """Include primary URLs read by the structured fact pass in the PDF source list."""
    merged = list(sources)
    known = {s.get("url") for s in merged if isinstance(s, dict)}
    groups = structured_brief.select(run.db)
    selected = set(fid for ids in groups.values() for fid in ids)
    for fid, fact in run.db.facts.items():
        rendered_kind = (fact.source_kind in ("sec_inline", "sec_company_facts")
                         and fact.data_type != "calculated" or
                         fact.source_kind == "filing_table" and fact.data_type != "calculated" or
                         fact.data_type == "calculated" and fact.source_kind != "market")
        if (fid in selected and fact.usable and rendered_kind and fact.source_url
                and fact.source_url not in known):
            merged.append({"url": fact.source_url, "title": fact.source})
            known.add(fact.source_url)
    return merged


async def write(data: MarketData, digest: str, sources: list[dict],
                client: openai.AsyncOpenAI, on_progress: ProgressFn | None = None,
                usage: dict | None = None, peer_cache: dict | None = None) -> Pitch:
    """Everything after research: the model, the prose, the checks and the review. On its
    own, `replay.py rewrite` reruns it on a saved pitch's research."""
    usage = usage if usage is not None else {"prompt_tokens": 0, "completion_tokens": 0}

    # Research may have moved to the fallback. The write is a single call and decides
    # the pitch's quality, so the primary model gets another chance at it.
    usage["model"] = config.MODEL

    fx = facts.build(digest, data.prompt_block) if config.FACTS_MODE else None
    if fx is not None:
        figure_sheet.attach(fx, data)
    base_messages = [{"role": "system", "content": _write_system_message(fx is not None)},
                     {"role": "user", "content": _facts_user_message(data, fx) if fx else
                      _write_user_message(data, digest)}]
    peer_cache = peer_cache if peer_cache is not None else {}
    # The conversation every write attempt continues: after the planning pass it ends
    # with the computed valuation, so the prose is written knowing where the value lands.
    prefix = None
    plan: dict = {}
    if config.TWO_PASS_WRITE:
        # The checked argument map is paired with the assumptions in the same header.
        prefix = await _plan(client, base_messages, data, on_progress, usage, peer_cache, plan,
                             fx, digest)
    plan_outline = plan.get("outline")
    plan_outline_problems = list(plan.get("outline_problems") or [])
    prefix = prefix or base_messages
    messages = list(prefix)

    # Research is the expensive half — 20 minutes and ~100 sources. A write turn that
    # ignores the JSON header, or whose numbers do not hold together, is not a reason to
    # discard all of it: re-ask with the problems quoted back, and if the last attempt
    # still has some, ship the best one with them flagged on the cover.
    last_error: PitchError | None = None
    best: tuple[Pitch, list[str], str] | None = None
    writes: list[str] = []      # every raw attempt, for the replay file
    parsed = 0
    # Review and repair rounds, as the stock-thesis skill runs them: each draft is
    # reviewed, its problems repaired, and the repair reviewed again, until a round comes
    # back clean, two rounds in a row make no progress, or config.REVIEW_ROUNDS have run.
    # Until 2026-10-12 one review and one repair ran, and the repair was never re-read.
    reviews: dict[int, tuple[list[dict], str]] = {}     # id(draft) -> (issues, status)
    rounds: list[dict] = []
    review_down = ""        # why the reviewer could not run, once it has failed
    high: list[dict] = []   # the latest round's issues, for the repair request
    best_open = 0           # material reviewer points open on `best`
    last_open: int | None = None
    stalled = 0
    patch_target: str | None = None
    patch_mode = False
    patch_problems: list[str] = []
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        if on_progress:
            await (on_progress("revising", len(high)) if high else on_progress("writing", 0))

        started = time.monotonic()
        content = await _write_turn(client, messages, on_progress, usage)
        _timed(usage, f"draft {attempt}", started)
        response = _clean(content)
        if patch_mode and patch_target:
            merged = _apply_patches(patch_target, response)
            patch_mode = False
            if merged is None:
                log.warning("paragraph patch could not be applied; falling back to a full rewrite")
                if attempt == WRITE_ATTEMPTS:
                    break
                messages = prefix + [
                    {"role": "assistant", "content": patch_target},
                    {"role": "user", "content": _fix_request(
                        patch_problems + ["the paragraph patch could not be applied; return a "
                                          "complete revised pitch with the requested fixes"],
                        issues=high, cite_facts=fx is not None)},
                ]
                continue
            text = merged
        else:
            text = response
        writes.append(text)

        try:
            if not text:
                raise PitchError("The model returned no text.")
            headline, body = _split_headline(text)
        except PitchError as exc:
            # Repeating "no JSON header" to a model that is trying to search got the
            # same tool calls back verbatim; it has to be told the research is over.
            wrote_calls = bool(TOOL_CALL_TEXT.search(text))
            if wrote_calls:
                exc = PitchError("The model wrote out tool calls instead of the pitch.")
            last_error = exc
            _dump_bad_response(data.symbol, attempt, content, usage, exc)
            if attempt == WRITE_ATTEMPTS:
                break
            log.warning("Write attempt %d unusable (%s) — re-asking", attempt, exc)
            messages = prefix + [
                {"role": "assistant", "content": text[:2000]},
                {"role": "user", "content": _fix_request(
                    [f"it could not be parsed: {exc}"], wrote_calls, issues=high)},
            ]
            continue

        # The assumptions the outline declared and the code checked carry over to a draft
        # that left them out of its header.
        if plan_outline and not headline.get("assumptions") and plan_outline.get("assumptions"):
            headline["assumptions"] = plan_outline["assumptions"]
        # So does the weights' rationale, while the weights are the plan's: NVO's revision
        # of 2026-10-11 rewrote the bear case, dropped the rationale and was flagged for it.
        model_now = headline.get("model") if isinstance(headline.get("model"), dict) else {}
        model_plan = (plan.get("headline") or {}).get("model") or {}
        if (isinstance(model_plan, dict) and model_plan.get("scenario_probabilities_rationale")
                and not model_now.get("scenario_probabilities_rationale")
                and model_now.get("scenario_probabilities") == model_plan.get(
                    "scenario_probabilities")):
            model_now["scenario_probabilities_rationale"] = \
                model_plan["scenario_probabilities_rationale"]
        if plan_outline:
            body = outline.strip_invalid_markers(body, plan_outline)
            body = outline.attach_obvious_markers(body, plan_outline)
        else:
            body = outline.strip_invalid_markers(body, None)
        peers = await _peers_for(headline, peer_cache, data)
        result, problems = _finalise(headline, body, data, peers, digest, fx)
        problems += [f"outline: {problem}" for problem in plan_outline_problems
                     if f"outline: {problem}" not in problems]
        if plan_outline:
            problems += outline.marker_problems(body, plan_outline)
        if isinstance(model_plan, dict) and model_plan:
            problems += [f"method revision: {p}" for p in
                         model_rules.revision_problems(model_plan, model_now)]
        result.writer = usage["model"]
        parsed += 1
        retry_problems = _retryable_problems(result, problems)
        # The reviewer judges the argument, so it needs a computed valuation to judge. A
        # leftover mismatch in the prose does not stop it: on 2026-09-23 one stray margin
        # figure kept the reviewer away from an NVDA pitch whose real problem, a forecast
        # starting $70bn below trailing revenue, was exactly what it is there to catch.
        high = []
        if (config.REVIEW_ENABLED and result.valuation and not review_down
                and len(rounds) < config.REVIEW_ROUNDS
                and _review_trigger(result, problems, data)[0]):
            high, status = await _review_draft(client, result, data, digest, on_progress,
                                               usage, rounds[-1] if rounds else None)
            if status.startswith(UNREVIEWED):
                review_down = status
            else:
                reviews[id(result)] = (high, status)
                rounds.append(_round_entry(len(rounds) + 1, attempt, result, high,
                                           retry_problems, rounds[-1] if rounds else None))
        # Lower is better by _rank: a draft's own open reviewer points count against it;
        # a repair the reviewer could not read is charged with the points it was asked to
        # answer.
        if id(result) in reviews:
            open_review = len(_material(reviews[id(result)][0]))
        else:
            open_review = len(_material(rounds[-1]["issues"])) if rounds else 0
        if best is None or _rank(result, problems, open_review) <= _rank(best[0], best[1],
                                                                         best_open):
            best, best_open = (result, problems, text), open_review
        if not retry_problems and not high:
            break
        open_now = len(_material(high)) + len(retry_problems)
        stalled = stalled + 1 if last_open is not None and open_now >= last_open else 0
        last_open = open_now
        if stalled >= 2:
            log.warning("%s: two review rounds in a row left %d problem(s) open — stopping",
                        data.symbol, open_now)
            break
        if parsed == DRAFTS or attempt == WRITE_ATTEMPTS:
            break
        # A repair after the last round would ship unread.
        if rounds and len(rounds) >= config.REVIEW_ROUNDS and not review_down:
            break
        log.warning("Write attempt %d has %d retryable problem(s) and %d reviewer issue(s) — "
                    "re-asking: %s", attempt, len(retry_problems), len(high),
                    "; ".join(retry_problems))
        computed = _computed_summary(result.valuation, data) if result.valuation else ""
        full_rewrite = _requires_full_rewrite(
            retry_problems + [str(i.get("problem") or "") for i in high], result)
        patch_mode = bool(config.PATCH_REPAIRS and not full_rewrite)
        if rounds and rounds[-1]["draft"] == attempt:
            rounds[-1]["repair"] = "paragraphs patched" if patch_mode else "full rewrite"
        patch_target = text
        patch_problems = retry_problems
        messages = prefix + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": (
                _patch_request(retry_problems, computed, high) if patch_mode else
                _fix_request(retry_problems, computed=computed, issues=high,
                             cite_facts=fx is not None))},
        ]

    if best is None:
        raise last_error
    result, problems, text = best
    result.sources, result.usage = sources, usage
    result.source_conflicts = _digest_section(
        digest, "Structured data conflicts (excluded or resolved by source authority)")
    result.source_exclusions = _digest_section(digest, "Structured data excluded from use")
    result.source_gaps = _digest_section(digest, "Structured research gaps")
    result.data_reconciliations = _data_reconciliation_table(data.prompt_block)
    result.source_check_available = "## Structured database guidance" in digest
    result = _prepare_delivery(result, problems, plan_outline,
                               usage.get("primary_sources_read"))
    if result.checks:
        log.warning("%s shipped with %d unresolved problem(s): %s",
                    data.symbol, len(result.checks), "; ".join(result.checks))
    result.replay = replay.record(data, digest, peer_cache, writes, facts_mode=fx is not None,
                                  outline=plan_outline)
    result.replay["primary_sources_read"] = usage.get("primary_sources_read")

    if not config.REVIEW_ENABLED:
        pass
    elif result.valuation is None:
        result.review_status = "skipped (the valuation could not be computed)"
    elif id(result) in reviews:
        result.review, status = reviews[id(result)]
        result.review_status = _rounds_status(rounds, result, status)
    elif rounds:
        # The delivered draft is a repair the reviewer could not read: the points it was
        # asked to answer are shown as asked, not as closed.
        result.review, result.review_addressed = rounds[-1]["issues"], True
        result.review_status = (f"{len(rounds)} round(s); the last revision was not "
                                f"re-reviewed ({review_down or 'no round was left'})")
    elif review_down:
        result.review_status = review_down
    elif not (trigger := _review_trigger(result, result.checks, data))[0]:
        result.review_status = f"skipped ({trigger[1]})"
    else:
        # No draft was reviewed with a write left to answer it (the first draft had no
        # valuation, or could not be parsed): the one that ships is, its points left open.
        result.review, status = await _review_draft(client, result, data, digest, on_progress,
                                                    usage)
        if not status.startswith(UNREVIEWED):
            rounds.append(_round_entry(1, len(writes), result, result.review, [], None))
            result.review_status = _rounds_status(rounds, result, status)
        else:
            result.review_status = status
    _withhold_if_unreviewed(result, data.symbol)
    material = [] if result.review_addressed else _material(result.review)
    if len(material) > MAX_OPEN_MATERIAL:
        _withhold(result, f"{len(material)} material reviewer points are still open",
                  data.symbol)
    result.review_log = [{k: v for k, v in r.items() if k != "issues"} for r in rounds]
    result.replay["review_rounds"] = result.review_log
    open_review = result.review if result.review and not result.review_addressed else []
    _cap_conviction(result, len(result.checks) + len(open_review))
    result.replay["timings"] = usage.get("timings", [])
    result.replay["research_path"] = usage.get("research_path", "")
    result.replay["review_status"] = result.review_status
    log.info("%s: %d prompt tokens, %d completion tokens, written by %s; %s",
             data.symbol, usage["prompt_tokens"], usage["completion_tokens"], usage["model"],
             ", ".join(f"{stage} {seconds}s" for stage, seconds in usage.get("timings", [])))
    return result


# The skill's rule: the rating is published only when the data gates pass, the review
# finished, and at most this many material problems are still open. Until 2026-10-12 one
# surviving reviewer point withheld it.
MAX_OPEN_MATERIAL = 3
UNREVIEWED = "skipped (reviewer unavailable"


# The skill's fourteen problem codes. STYLE_ONLY is never repaired or counted.
REVIEW_CODES = ("FACT_ERROR", "PERIOD_ERROR", "UNIT_ERROR", "CITATION_ERROR",
                "CALCULATION_ERROR", "UNSUPPORTED_INFERENCE", "UNSUPPORTED_CAUSALITY",
                "ASSUMPTION_ERROR", "VALUATION_ERROR", "CONTRADICTION",
                "MISSING_COUNTERARGUMENT", "LOGICAL_ERROR", "THIN_FORWARD_VIEW", "STYLE_ONLY")
STATUSES = ("FAIL", "PARTIAL")
# How each kind of problem is repaired, from the skill's Step 6.
REPAIR_BY_CODE = {
    ("FACT_ERROR", "PERIOD_ERROR", "UNIT_ERROR", "CITATION_ERROR", "CALCULATION_ERROR"):
        "fix the figure from the research notes or the market data, for the right period, "
        "unit and line, or cite the computed placeholder",
    ("UNSUPPORTED_INFERENCE", "UNSUPPORTED_CAUSALITY", "LOGICAL_ERROR",
     "MISSING_COUNTERARGUMENT", "CONTRADICTION"):
        "rewrite that one claim to say only what its evidence supports (a cause the evidence "
        "does not show becomes a hypothesis: \"may\", \"if ... then\"), add the strongest "
        "counterargument where it is missing, or drop the claim",
    ("THIN_FORWARD_VIEW",):
        "complete the chain from the research notes: the evidence, the mechanism, an "
        "analogue and where it breaks, the latest reported rate beside the projected one, "
        "the awkward fact conceded, and the model input it becomes; if the notes hold "
        "nothing more, keep the view, lower its confidence and say it rests on judgement",
    ("ASSUMPTION_ERROR", "VALUATION_ERROR"):
        "change the model input, writing the reason first (in `method_revisions` for a "
        "method choice), from the evidence and never toward a result",
}


def _repair_hint(code: str) -> str:
    return next((hint for codes, hint in REPAIR_BY_CODE.items() if code in codes), "")


def _material(issues: list[dict] | None) -> list[dict]:
    """The points that count toward withholding the rating and toward progress: anything
    wrong or unsupported, and anything high-severity, except wording."""
    return [i for i in issues or [] if i.get("code") != "STYLE_ONLY"
            and (i.get("status") == "FAIL" or i.get("severity") == "high")]


def _to_repair(review: dict | None) -> list[dict]:
    """The reviewer's high and medium points that are not wording only."""
    return [i for i in (review or {}).get("issues", [])
            if i["severity"] in ("high", "medium") and i.get("code") != "STYLE_ONLY"]


def _round_entry(n: int, draft: int, result: Pitch, issues: list[dict],
                 code_problems: list[str], previous: dict | None) -> dict:
    """One line of the review log: what was found on which draft, and the rating then."""
    codes: dict[str, int] = {}
    for i in issues:
        codes[i.get("code") or "UNCODED"] = codes.get(i.get("code") or "UNCODED", 0) + 1
    rating = result.valuation.rating if result.valuation else None
    entry = {"round": n, "draft": draft,
             "fail": sum(1 for i in issues if i.get("status") == "FAIL"),
             "partial": sum(1 for i in issues if i.get("status") != "FAIL"),
             "material": len(_material(issues)), "codes": codes,
             "code_checks": len(code_problems), "rating": rating, "repair": None,
             "issues": issues}
    if previous and previous.get("rating") and rating and previous["rating"] != rating:
        entry["rating_change"] = (f"{previous['rating']} to {rating} after the round "
                                  f"{previous['round']} repair")
    return entry


def _rounds_status(rounds: list[dict], result: Pitch, status: str) -> str:
    n = len(rounds)
    if not n:
        return status
    if not result.review:
        return "passed" if n == 1 else f"passed in round {n}"
    material = len(_material(result.review))
    return (f"{n} round{'s' if n != 1 else ''}; {len(result.review)} point(s) open, "
            f"{material} material")


def _withhold(result: Pitch, why: str, symbol: str) -> None:
    """No rating is published; the value and the analysis still are."""
    log.warning("%s: rating withheld, %s", symbol, why)
    result.headline["recommendation"] = "NO RATING"
    result.headline["conviction"] = "Low"
    # The Model Outputs tables were built before the review closed; their rating row says
    # what the model computed, which GS on 2026-10-13 showed as SELL under NO RATING.
    result.body = re.sub(r"(?m)^(\|\s*Rating\s*\|[^|\n]*\|)\s*(BUY|HOLD|SELL)\s*\|",
                         lambda m: f"{m.group(1)} NO RATING (model: {m.group(2)}) |",
                         result.body)
    result.withheld = why


def _withhold_if_unreviewed(result: Pitch, symbol: str) -> None:
    if (result.review_status or "").startswith(UNREVIEWED):
        _withhold(result, "the review did not finish: " + result.review_status, symbol)


async def _review_draft(client, result: Pitch, data: MarketData, digest: str, on_progress,
                        usage: dict, previous: dict | None = None) -> tuple[list[dict], str]:
    """The reviewer's points to repair on a draft, those whose quoted evidence is in the
    notes, and the status when there are none ("passed", or why it was skipped).
    `previous` is the last round's log entry, shown to the reviewer to check against."""
    if on_progress:
        await on_progress("reviewing", 0)
    started = time.monotonic()
    review, reason = await _review(client, result, data, digest, usage, previous)
    _timed(usage, "review", started)
    if review is None:
        return [], f"skipped (reviewer unavailable: {reason})"
    review = _verified(review, digest, data, result.body)
    for issue in review["issues"]:
        log.info("review %s %s %s [%s] %s: %s", issue["code"], issue["status"],
                 issue["severity"], issue["section"], issue["claim"][:80],
                 issue["problem"][:200])
    issues = _to_repair(review)
    return issues, "" if issues else "passed"


def _review_trigger(result: Pitch, problems: list[str], data: MarketData) -> tuple[bool, str]:
    """(review it?, why). Under REVIEW_WHEN=flagged only a draft that looks off is reviewed:
    problems the checks left, a DCF value more than REVIEW_GAP from the Street average or the
    peer value, or neither of those to judge it by."""
    if config.REVIEW_WHEN != "flagged":
        return True, f"REVIEW_WHEN={config.REVIEW_WHEN}"
    if problems:
        return True, f"{len(problems)} problem(s) left by the code checks"
    v = result.valuation
    peer = v.peers.value if v and v.peers else None
    refs = [(label, ref) for label, ref in (("the Street average target", data.analyst_target),
                                             ("the peer value", peer)) if ref and ref > 0]
    if not refs:
        return True, "no Street target or peer value to judge the DCF by"
    for label, ref in refs:
        gap = v.recommendation_value / ref - 1
        if abs(gap) > config.REVIEW_GAP:
            return True, f"the DCF value is {gap:+.0%} from {label}"
    return False, (f"REVIEW_WHEN=flagged: the probability-weighted value is within {config.REVIEW_GAP:.0%} of "
                   + " and ".join(label for label, _ in refs)
                   + ", and the code checks left no problems")


def _rank(result: Pitch, problems: list[str], open_review: int = 0
          ) -> tuple[int, int, int, int]:
    """Lower is better: a computed valuation, then the fewest problems with the model's
    inputs, then the fewest sentences deleted and problems left open together, then the
    fewest problems the code found (a reviewer's point is an opinion; a check is not). SPGI's fourth facts-mode rewrite kept its second draft (48 sentences
    deleted) over its third (25), which had more problems, all of them in how it declared
    its assumptions. SPOT's of 2026-10-07 kept its first draft (49 deleted, 9 problems and
    5 reviewer points open) over the revision that answered the reviewer (50 deleted, 2
    problems): one sentence fewer is not worth seven problems and an ignored reviewer.
    `open_review` counts the reviewer's points raised on this draft, which it never answered."""
    model = sum(1 for p in problems if p.startswith(("model input", "the valuation")))
    left = len(problems) + open_review
    return (0 if result.valuation else 1, model, len(result.removed) + left, len(problems))


# --------------------------------------------------------------------------- review

REVIEWER_PROMPT = (config.PROMPTS_DIR / "reviewer_system.md").read_text(encoding="utf-8")
SEVERITIES = ("high", "medium", "low")


def _previous_round(previous: dict | None) -> str:
    if not previous:
        return ""
    issues = previous.get("issues") or []
    lines = [f"# Round {previous['round']} of the review\n\nThe last round raised these "
             "points and the analyst has repaired the pitch since. Check each against this "
             "draft: raise it again only if it is still open, and raise anything the repair "
             "broke."]
    lines += [f"- {i.get('code') or 'UNCODED'} {i.get('status') or ''} "
              f"[{i.get('section') or 'general'}] {i.get('claim')}: {i.get('problem')}"
              for i in issues] or ["- (none from the reviewer; the code checks sent it back)"]
    return "\n".join(lines) + "\n\n"


def _review_message(result: Pitch, data: MarketData, digest: str,
                    previous: dict | None = None) -> str:
    v = result.valuation
    model = json.dumps(result.get("model"), indent=1)
    computed = _computed_summary(v, data)
    for note in v.notes:
        computed += f" Note: {note}."

    notes = digest[:config.REVIEW_DIGEST_CHARS]
    if len(digest) > len(notes):
        notes += "\n[research notes truncated for review]"
    return (
        f"Today's date: {date.today().isoformat()}.\n\n"
        + _previous_round(previous) + _rating_moved(previous, result) +
        f"# The pitch\n\n{result.body}\n\n"
        f"# The analyst's model inputs\n\n```json\n{model}\n```\n\n"
        f"# Computed valuation\n\n{computed}\n\n"
        f"# Consensus\n\n{_consensus_line(data)}\n\n"
        f"{data.prompt_block}\n\n"
        f"# Research notes\n\n{notes}"
    )


def _rating_moved(previous: dict | None, result: Pitch) -> str:
    """The skill's rule: a repair that moves the rating is checked once more on its own."""
    was = (previous or {}).get("rating")
    now = result.valuation.rating if result.valuation else None
    if not was or not now or was == now:
        return ""
    return (f"The last repair moved the computed rating from {was} to {now}. Check the "
            "change that moved it on its own merits: was an input changed because the "
            "evidence required it, or toward a result?\n\n")


def _parse_review(text: str) -> dict | None:
    """The reviewer's verdict, or None when it did not return a usable one."""
    text = _clean(text)
    decoder = json.JSONDecoder()
    for start in (m.start() for m in re.finditer(r"\{", text)):
        try:
            obj, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("issues"), list):
            issues = [
                {"severity": str(i.get("severity", "")).lower(),
                 "code": _code(i.get("code")),
                 "status": "FAIL" if str(i.get("status", "")).upper() == "FAIL" else
                           "PARTIAL" if str(i.get("status", "")).upper() == "PARTIAL" else
                           "FAIL" if str(i.get("severity", "")).lower() == "high" else
                           "PARTIAL",
                 "section": str(i.get("section", "")), "claim": str(i.get("claim", "")),
                 "problem": str(i.get("problem", "")), "fix": str(i.get("fix", "")),
                 "evidence": str(i.get("evidence") or "")}
                for i in obj["issues"]
                if isinstance(i, dict) and str(i.get("severity", "")).lower() in SEVERITIES
                and i.get("problem")
            ]
            return {"verdict": str(obj.get("verdict", "")).lower(), "issues": issues}
    return None


async def _review(client, result: Pitch, data: MarketData, digest: str,
                  usage: dict, previous: dict | None = None) -> tuple[dict | None, str]:
    """The reviewer's verdict, or None and why every reviewer failed. Never raises.

    GLW and GOOG on 2026-09-29 shipped "skipped (reviewer unavailable)" with all four
    reviewers tried and nothing to say which failure hit which. Each failure is now named
    in the status, and a reviewer that failed fast (overloaded, rate-limited, an answer
    that was not a verdict) gets one more try after a pause; a timed-out one or a spent
    daily quota does not, since waiting again would only repeat it.
    """
    messages = [{"role": "system", "content": REVIEWER_PROMPT},
                {"role": "user", "content": _review_message(result, data, digest, previous)}]
    chain = ([config.REVIEW_MODEL] + config.REVIEW_FALLBACK_MODELS
             + ([config.REVIEW_LAST_RESORT_MODEL] if config.REVIEW_LAST_RESORT_MODEL else []))
    # The writer does not review itself, except Claude: config.REVIEW_MODEL says why.
    # A reviewer that timed out or could not be reached in an earlier round of this pitch
    # is skipped: GS on 2026-10-13 lost 15 minutes retrying the same dead ones each round.
    dead = usage.setdefault("dead_reviewers", set())
    reviewers = [m for i, m in enumerate(chain)
                 if m not in chain[:i] and _available(m)
                 and (m != usage.get("model") or claude_code.is_claude(m))]
    reviewers = [m for m in reviewers if m not in dead] or reviewers[-1:]
    failures: dict[str, str] = {}
    queue, retry = list(reviewers), []
    for attempt in (1, 2):
        for model in queue:
            review_usage = {"prompt_tokens": 0, "completion_tokens": 0, "model": model}
            if model != reviewers[-1]:
                review_usage["first_byte_timeout"] = config.REVIEW_FIRST_BYTE_S
            failures.pop(model, None)   # a retry logs its own outcome, not attempt 1's
            started = time.monotonic()
            try:
                content, _ = await asyncio.wait_for(
                    _one_stream(client, messages, None, None, review_usage),
                    timeout=config.REVIEW_TIMEOUT_S)
            except Exception as exc:  # timeout, overload, anything: try the next reviewer
                _note_quota(model, exc)
                log.warning("review on %s failed for %s (attempt %d): %s: %s", model,
                            data.symbol, attempt, type(exc).__name__, exc)
                if isinstance(exc, (TimeoutError, openai.APITimeoutError)):
                    first_byte = review_usage.get("first_byte_timeout")
                    failures[model] = (f"no first byte in {first_byte:.0f}s" if first_byte else
                                       f"no answer in {config.REVIEW_TIMEOUT_S:.0f}s")
                    dead.add(model)
                elif isinstance(exc, openai.APIConnectionError):
                    failures[model] = _failure_reason(exc)
                    dead.add(model)
                elif not _available(model):
                    failures[model] = "daily quota spent"
                elif isinstance(exc, openai.RateLimitError) or (
                        isinstance(exc, openai.APIStatusError) and exc.status_code == 503):
                    failures[model] = _failure_reason(exc)
                    # Rate limits and service overload persist across a short retry window.
                else:
                    failures[model] = _failure_reason(exc)
                    retry.append(model)
                continue
            finally:
                seconds = round(time.monotonic() - started)
                usage["prompt_tokens"] += review_usage["prompt_tokens"]
                usage["completion_tokens"] += review_usage["completion_tokens"]
                if model in failures:
                    outcome = failures[model]
                    usage.setdefault("reviewer_timings", []).append((model, seconds, outcome))
                    log.info("reviewer %s for %s: %ds (%s)", model, data.symbol, seconds, outcome)
            # An answer that is not a verdict is a failed review too: the next reviewer may
            # manage one.
            review = _parse_review(content)
            outcome = "answered" if review is not None else "unusable reply"
            usage.setdefault("reviewer_timings", []).append((model, seconds, outcome))
            log.info("reviewer %s for %s: %ds (%s)", model, data.symbol, seconds, outcome)
            if review is not None:
                log.info("%s reviewed by %s", data.symbol, model)
                return review, ""
            log.warning("review on %s for %s gave no usable verdict: %r", model, data.symbol,
                        content[:300])
            failures[model] = "answer was not a verdict"
            retry.append(model)
        if attempt == 1 and retry:
            log.info("review for %s: retrying %s in %.0fs", data.symbol, ", ".join(retry),
                     config.REVIEW_RETRY_PAUSE_S)
            await asyncio.sleep(config.REVIEW_RETRY_PAUSE_S)
            queue, retry = retry, []
        else:
            break
    reason = "; ".join(f"{m.rsplit('/', 1)[-1]}: {why}" for m, why in failures.items())
    log.warning("review skipped for %s: no reviewer returned a verdict (%s)",
                data.symbol, reason or "no reviewer available")
    return None, reason or "no reviewer available"


def _failure_reason(exc: Exception) -> str:
    """A few words for the PDF on why a reviewer failed."""
    if isinstance(exc, openai.RateLimitError):
        return "rate-limited"
    if isinstance(exc, openai.APIStatusError):
        return f"HTTP {exc.status_code}"
    if isinstance(exc, openai.APIConnectionError):
        return "connection failed"
    return type(exc).__name__


def _code(value) -> str:
    """A reviewer's code as one of REVIEW_CODES; an unknown one is kept as a judgement."""
    code = re.sub(r"[^A-Z_]", "", str(value or "").upper().replace(" ", "_"))
    return code if code in REVIEW_CODES else "LOGICAL_ERROR"


def _normalise(text: str) -> str:
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"[^a-z0-9.%$]+", " ", text.lower()).strip()


def _evidence_found(quote: str, corpus: str) -> bool:
    """A reviewer's quote, word for word up to punctuation and case, in the notes; a long
    one may be cut at either end."""
    q = _normalise(quote)
    if len(q) < 12:
        return False
    return q in corpus or (len(q) > 80 and (q[:70] in corpus or q[-70:] in corpus))


def _verified(review: dict | None, digest: str, data: MarketData, body: str) -> dict | None:
    """The review without factual objections whose quoted evidence is not in the notes.

    AVGO's reviewer of 2026-10-01 told the reader the 10-Q gave FY2026 revenue as $33bn
    (it was a segment's), and the PDF printed it as a high-severity issue still open. An
    issue that quotes nothing is a judgement about support and is kept as one.
    """
    if not review:
        return review
    corpus = _normalise(" ".join([digest, data.prompt_block, body]))
    kept = []
    for issue in review["issues"]:
        if issue.get("evidence") and not _evidence_found(issue["evidence"], corpus):
            log.info("review issue dropped, its evidence is not in the notes: %s | %r",
                     issue["problem"][:120], issue["evidence"][:120])
            continue
        kept.append(issue)
    return {**review, "issues": kept}
