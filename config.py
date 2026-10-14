"""Runtime configuration, read from the environment (.env is loaded if present)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"
OUT_DIR = BASE_DIR / "out"

load_dotenv(BASE_DIR / ".env")


def _int_set(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {int(part) for part in raw.replace(",", " ").split() if part.strip().lstrip("-").isdigit()}


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Public HTTPS origin used by Telegram to open the assumptions editor. Render exposes
# its own service URL; local runs can set WEB_APP_BASE_URL when testing through a tunnel.
WEB_APP_BASE_URL = (os.getenv("WEB_APP_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")

# NVIDIA NIM: OpenAI-compatible inference at build.nvidia.com. Keys look like "nvapi-...".
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NIM_BASE_URL = os.getenv("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")

# Optional web search API (tavily.com, keys look like "tvly-..."). The keyless engines
# work from a home connection but are blocked from cloud hosts such as Render, which
# leaves the model writing pitches from memory. Without a key, keyless is all there is.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# The Stock Finder screen's snapshot (snapshot.json on its `snapshot` branch) carries
# Street targets for every screened ticker; read when Yahoo gives none. From the local
# checkout first, then the GitHub API (the repo is private: needs GITHUB_TOKEN).
STOCK_FINDER_DIR = os.getenv("STOCK_FINDER_DIR", str(Path(__file__).resolve().parents[2] / "Stock Finder V1"))
SNAPSHOT_REPO = os.getenv("SNAPSHOT_REPO", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# SEC EDGAR refuses browser-like User-Agents (403) and asks automated clients to declare
# themselves as "<app name> <contact email>". Without it, fetch_page does not try sec.gov.
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "")

# Who writes. IDs from the NIM catalog at build.nvidia.com must support tool calling.
# "claude-..." runs through Claude Code (claude_code.py), signed in with
# CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`, or a local `claude` login; it is
# opt-in, since Claude Code's ~240 MB does not fit beside the bot on a 512 MB host.
MODEL = os.getenv("MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
# How hard Claude thinks: the write, plan and revisions; and research and the gap-fill.
CLAUDE_WRITE_EFFORT = os.getenv("CLAUDE_WRITE_EFFORT", "high")
CLAUDE_RESEARCH_EFFORT = os.getenv("CLAUDE_RESEARCH_EFFORT", "medium")

# Takes over when MODEL is overloaded. NIM's free tier saturates its biggest models
# first, and each refusal arrives only after minutes in the queue; a smaller model
# usually still answers. Must also call tools. Set it empty to disable the fallback.
# Probed 2026-09-14 while the default MODEL was queueing ~140s per call: gpt-oss-20b
# answered a research turn in under 5s, nemotron-3.5-lightning in 60-90s.
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "openai/gpt-oss-20b")

# The write turn decides the pitch's quality, so it does not drop to FALLBACK_MODEL on
# the first hiccup. It retries MODEL WRITE_PRIMARY_RETRIES times, then tries each of
# WRITE_FALLBACK_MODELS in order, then FALLBACK_MODEL as the last resort. The fallbacks
# are Gemini, on Google's OpenAI-compatible endpoint, so an NVIDIA outage does not take
# the backup down with it. On 2026-09-23 gpt-oss-20b, handed a live NVDA write, started
# the forecast $70bn below the company's trailing revenue; gemini-2.5-flash, given the
# same prompt, wrote 2,600 words that parsed and computed first time. gemini-3.8-flash
# is newer but was refusing with 503 "high demand" that day; a refusal costs about a
# second, so it is tried first. Without GEMINI_API_KEY the Gemini entries are skipped.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL",
                            "https://generativelanguage.googleapis.com/v1beta/openai/")
WRITE_FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "WRITE_FALLBACK_MODELS", "gemini-3.8-flash,gemini-2.5-flash").split(",") if m.strip()]
WRITE_PRIMARY_RETRIES = int(os.getenv("WRITE_PRIMARY_RETRIES", "2"))

# Who drives research: picking searches and pages, not writing. Research is 5-15 short
# model turns, and on NIM's free tier each one queued for a minute or more; on
# 2026-09-23 gemini-2.5-flash answered a research turn in 3.4s with six searches in
# parallel, and read tool results back in 2.5s. So with a Gemini key it defaults to
# Gemini, and MODEL keeps the write. Without a key, research stays on MODEL. Either
# way FALLBACK_MODEL takes a turn over when the research model fails.
# Claude does not queue like NIM, so a Claude MODEL researches too, with the same tools.
RESEARCH_MODEL = os.getenv("RESEARCH_MODEL") or (
    "gemini-2.5-flash" if GEMINI_API_KEY and not MODEL.startswith("claude-") else MODEL)

# How long a research turn may wait before it moves to the next research model. NIM
# sends nothing, not even headers, until a request leaves the queue, so this is
# effectively a time-to-first-byte limit. The write turn is never capped.
RESEARCH_FIRST_BYTE_S = float(os.getenv("RESEARCH_FIRST_BYTE_S", "60"))
# The same limit for the last research model in line, which has no one to hand over to.
RESEARCH_LAST_RESORT_S = float(os.getenv("RESEARCH_LAST_RESORT_S", "120"))

# Empty set means the bot answers anyone who messages it.
ALLOWED_USER_IDS = _int_set(os.getenv("ALLOWED_USER_IDS"))

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.4"))
# The write, the model and the review. Lower than research so that two runs on the same
# facts choose similar inputs: two NEE pitches of 2026-09-24/26 landed at +41% and +2%.
WRITE_TEMPERATURE = float(os.getenv("WRITE_TEMPERATURE", "0.2"))
TOP_P = float(os.getenv("TOP_P", "0.95"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "16000"))
# The plan's JSON plus the model's reasoning ran past 16,000: BABA's plan of 2026-10-12
# stopped at 16,787 tokens mid-string (finish_reason=length) and the pitch lost its plan.
WRITE_MAX_TOKENS = int(os.getenv("WRITE_MAX_TOKENS", "32000"))

# Research budget: how many tool-calling rounds before the model must write.
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "16"))
# Hard cap on searches in the research loop. Rounds alone do not bound it: a model can ask
# for ten searches in one round, and a GS run of 2026-10-13 reached 151 and timed out on
# Render. Past the cap, searches are refused and research ends after that round.
MAX_SEARCHES = int(os.getenv("MAX_SEARCHES", "40"))
# How long gather waits for the SEC filings and the 8-K/Form 4 read. From Render, GS's
# filings took about 135s on 2026-10-13; the old 90s limit read that as "no filings" and
# sent the pitch to the slow research loop.
GATHER_SEC_TIMEOUT_S = float(os.getenv("GATHER_SEC_TIMEOUT_S", "240"))

# Who gathers the research notes. "loop": the model drives up to MAX_TOOL_ROUNDS of searches
# and fetches, each round resending the whole conversation, about 20 minutes a pitch.
# "gather": code reads the filings, the 10-K's business and risk sections, the news and the
# SEC's 8-K and Form 4 filings in parallel, then one short model call picks searches for
# what is missing (gather.py). A company without SEC filings (ESLT, ASML, SPOT) still goes
# to the loop. "loop" remains available as an override for replay comparisons.
RESEARCH_MODE = os.getenv("RESEARCH_MODE", "gather").strip().lower()
# How many searches the gather mode's gap-fill call may ask for; 0 turns the call off.
GATHER_GAP_SEARCHES = int(os.getenv("GATHER_GAP_SEARCHES", "6"))
PLAN_REASK_WARNINGS = os.getenv("PLAN_REASK_WARNINGS", "0").lower() not in ("0", "false", "no", "")
AUTO_REPAIR_NO_REASK = os.getenv("AUTO_REPAIR_NO_REASK", "1").lower() not in ("0", "false", "no", "")
PATCH_REPAIRS = os.getenv("PATCH_REPAIRS", os.getenv("PATCH_RETRIES", "1")).lower() not in ("0", "false", "no", "")

# Longest the peer-multiple lookup may take before the pitch goes without a peer check.
PEER_TIMEOUT_S = float(os.getenv("PEER_TIMEOUT_S", "45"))

# Longest a single search or page fetch may take before research moves on without it.
TOOL_TIMEOUT_S = float(os.getenv("TOOL_TIMEOUT_S", "45"))

# Research floor: a model that says DONE with fewer successful searches than this is
# sent back to fill the gaps. FALLBACK_MODEL runs one query per round and stops after
# about six; this is what keeps its research from being thin. 0 disables. Keep it well
# under MAX_TOOL_ROUNDS, since fetches use rounds too.
MIN_SEARCHES = int(os.getenv("MIN_SEARCHES", "10"))

# Primary-source floor: research is not done until it has read at least this many
# filings, earnings releases or investor-relations pages. Reported figures should come
# from the company, not from a blog quoting it. Enforced by the same bounded nudges
# as MIN_SEARCHES. 0 disables. Raised from 1 after two NEE pitches of 2026-09-24/26
# took the backlog, the merger date and the exchange ratio from secondary sources and
# disagreed on all three. The latest release and 10-Q/10-K read from EDGAR count.
MIN_PRIMARY_FETCHES = int(os.getenv("MIN_PRIMARY_FETCHES", "2"))

# Every pitch shows the Street average target beside its own value. Brokers named here
# (as Yahoo writes them, e.g. "JP Morgan,Morgan Stanley") are listed under it with their
# latest target and rating. Empty by default: the average alone.
STREET_FIRMS = [f.strip() for f in os.getenv("STREET_FIRMS", "").split(",") if f.strip()]

# Yahoo refuses the quote summary (targets, beta, ratios) to Render's shared IPs on and
# off: NEE had its Street average at 01:30 on 2026-09-26 and none at 10:40. One refusal
# used to cost the whole pitch those fields. Pauses between tries at the start; one more
# try after research, minutes later.
QUOTE_RETRY_DELAYS_S = [float(s) for s in os.getenv("QUOTE_RETRY_DELAYS_S", "3,10").split(",")
                        if s.strip()]

# House cost of capital: the code computes the WACC, the model does not choose it. Two
# NEE runs used a 0.64 and a 0.75 beta and 6.5% and 7.5% WACCs, neither of which their
# own components added up to. Beta is regressed from five years of monthly prices against
# the S&P 500 (the chart endpoint, which Render can reach), Blume-adjusted; the risk-free
# rate is the live 10-year Treasury yield. The model may add a premium of up to
# WACC_PREMIUM_MAX for a risk the beta misses, and must say why.
HOUSE_WACC = os.getenv("HOUSE_WACC", "1").lower() not in ("0", "false", "no", "")
EQUITY_RISK_PREMIUM = float(os.getenv("EQUITY_RISK_PREMIUM", "0.045"))
RISK_FREE_DEFAULT = float(os.getenv("RISK_FREE_DEFAULT", "0.043"))
DEBT_SPREAD = float(os.getenv("DEBT_SPREAD", "0.015"))       # over risk-free, pre-tax
WACC_TAX_RATE = float(os.getenv("WACC_TAX_RATE", "0.21"))    # the debt tax shield
DEFAULT_BETA = 1.0
# The lowest adjusted beta the house rate uses. A beta against the S&P 500 says little
# for a stock whose price moves with another market: ESLT (Nasdaq and Tel Aviv) regressed
# at 0.11 on 2026-10-01, a 7.1% cost of equity for an Israeli defence company. 0.6 sits at
# the low end of US utilities' adjusted betas, so it leaves those untouched.
BETA_FLOOR = float(os.getenv("BETA_FLOOR", "0.6"))
WACC_PREMIUM_MAX = 0.02
# Years between the five-year forecast and the perpetuity, computed in code: revenue growth
# steps down evenly from the final forecast year's to the terminal rate, margins held.
# Without it a business still growing 10% in FY2030 drops to 3% in FY2031, which with an
# 11% WACC is most of why ten of the twelve pitches after 2026-09-26 came out HOLD or SELL.
# 0 restores the cliff. Exit-multiple terminals are unaffected: the multiple prices the rest.
GLIDE_YEARS = int(os.getenv("GLIDE_YEARS", "5"))
# Growth forever must be paid for: the terminal year reinvests growth / return on new
# capital, or the model goes back to the writer (valuation._reinvestment_warnings).
REINVESTMENT_CHECK = os.getenv("REINVESTMENT_CHECK", "1").lower() not in ("0", "false", "no", "")

# Keep the run history (history.py) as a document pinned in each chat, edited after every
# pitch, since Render's disk does not survive a deploy. 0 keeps it on local disk only.
HISTORY_PIN = os.getenv("HISTORY_PIN", "1").lower() not in ("0", "false", "no", "")

# Two-pass write: the model sends its `model` inputs alone, the code values them, and the
# prose is written with the computed numbers in hand. In one pass the prose argued the
# call the model expected: AVGO's of 2026-09-24 made a bull case over a computed SELL.
# Costs one extra write call a pitch (a Gemini request when the writer falls back).
TWO_PASS_WRITE = os.getenv("TWO_PASS_WRITE", "1").lower() not in ("0", "false", "no", "")

# Facts mode (facts.py): the code tags every figure in the research notes and the market
# data with an id, and the writer cites ids instead of typing numbers. A sentence that
# still types one in the last draft is deleted. Off until replays show it beats the
# current † marking. The checked claim map runs in both modes.
FACTS_MODE = os.getenv("FACTS_MODE", "0").lower() not in ("0", "false", "no", "")
# With the two-pass write, a checked claim map comes with the model inputs. It links claims,
# sources, counterarguments and signposts before the V1 prose is written.
OUTLINE_STEP = os.getenv("OUTLINE_STEP", "1").lower() not in ("0", "false", "no", "")
# In facts mode, the tables of the latest earnings release and 10-Q/10-K go into the notes
# row by row (filing_tables.py), up to this many characters a filing.
FILING_TABLE_CHARS = int(os.getenv("FILING_TABLE_CHARS", "14000"))

# Reconciled fact database (the code-first V2 research stage, now used to enrich V1's
# existing qualitative research): keep five fiscal years and flag material filing clashes.
ANNUAL_YEARS = int(os.getenv("ANNUAL_YEARS", "5"))
CONFLICT_TOLERANCE = float(os.getenv("CONFLICT_TOLERANCE", "0.005"))
RECONCILE_TOLERANCE = float(os.getenv("RECONCILE_TOLERANCE", "0.02"))
RESTATEMENT_LIMIT = float(os.getenv("RESTATEMENT_LIMIT", "0.20"))
STRUCTURED_FACTS = os.getenv("STRUCTURED_FACTS", "1").lower() not in ("0", "false", "no", "")
QUARTERS = int(os.getenv("QUARTERS", "8"))

# After the PDF, send the replay file (replay.py): the market data, research notes and raw
# writes, so every live pitch can be replayed against later code. A few hundred KB.
SEND_REPLAY = os.getenv("SEND_REPLAY", "1").lower() not in ("0", "false", "no", "")

# Reviewer: after the pitch passes the code checks, a second model reads it as a PM
# would and sends high-severity issues back for one revision. A different model family
# from MODEL, so it does not share the writer's blind spots. Probed 2026-09-23 with the
# writer queueing: deepseek-v4.1-flash returned valid JSON in 13s, nemotron-3-super in
# 6s (same family as the writer), glm-5.3 and kimi-k3 timed out at 90s.
REVIEW_ENABLED = os.getenv("REVIEW_ENABLED", "1").lower() not in ("0", "false", "no", "")
# OpenRouter (openrouter.ai, keys look like "sk-or-..."): a model named
# "openrouter/<vendor>/<model>" is sent there instead of NIM. With a key, the reviewer
# defaults to a free model from a different family than the writer (free models: 20
# requests a minute; 50 a day until the account has bought credits): gpt-oss-20b
# missed the NVO 2026-10-11 draft's stale catalysts and uncited market-share figures.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_FREE_REVIEWER = ("openrouter/google/gemma-4-31b-it:free" if OPENROUTER_API_KEY
                  else "openai/gpt-oss-20b")
# A Claude writer is reviewed by Claude too: Opus outreads the free models by a wide
# margin, which outweighs sharing the writer's blind spots. The free reviewers follow.
REVIEW_MODEL = os.getenv("REVIEW_MODEL") or (MODEL if MODEL.startswith("claude-")
                                             else _FREE_REVIEWER)
# The review is optional: one that has not finished in this long is skipped, never
# waited on. Covers queueing and generation together. On 2026-09-23/24 the reviewer hit
# the old 240s limit on NEE, NFLX and AVGO alike, so no pitch was reviewed; a timed-out
# review now moves on to REVIEW_FALLBACK_MODELS (Gemini, a third model family, skipped
# without GEMINI_API_KEY or once its daily quota is spent), each with its own limit.
# Opus reads the whole pitch and notes at high effort, which can run past five minutes.
REVIEW_TIMEOUT_S = float(os.getenv(
    "REVIEW_TIMEOUT_S", "600" if REVIEW_MODEL.startswith("claude-") else "300"))
REVIEW_FIRST_BYTE_S = float(os.getenv("REVIEW_FIRST_BYTE_S", str(RESEARCH_FIRST_BYTE_S)))
# Free OpenRouter models are often rate-limited upstream (Gemma 4 was, 2026-10-11), so a
# second free one goes first in line; it is Nemotron, the writer's family, hence second.
# thinkingmachines/inkling:free refuses plain API callers ("agentic harnesses" only).
REVIEW_FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "REVIEW_FALLBACK_MODELS",
    (_FREE_REVIEWER + "," if REVIEW_MODEL.startswith("claude-")
     and _FREE_REVIEWER.startswith("openrouter/") else "")
    + ("openrouter/nvidia/nemotron-3-super-120b-a12b:free," if OPENROUTER_API_KEY else "")
    + "deepseek-ai/deepseek-v4.1-flash,gemini-3.8-flash,"
    "gemini-2.5-flash").split(",") if m.strip()]
# "always" reviews every pitch. "flagged" reviews only one that looks off: a DCF value more
# than REVIEW_GAP from the Street average target or the peer value, problems left by the
# code checks, or neither reference to judge it by. A review is one more model call of
# minutes, and on clean pitches it mostly passed.
REVIEW_WHEN = os.getenv("REVIEW_WHEN", "always").strip().lower()
REVIEW_GAP = float(os.getenv("REVIEW_GAP", "0.30"))
# Review and repair rounds at most: the skill repeats them until a round comes back clean
# or two rounds in a row make no progress; this cap keeps a pitch on Render under about
# twenty minutes after research. Each round is a review and a repair of minutes each.
REVIEW_ROUNDS = max(1, int(os.getenv("REVIEW_ROUNDS", "3")))
REVIEW_DIGEST_CHARS = int(os.getenv("REVIEW_DIGEST_CHARS", "30000"))
# Reviewers that failed fast (overloaded, rate-limited, no verdict) are tried once more
# after this pause; timed-out ones and spent daily quotas are not.
REVIEW_RETRY_PAUSE_S = float(os.getenv("REVIEW_RETRY_PAUSE_S", "30"))
# Tried after every other reviewer: on NIM, so a spent Gemini quota does not leave a pitch
# unreviewed (two NEE pitches on 2026-09-26 were), and from a different family than the
# writer. Small and fast; a weaker review beats none. Empty turns it off.
REVIEW_LAST_RESORT_MODEL = os.getenv("REVIEW_LAST_RESORT_MODEL", "openai/gpt-oss-20b")

# Token efficiency. During research, only the most recent tool results are kept whole —
# older ones are trimmed, because everything is preserved in the digest for the write
# phase anyway. DIGEST_MAX_CHARS caps what that write call carries.
RESEARCH_WINDOW_CHARS = int(os.getenv("RESEARCH_WINDOW_CHARS", "24000"))
# Raised from 60k when the EDGAR filings (c.12k each) and longer primary pages went in
# ahead of everything else, so market and competitor research is not what gets cut.
DIGEST_MAX_CHARS = int(os.getenv("DIGEST_MAX_CHARS", "80000"))

# Render sets PORT for a web service and fails the deploy if nothing binds it. Unset
# locally, so a local run opens no port. See health.py.
HEALTH_PORT = int(os.getenv("PORT", "0"))

# A pitch takes minutes: give the HTTP client room.
REQUEST_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_S", "1800"))

# NIM's free tier sheds load with 503s, sometimes mid-stream. Retry the whole turn.
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BASE_DELAY_S = float(os.getenv("RETRY_BASE_DELAY_S", "5"))

DISCLAIMER = (
    "Generated by an AI research assistant for informational purposes only. This is not "
    "investment advice, not a recommendation to transact, and not a substitute for your own "
    "diligence. Figures are model estimates and may be wrong, stale, or misattributed. Verify "
    "every number against primary filings before acting on any of it."
)


def missing_required() -> list[str]:
    """Names of required settings that are absent, for a clear startup failure."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not NVIDIA_API_KEY and not MODEL.startswith(("claude-", "gemini", "openrouter/")):
        missing.append("NVIDIA_API_KEY")
    return missing
