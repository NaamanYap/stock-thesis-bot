"""Offline checks for the parts that are painful to debug against the live API.

Run: .venv/Scripts/python.exe test_offline.py
Exercises the NIM tool-calling loop, response parsing, source tracking, PDF rendering
and Telegram formatting without spending an API call. Pass --live-search to also run a
real web search and verify the research tools really work.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd
import requests
from yfinance import cache as yf_cache
from fastapi.testclient import TestClient

import charts
import config
import consistency
import facts
import figure_sheet
import gather
import outline as outline_mod
import filing_tables
import sec_xbrl
import health
import history
import market_data
import pdf_render
import pitch as pitch_mod
import provenance
import replay
import research_tools
import tg_format
import valuation

# yfinance defaults to a per-user cache directory, which may be read-only in
# sandboxed runs. Keep its SQLite cookie/timezone cache in a writable temp path.
_YF_CACHE = Path(tempfile.gettempdir()) / "stock-thesis-yfinance-cache"
_YF_CACHE.mkdir(parents=True, exist_ok=True)
yf_cache.set_cache_location(str(_YF_CACHE))

PASSES: list[str] = []
FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSES if condition else FAILS).append(f"{name}{' — ' + detail if detail else ''}")


# --------------------------------------------------------------------------- mocks

def delta(content=None, tool_calls=None, reasoning=None):
    d = NS(content=content, tool_calls=tool_calls, reasoning_content=reasoning)
    return NS(choices=[NS(delta=d, finish_reason=None)], usage=None)


def tool_delta(index, call_id, name, args):
    return delta(tool_calls=[NS(index=index, id=call_id,
                                function=NS(name=name, arguments=args))])


def final_chunk(reason="stop", prompt=100, completion=200):
    return NS(choices=[NS(delta=NS(content=None, tool_calls=None), finish_reason=reason)],
              usage=NS(prompt_tokens=prompt, completion_tokens=completion))


class FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk
        return gen()

    async def close(self):
        pass


class FakeCompletions:
    """Replays a scripted list of streams, one per create() call."""

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self._scripts.pop(0))


class FakeClient:
    def __init__(self, scripts):
        self.completions = FakeCompletions(scripts)
        self.chat = NS(completions=self.completions)


def header_obj(base_revenue: float) -> dict:
    """A header whose model passes every bound against live SPOT data."""
    return {
        "ticker": "SPOT", "company": "Spotify Technology S.A.", "exchange": "NYSE",
        "conviction": "Medium", "one_liner": "A strong franchise at a demanding valuation.",
        "thesis_pillars": ["A", "B", "C"], "key_risks": ["X", "Y"],
        "model": {
            "base_year": "FY2025", "base_revenue": round(base_revenue),
            "years": ["FY2026", "FY2027", "FY2028", "FY2029", "FY2030"],
            "segments": {
                "Premium": [round(base_revenue * f * 0.88) for f in (1.1, 1.2, 1.3, 1.4, 1.5)],
                "Ad-Supported": [round(base_revenue * f * 0.12) for f in (1.1, 1.2, 1.3, 1.4, 1.5)],
            },
            "ebit_margin": [0.155, 0.16, 0.165, 0.17, 0.18],
            "tax_rate": 0.21, "da_pct_revenue": 0.01, "capex_pct_revenue": 0.005,
            "nwc_pct_revenue_change": -0.15, "wacc": 0.09,
            "wacc_rationale": "4.3% risk-free + 1.1 beta x 4.5% ERP",
            "terminal": {"method": "perpetuity", "growth": 0.03, "exit_ev_ebit": 18},
            "gross_margin": [0.32, 0.33, 0.34, 0.35, 0.35],
            "drivers": {"Premium subscribers": {
                "unit": "mn", "base": 281, "values": [305, 330, 355, 378, 400],
                "segment": "Premium", "basis": "analyst",
                "definition": "paying subscribers at the year end, grown from the latest count"}},
            "base_rationale": "Subscribers keep growing while prices rise once a year.",
            "quality": QUALITY,
            "revenue_build_reason": "The fixture projects segments directly; the build has its own tests.",
            "thresholds": [{"measure": "Premium subscribers", "latest": "subs", "wrong_if":
                            "below 300 million", "when": "FY2026 full year", "claims": ["C1"]}],
            "bull": {"revenue_change": 0.15, "ebit_margin": [0.18, 0.22, 0.26, 0.29, 0.32],
                     "rationale": "Pricing and subscriber growth exceed the central outlook."},
            "downside": {"revenue_change": -0.12, "ebit_margin": 0.10,
                         "rationale": "royalty step-up; subscriber growth halves"},
            "scenario_probabilities": {"bull": 0.25, "base": 0.50, "bear": 0.25},
            "scenario_probabilities_rationale": "The installed base supports the central outlook; "
                                                "pricing and subscriber momentum inform upside, "
                                                "while royalty risk supports the downside.",
        },
    }


QUALITY = {area: {"rating": "adequate", "summary": "The business earns its keep and the record shows it plainly.", "claims": ["O1"]}
           for area in ("business_quality", "earnings_outlook", "balance_sheet", "competitive_position")}


# Set from live market data in run_checks, so the base year matches what Yahoo reports.
HEADER_OBJ: dict = {}
HEADER = ""

BODY = """Spotify Technology S.A. (NYSE: SPOT) | Current Price: {{price}} | Intrinsic Price: {{intrinsic}} ({{upside}}) | Recommendation: {{rating}}

Spotify is the #1 music streaming service.

## Investment Thesis

### (1) Margin expansion

- **Label economics.** Rates fell to c.52%.

## Intrinsic Valuation

Discounting at {{wacc}} gives an enterprise value of {{ev}} and an implied share price of {{intrinsic}}, {{upside}} from here. Free cash flow reaches {{fcf:FY2030}} by FY2030.

**Versus consensus.** The Street's mean target of {{consensus_target}} implies 12% upside; my value sits {{vs_consensus}} from it because I assume slower ad-supported growth.

**Returns on capital.** The business needs little capital, so returns stay well above the {{wacc}} cost of capital.

## Risks and Downside Case

**Label royalty step-up.** A 3pp rise at renewal takes the margin path down.

**Downside case.** Revenue 12% lower and a 10% margin give {{downside}} ({{downside_upside}}).

## Conclusions

The operating view is a durable streaming business; the valuation view is a demanding share price. Margin expansion is the main swing factor; no dated event that tests it is publicly scheduled.

## Model Outputs

### Definitions

Total addressable market: smartphone users ex-China.

### Financial Projections

| Metric | Unit | 2025A | 2032F |
| --- | --- | --- | --- |
| Revenue | EUR mn | 17,297 | 43,091 |
"""


def set_fixtures(data) -> None:
    global HEADER_OBJ, HEADER
    HEADER_OBJ = header_obj(data.revenue or 17000)
    HEADER = "```json\n" + json.dumps(HEADER_OBJ, indent=1) + "\n```\n"


def synthetic_data(**overrides):
    """Market data with round numbers, so the DCF can be checked by hand."""
    fields = dict(symbol="TEST", name="Test Co", exchange="NYSE", currency="USD", price=250.0,
                  market_cap=None, shares_outstanding=1e7, prompt_block="", cash=500.0,
                  debt=200.0, cash_debt_known=True, revenue=1000.0)
    fields.update(overrides)
    return market_data.MarketData(**fields)


def v_model_for_pct_driver() -> dict:
    """A model whose one driver is a percentage, which has no meaningful CAGR."""
    return synthetic_model(drivers={"Margin": {"unit": "%", "base": 50.0,
                                               "values": [51.0, 52.0, 53.0, 54.0, 55.0]}})


def synthetic_model(**overrides) -> dict:
    # Revenue grows 10% a year and FCF is 15% of revenue, so every year's PV is 150.
    model = {"base_year": "FY2026", "base_revenue": 1000,
             "years": ["FY2027", "FY2028", "FY2029", "FY2030", "FY2031"],
             "revenue": [1100, 1210, 1331, 1464.1, 1610.51],
             "ebit_margin": 0.2, "tax_rate": 0.25, "da_pct_revenue": 0.05,
             "capex_pct_revenue": 0.05, "wacc": 0.10, "gross_margin": 0.5,
             "terminal": {"method": "perpetuity", "growth": 0.02, "exit_ev_ebit": 10},
             "base_rationale": "Revenue grows ten percent a year at a steady margin.",
             "quality": QUALITY,
             "revenue_build_reason": "The fixture projects segments directly; the build has its own tests.",
             "thresholds": [{"measure": "Revenue growth", "latest": "revenue_growth",
                             "wrong_if": "below 5%", "when": "FY2027", "claims": ["C1"]}]}
    model.update(overrides)
    return model


def patch_tools(monkey: dict):
    """Swap the real network tools for canned answers."""
    original = research_tools.dispatch

    def fake_dispatch(name, args, log_to=None):
        if log_to:
            if name == "web_search":
                log_to.searches += 1
                log_to.add("https://newsroom.spotify.com/q2", "Spotify Q2 2026 Earnings")
                log_to.add("https://www.ifpi.org/report", "IFPI Global Music Report")
            else:
                log_to.fetches += 1
                if research_tools.is_primary(args.get("url", "")):
                    log_to.primary_fetches += 1
                log_to.add(args.get("url", ""), "Fetched page")
        monkey.setdefault("calls", []).append((name, args))
        return f"[stub result for {name}]"

    research_tools.dispatch = fake_dispatch
    return original


# --------------------------------------------------------------------------- tests

async def test_tool_loop(data):
    """The model searches, gets results, then writes — and we track what it read."""
    monkey: dict = {}
    original = patch_tools(monkey)
    try:
        round_one = [
            tool_delta(0, "call_a", "web_search", '{"query":'),
            tool_delta(0, "", "", ' "Spotify Q2 2026"}'),
            tool_delta(1, "call_b", "fetch_page", '{"url": "https://newsroom.spotify.com/q2"}'),
            final_chunk("tool_calls", 500, 40),
        ]
        round_two = [                      # no tool calls -> research ends
            delta(reasoning="considering the margin path"),
            delta(content="DONE"),
            final_chunk("stop", 200, 10),
        ]
        write = [
            delta(content=HEADER),
            delta(content=BODY),
            final_chunk("stop", 900, 800),
        ]
        client = FakeClient([round_one, round_two, write])

        seen: list[tuple[str, int]] = []

        async def progress(phase, value):
            seen.append((phase, value))

        result = await pitch_mod.generate(data, client, on_progress=progress)

        calls = client.completions.calls
        check("two research rounds then a write call", len(calls) == 3, f"{len(calls)} call(s)")
        check("both tool calls executed", len(monkey["calls"]) == 2, str(monkey["calls"]))
        check("split tool arguments reassembled",
              # The two calls run on parallel threads, so they are recorded in either order.
              "Spotify Q2 2026" in [a.get("query") for _, a in monkey["calls"]])
        check("research phase omits the exemplar",
              "Style exemplar" not in calls[0]["messages"][0]["content"])
        check("research phase omits the statements",
              "MARKET DATA" not in calls[0]["messages"][1]["content"])
        check("write phase carries the exemplar",
              "Style exemplar" in calls[2]["messages"][0]["content"])
        check("write phase carries the statements and notes",
              "MARKET DATA" in calls[2]["messages"][1]["content"]
              and "Research notes" in calls[2]["messages"][1]["content"])
        check("write phase is a fresh 2-message context", len(calls[2]["messages"]) == 2,
              f"{len(calls[2]['messages'])} messages")
        check("write phase offers no tools", "tools" not in calls[2])
        check("findings reached the digest",
              "stub result for web_search" in calls[2]["messages"][1]["content"])
        check("digest tags fetched pages by source quality",
              "[PRIMARY] fetch_page: https://newsroom.spotify.com/q2"
              in calls[2]["messages"][1]["content"]
              and "[SNIPPETS] web_search" in calls[2]["messages"][1]["content"])
        check("write prompt carries the sell-side consensus",
              "Sell-side consensus" in calls[2]["messages"][1]["content"])
        check("consensus placeholders filled",
              "{{" not in result.body and "Street's mean target of USD" in result.body)
        check("research context stays small",
              len(str(calls[1]["messages"])) < len(str(calls[2]["messages"])),
              "research round < write call")
        check("headline parsed", result.get("ticker") == "SPOT")
        check("cover numbers come from the computed model",
              result.valuation is not None
              and result.get("upside_pct") == round(result.valuation.recommendation_upside)
              and result.get("recommendation") == result.valuation.rating
              and result.get("current_price") == data.price, str(result.headline))
        check("clean pitch carries no unresolved checks", result.checks == [], str(result.checks))
        check("placeholders filled in the prose",
              "{{" not in result.body and result.valuation.rating in result.body)
        check("generated tables replace typed ones",
              "### 4. DCF Build" in result.body and "43,091" not in result.body
              and "### 1b. Definitions" in result.body and "Total addressable market" in result.body)
        check("body excludes the JSON fence", result.body.startswith("Spotify Technology"))
        # The fetched URL is one the search already logged, so it must not appear twice.
        check("sources collected and deduped", len(result.sources) == 2,
              f"{[s['url'] for s in result.sources]}")
        check("usage accumulated across both phases",
              result.usage["prompt_tokens"] == 1600
              and result.usage["completion_tokens"] == 850,
              str(result.usage))
        check("progress reported research and writing",
              any(p == "searching" for p, _ in seen) and any(p == "streaming" for p, _ in seen))
        check("reasoning_content surfaced as thinking", ("thinking", 0) in seen)
        return result
    finally:
        research_tools.dispatch = original


async def test_claude_code_route(data):
    """A claude- model researches as one Claude Code session with the bot's own tools, and
    writes through Claude Code; when Claude Code fails, the NIM fallback takes over."""
    import claude_code
    monkey: dict = {}
    original = patch_tools(monkey)
    saved = (config.MODEL, config.RESEARCH_MODEL, claude_code.research, claude_code.complete)
    sessions: list[dict] = []
    writes: list[dict] = []

    async def fake_research(model, messages, schemas, run_tool, follow_up, effort,
                            max_turns, on_progress, usage):
        sessions.append({"model": model, "effort": effort, "tools":
                         [s["function"]["name"] for s in schemas], "messages": messages})
        await run_tool("web_search", {"query": "Spotify Q2 2026"})
        await run_tool("fetch_page", {"url": "https://newsroom.spotify.com/q2"})
        await run_tool("fetch_page", {"url": "https://newsroom.spotify.com/q2"})
        sessions[-1]["follow_up"] = follow_up("DONE")

    async def fake_complete(model, messages, effort, on_progress, usage):
        writes.append({"model": model, "effort": effort, "messages": messages})
        usage["prompt_tokens"] += 900
        usage["completion_tokens"] += 800
        return HEADER + BODY

    try:
        config.MODEL = config.RESEARCH_MODEL = "claude-opus-5-5"
        claude_code.research, claude_code.complete = fake_research, fake_complete
        result = await pitch_mod.generate(data, FakeClient([]))
        check("Claude researches with the bot's tools",
              len(sessions) == 1 and sessions[0]["tools"] == ["web_search", "fetch_page"]
              and sessions[0]["effort"] == config.CLAUDE_RESEARCH_EFFORT, str(sessions))
        check("Claude's tool calls run through the bot's guards",
              [n for n, _ in monkey["calls"]] == ["web_search", "fetch_page"],
              str(monkey["calls"]))
        check("no nudge once the floors are met", sessions[0]["follow_up"] is None)
        check("Claude writes from the research notes",
              len(writes) == 1 and writes[0]["effort"] == config.CLAUDE_WRITE_EFFORT
              and "stub result for web_search" in writes[0]["messages"][1]["content"])
        check("Claude recorded as the writer", result.writer == "claude-opus-5-5",
              str(result.writer))

        async def down(*args, **kwargs):
            raise claude_code.ClaudeCodeError("usage limit reached")

        claude_code.research = claude_code.complete = down
        client = FakeClient([[delta(content="DONE"), final_chunk()],
                             [delta(content=HEADER + BODY), final_chunk()]])
        result = await pitch_mod.generate(data, client)
        check("a failed Claude Code falls back to the NIM fallback",
              [c["model"] for c in client.completions.calls]
              == [config.FALLBACK_MODEL, config.FALLBACK_MODEL]
              and result.writer == config.FALLBACK_MODEL,
              str([c["model"] for c in client.completions.calls]))
    finally:
        (config.MODEL, config.RESEARCH_MODEL, claude_code.research,
         claude_code.complete) = saved
        research_tools.dispatch = original

    system, prompt = claude_code._split([
        {"role": "system", "content": "S"}, {"role": "user", "content": "plan please"},
        {"role": "assistant", "content": "the plan"}, {"role": "user", "content": "now write"}])
    check("a multi-turn request becomes one transcript prompt",
          system == "S" and prompt.index("plan please") < prompt.index("the plan")
          < prompt.index("now write"), prompt)


async def test_request_shape(data):
    # No tool calls in round 1, so research ends immediately and the write call follows.
    client = FakeClient([[delta(content="DONE"), final_chunk()],
                         [delta(content=HEADER + BODY), final_chunk()]])
    await pitch_mod.generate(data, client)
    research, write = client.completions.calls

    check("model from config", research["model"] == config.MODEL)
    check("streaming enabled", research["stream"] is True)
    check("usage requested in stream", research["stream_options"]["include_usage"] is True)
    check("both tools offered in research",
          {t["function"]["name"] for t in research["tools"]} == {"web_search", "fetch_page"})
    check("tool_choice auto", research["tool_choice"] == "auto")
    check("research brief tells the model not to write",
          "NOT writing the pitch" in research["messages"][0]["content"])
    check("research prompt carries today's date",
          date.today().isoformat() in research["messages"][1]["content"])
    check("write call carries market data", "MARKET DATA" in write["messages"][1]["content"])
    check("research turn capped at the first-byte limit",
          research.get("timeout") == config.RESEARCH_FIRST_BYTE_S)
    check("write turn keeps the long timeout", "timeout" not in write)


async def test_budget_exhaustion(data):
    """When the model will not stop researching, we take the tools away."""
    monkey: dict = {}
    original = patch_tools(monkey)
    try:
        searching = [tool_delta(0, "c1", "web_search", '{"query":"x"}'),
                     final_chunk("tool_calls")]
        scripts = [list(searching) for _ in range(config.MAX_TOOL_ROUNDS)]
        scripts.append([delta(content=HEADER + BODY), final_chunk()])  # the write phase
        client = FakeClient(scripts)

        result = await pitch_mod.generate(data, client)
        write = client.completions.calls[-1]

        check("research stops at the budget",
              len(client.completions.calls) == config.MAX_TOOL_ROUNDS + 1,
              f"{len(client.completions.calls)} calls")
        check("write phase drops the tools", "tools" not in write)
        check("pitch still produced after budget exhaustion", result.get("ticker") == "SPOT")
    finally:
        research_tools.dispatch = original


async def test_research_floor(data):
    """A model that stops short of MIN_SEARCHES is sent back, but not forever."""
    monkey: dict = {}
    original = patch_tools(monkey)
    original_floor = config.MIN_SEARCHES
    config.MIN_SEARCHES = 2
    try:
        def search(query):
            return [tool_delta(0, "c1", "web_search", '{"query": "%s"}' % query),
                    final_chunk("tool_calls")]
        done = [delta(content="DONE"), final_chunk()]
        write = [delta(content=HEADER + BODY), final_chunk()]

        client = FakeClient([search("Spotify Q2 2026"), done, search("Spotify rivals"), done,
                             write])
        await pitch_mod.generate(data, client)
        calls = client.completions.calls
        check("early DONE is sent back for more searches",
              len(monkey["calls"]) == 2 and len(calls) == 5, f"{len(calls)} calls")
        research = calls[0]["messages"]
        check("research brief states the floor", "at least 2 searches" in research[0]["content"])
        nudges = [m["content"] for m in research if m["role"] == "user"][1:]
        check("nudge lists the queries already run",
              len(nudges) == 1 and "Spotify Q2 2026" in nudges[0], str(nudges))

        client = FakeClient([done] * (pitch_mod.MAX_RESEARCH_NUDGES + 1) + [write])
        result = await pitch_mod.generate(data, client)
        check("a model that will not search is not nudged forever",
              len(client.completions.calls) == pitch_mod.MAX_RESEARCH_NUDGES + 2
              and result.get("ticker") == "SPOT",
              f"{len(client.completions.calls)} calls")
    finally:
        config.MIN_SEARCHES = original_floor
        research_tools.dispatch = original


async def test_parsing_resilience(data):
    """Reasoning models leak think tags and drop code fences."""
    def scripted(*write_texts):
        """A research turn that calls no tools, then one response per write attempt."""
        return FakeClient(
            [[delta(content="DONE"), final_chunk()]]
            + [[delta(content=t), final_chunk()] for t in write_texts]
        )

    unfenced = "Here is the pitch.\n" + json.dumps(HEADER_OBJ) + "\n\n" + BODY
    result = await pitch_mod.generate(data, scripted(unfenced))
    check("unfenced nested JSON header recovered",
          result.valuation is not None and result.get("ticker") == "SPOT")
    check("body survives unfenced header", result.body.startswith("Spotify Technology"))

    thinky = "<think>Let me work through the DCF.</think>\n" + HEADER + BODY
    result = await pitch_mod.generate(data, scripted(thinky))
    check("think tags stripped", result.get("ticker") == "SPOT" and "<think>" not in result.body)

    bad = "Just prose, no header."
    try:
        await pitch_mod.generate(data, scripted(*[bad] * pitch_mod.WRITE_ATTEMPTS))
        check("missing JSON header raises PitchError", False, "no exception")
    except pitch_mod.PitchError as exc:
        check("missing JSON header raises PitchError", "JSON header" in str(exc))

    # The research phase costs ~20 minutes; a single malformed write must not bin it.
    result = await pitch_mod.generate(data, scripted(bad, HEADER + BODY))
    check("write turn retried after a bad header", result.get("ticker") == "SPOT")

    client = scripted(bad, HEADER + BODY)
    await pitch_mod.generate(data, client)
    check("retry costs one extra call, not a new research phase",
          len(client.completions.calls) == 3)

    try:
        await pitch_mod.generate(data, scripted(*[""] * pitch_mod.WRITE_ATTEMPTS))
        check("empty response raises PitchError", False, "no exception")
    except pitch_mod.PitchError as exc:
        check("empty response raises PitchError", "no text" in str(exc))

    # The write turn has no tools. A model that still wants to search writes the calls
    # out as text (RTX, 2026-09-14); the re-ask must tell it the research is over.
    check("write prompt offers no tools",
          "no tools" in pitch_mod._write_system_message()
          and "web_search(query)" not in pitch_mod._write_system_message())

    calls_as_text = ('tool_code\nweb_search\n{"query": "RTX Q2 2026 earnings call"}\n'
                     'tool_code\nweb_search\n{"query": "RTX free cash flow"}')
    client = scripted(calls_as_text, HEADER + BODY)
    result = await pitch_mod.generate(data, client)
    reask = client.completions.calls[2]["messages"][-1]["content"]
    check("tool calls written as text are re-asked with tools ruled out",
          result.get("ticker") == "SPOT" and "no tools" in reask)

    try:
        await pitch_mod.generate(data, scripted(*[calls_as_text] * pitch_mod.WRITE_ATTEMPTS))
        check("tool calls written as text raise a clear PitchError", False, "no exception")
    except pitch_mod.PitchError as exc:
        check("tool calls written as text raise a clear PitchError", "tool calls" in str(exc))


async def test_research_on_gemini(data):
    """Research can run on Gemini, which sends each tool call whole and unindexed."""
    whole = [delta(tool_calls=[NS(index=None, id="function-call-1", function=NS(
                 name="web_search", arguments='{"query": "Spotify Q3 2026 earnings"}'))]),
             delta(tool_calls=[NS(index=None, id="function-call-2", function=NS(
                 name="web_search", arguments='{"query": "Spotify segments"}'))]),
             final_chunk("tool_calls")]
    monkey: dict = {}
    original = patch_tools(monkey)
    saved = config.GEMINI_API_KEY, config.RESEARCH_MODEL, pitch_mod._gemini_client
    try:
        config.GEMINI_API_KEY, config.RESEARCH_MODEL = "test-key", "gemini-2.5-flash"
        gemini = FakeClient([whole, [delta(content="DONE"), final_chunk()]])
        nim = FakeClient([[delta(content=HEADER + BODY), final_chunk()]])
        pitch_mod._gemini_client = gemini
        result = await pitch_mod.generate(data, nim)
        check("Gemini's unindexed tool calls read as separate calls",
              sorted(a.get("query") for _, a in monkey["calls"])
              == ["Spotify Q3 2026 earnings", "Spotify segments"], str(monkey["calls"]))
        check("research on Gemini, write stays on the primary",
              {k["model"] for k in gemini.completions.calls} == {"gemini-2.5-flash"}
              and [k["model"] for k in nim.completions.calls] == [config.MODEL]
              and result.writer == config.MODEL)
    finally:
        config.GEMINI_API_KEY, config.RESEARCH_MODEL, pitch_mod._gemini_client = saved
        research_tools.dispatch = original


async def test_write_fallback(data):
    """The write retries the primary, then Gemini, then NIM's small fallback."""
    import httpx2
    import openai

    overloaded = openai.APIError("Service temporarily overloaded", request=None, body=None)

    class Scripted(FakeCompletions):
        """Fails write calls to the models in `fail`; research always answers."""

        def __init__(self, fail):
            super().__init__([])
            self.fail, self.models = fail, []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            self.models.append(kwargs["model"])
            if kwargs["model"] in self.fail and "tools" not in kwargs:  # writes only
                raise overloaded
            text = "DONE" if "tools" in kwargs else HEADER + BODY
            return FakeStream([delta(content=text), final_chunk()])

    def client_for(fail):
        c = FakeClient([])
        c.completions = Scripted(fail)
        c.chat = NS(completions=c.completions)
        return c

    saved = (config.GEMINI_API_KEY, config.RETRY_BASE_DELAY_S, pitch_mod._gemini_client,
             config.WRITE_FALLBACK_MODELS)
    config.RETRY_BASE_DELAY_S = 0.01
    config.WRITE_FALLBACK_MODELS = ["gemini-3.8-flash", "gemini-2.5-flash"]
    try:
        primary_down = {config.MODEL}
        writes = lambda c: [k["model"] for k in c.completions.calls if "tools" not in k]

        config.GEMINI_API_KEY = "test-key"
        client = client_for(primary_down)
        pitch_mod._gemini_client = client  # one fake serves both providers
        result = await pitch_mod.generate(data, client)
        retries = [config.MODEL] * (1 + config.WRITE_PRIMARY_RETRIES)
        check("write retries the primary before falling back",
              writes(client) == retries + ["gemini-3.8-flash"], " -> ".join(writes(client)))
        check("the delivered pitch names its backup writer",
              result.writer == "gemini-3.8-flash"
              and "backup model" in tg_format.summary_card(result, data))

        client = client_for(primary_down | {"gemini-3.8-flash"})
        pitch_mod._gemini_client = client
        result = await pitch_mod.generate(data, client)
        check("a busy Gemini version hands on to the next",
              result.writer == "gemini-2.5-flash", " -> ".join(writes(client)))

        config.GEMINI_API_KEY = ""
        client = client_for(primary_down)
        result = await pitch_mod.generate(data, client)
        check("without a Gemini key the NIM fallback writes",
              result.writer == config.FALLBACK_MODEL
              and not any(m.startswith("gemini") for m in client.completions.models),
              " -> ".join(writes(client)))

        client = client_for({config.MODEL, config.FALLBACK_MODEL})
        try:
            await pitch_mod.generate(data, client)
            check("every writer failing raises a clear PitchError", False, "no exception")
        except pitch_mod.PitchError as exc:
            check("every writer failing raises a clear PitchError", "Every model" in str(exc))

        config.GEMINI_API_KEY = "test-key"
        seen = {}
        nim, gemini = client_for(set()), client_for(set())
        pitch_mod._gemini_client = gemini
        await pitch_mod._one_stream(nim, [], None, None, {"prompt_tokens": 0,
                                    "completion_tokens": 0, "model": "gemini-2.5-flash"})
        check("Gemini models are sent to Google's endpoint",
              gemini.completions.models == ["gemini-2.5-flash"] and not nim.completions.models)
    finally:
        (config.GEMINI_API_KEY, config.RETRY_BASE_DELAY_S, pitch_mod._gemini_client,
         config.WRITE_FALLBACK_MODELS) = saved

    # Gemini repeats a running usage total on every chunk; count each call once.
    running = [NS(choices=[NS(delta=NS(content="a", tool_calls=None), finish_reason=None)],
                  usage=NS(prompt_tokens=1000, completion_tokens=n)) for n in (10, 20, 30)]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "model": config.MODEL}
    await pitch_mod._one_stream(FakeClient([running]), [], None, None, usage)
    check("cumulative usage counted once per call",
          usage["prompt_tokens"] == 1000 and usage["completion_tokens"] == 30, str(usage))


async def test_transient_retry(data):
    """NIM sheds load mid-stream; the turn must be replayed, not lost."""
    import httpx2
    import openai

    class FlakyCompletions(FakeCompletions):
        def __init__(self, scripts, fail_times, error):
            super().__init__(scripts)
            self.fail_times = fail_times
            self.error = error
            self.attempts = 0
            self.models: list[str] = []

        async def create(self, **kwargs):
            self.attempts += 1
            self.models.append(kwargs["model"])
            if self.attempts <= self.fail_times:
                raise self.error
            return await super().create(**kwargs)

    def flaky(fail_times, error):
        c = FakeClient([])
        c.completions = FlakyCompletions(
            [[delta(content="DONE"), final_chunk()],
             [delta(content=HEADER + BODY), final_chunk()]], fail_times, error)
        c.chat = NS(completions=c.completions)
        return c

    original_delay = config.RETRY_BASE_DELAY_S
    config.RETRY_BASE_DELAY_S = 0.01          # keep the test fast
    try:
        # The exact failure that killed the live run: a bare APIError mid-stream.
        overloaded = openai.APIError("Service temporarily overloaded", request=None, body=None)
        client = flaky(2, overloaded)
        result = await pitch_mod.generate(data, client)
        check("mid-stream APIError is retried", result.get("ticker") == "SPOT")
        check("retried the failing turn twice", client.completions.attempts == 4,
              f"{client.completions.attempts} attempts")

        # An overloaded primary hands the turn straight to the fallback; the write,
        # which decides the pitch's quality, goes back to the primary.
        client = flaky(1, overloaded)
        await pitch_mod.generate(data, client)
        check("overload moves research to the fallback, write back to primary",
              client.completions.models
              == [config.MODEL, config.FALLBACK_MODEL, config.MODEL],
              " -> ".join(client.completions.models))
        check("the last research model gets the longer cap, not none",
              client.completions.calls[0].get("timeout") == config.RESEARCH_LAST_RESORT_S)

        # A queue that outlasts the research limit surfaces as a timeout — sometimes
        # mid-stream, as the raw httpx2 error the SDK does not wrap.
        client = flaky(1, httpx2.ReadTimeout("read timed out"))
        await pitch_mod.generate(data, client)
        check("research timeout hands the turn to the fallback",
              client.completions.models[:2] == [config.MODEL, config.FALLBACK_MODEL],
              " -> ".join(client.completions.models))

        # A connection dropped mid-stream is as transient as a timeout (NVDA, 2026-09-23).
        client = flaky(1, httpx2.ReadError("connection reset"))
        result = await pitch_mod.generate(data, client)
        check("connection dropped mid-stream is retried", result.get("ticker") == "SPOT")

        # A stream abandoned between chunks must still be closed, or the garbage
        # collector does it later and logs a traceback from httpcore.
        class BrokenStream(FakeStream):
            closed = False

            def __aiter__(self):
                async def gen():
                    yield delta(content="partial")
                    raise httpx2.ReadTimeout("read timed out")
                return gen()

            async def close(self):
                BrokenStream.closed = True

        async def broken_create(**kwargs):
            return BrokenStream([])

        broken = NS(chat=NS(completions=NS(create=broken_create)))
        try:
            await pitch_mod._one_stream(broken, [], None, None,
                                        {"prompt_tokens": 0, "completion_tokens": 0})
        except httpx2.ReadTimeout:
            pass
        check("stream closed when abandoned mid-way", BrokenStream.closed)

        original_fallback = config.FALLBACK_MODEL
        config.FALLBACK_MODEL = ""
        try:
            client = flaky(1, overloaded)
            await pitch_mod.generate(data, client)
            check("empty FALLBACK_MODEL keeps retrying the primary",
                  set(client.completions.models) == {config.MODEL})
        finally:
            config.FALLBACK_MODEL = original_fallback

        # Exhausting retries must surface a clear message, not a raw SDK traceback.
        client = flaky(99, overloaded)
        try:
            await pitch_mod.generate(data, client)
            check("exhausted retries raise PitchError", False, "no exception")
        except pitch_mod.PitchError as exc:
            check("exhausted retries raise PitchError", "overloaded" in str(exc).lower())

        # A bad request will fail identically forever — do not burn retries on it.
        import httpx2  # the HTTP layer the openai SDK is built on
        bad_response = httpx2.Response(
            400, request=httpx2.Request("POST", "https://integrate.api.nvidia.com/v1")
        )
        client = flaky(99, openai.BadRequestError("bad model", response=bad_response, body=None))
        try:
            await pitch_mod.generate(data, client)
            check("fatal errors are not retried", False, "no exception")
        except pitch_mod.PitchError:
            check("a rejected request moves on, never retries the same model",
                  client.completions.models == [config.MODEL, config.FALLBACK_MODEL],
                  " -> ".join(client.completions.models))
    finally:
        config.RETRY_BASE_DELAY_S = original_delay


def test_valuation_math():
    """The DCF is computed by hand-checkable arithmetic, not by the model."""
    data = synthetic_data()
    v = valuation.compute(synthetic_model(), data)
    near = lambda a, b, tol=0.01: abs(a - b) <= tol
    check("every year's PV computed", all(near(r.pv_fcf, 150) for r in v.rows),
          str([round(r.pv_fcf, 3) for r in v.rows]))
    check("PV of forecast FCF summed correctly", near(v.pv_fcf, 750), f"{v.pv_fcf:.3f}")
    check("perpetuity terminal value discounted", near(v.pv_terminal, 1912.5), f"{v.pv_terminal:.3f}")
    check("bridge adds cash and subtracts debt",
          near(v.equity_value, v.enterprise_value + 500 - 200))
    check("per-share value from market data share count", near(v.per_share, 296.25),
          f"{v.per_share:.4f}")
    check("rating follows the rule", v.rating == "HOLD" and near(v.upside_pct, 18.5))
    check("second terminal method is the cross-check", near(v.cross_check_per_share, 305.0),
          f"{v.cross_check_per_share}")
    check("clean inputs raise no warnings", v.warnings == [], str(v.warnings))

    bear = synthetic_model(bear={"wacc": 0.12})
    bear_value = valuation.compute(bear, data)
    check("a WACC-only bear with a large value gap is not called close to base",
          abs(bear_value.downside_per_share / bear_value.price * 100 - bear_value.upside_pct) >= 10
          and not any("close to the base case" in w for w in bear_value.warnings),
          str(bear_value.warnings))
    check("bound warning predicate recognizes WACC bounds only",
          valuation.is_bound_warning("WACC of 20% is outside 6%-14%")
          and valuation.is_bound_warning("tax rate 45% is outside 0-40%; it was held to that range")
          and valuation.is_bound_warning("`terminal.new_capital_return` was held to that range")
          and valuation.is_bound_warning("exit multiple of 30x is outside range; it was held to that range")
          and valuation.is_bound_warning("scenario values are out of order")
          and valuation.is_bound_warning("the bear case comes out not below the base case")
          and not valuation.is_bound_warning("WACC rationale is unclear"))
    check("the house WACC held to its floor is not sent back to the model",
          not valuation.is_bound_warning("house WACC was 2.6% computed, held to 6%"))

    ten = synthetic_model(years=[f"FY{y}" for y in range(2027, 2037)],
                          revenue=[1000 * 1.1 ** k for k in range(1, 11)])
    check("a ten-year forecast is accepted",
          not any("forecast runs" in w for w in valuation.compute(ten, synthetic_data()).warnings))
    eleven = synthetic_model(years=[f"FY{y}" for y in range(2027, 2038)],
                             revenue=[1000 * 1.1 ** k for k in range(1, 12)])
    check("an eleven-year forecast is sent back",
          any("use 5 to 10" in w for w in valuation.compute(eleven, synthetic_data()).warnings))

    behind = valuation.compute(synthetic_model(), synthetic_data(ttm_revenue=1500.0))
    check("forecast starting below trailing revenue flagged",
          any("last four quarters" in w for w in behind.warnings), str(behind.warnings))
    check("forecast at or above trailing revenue not flagged",
          valuation.compute(synthetic_model(), synthetic_data(ttm_revenue=1150.0)).warnings == [])

    fx = valuation.compute(synthetic_model(), synthetic_data(reporting_currency="EUR", fx=1.1))
    check("reporting currency converted per share", near(fx.per_share, 325.875),
          f"{fx.per_share:.4f}")

    pct = valuation.compute(synthetic_model(ebit_margin=20, wacc=10), data)
    check("percent written as 20 read as 0.20", near(pct.per_share, 296.25))

    check("rating boundaries",
          [valuation.rating_for(x) for x in (25.0, 25.1, -10.0, -10.1)]
          == ["HOLD", "BUY", "HOLD", "SELL"])

    loose = valuation.compute(synthetic_model(
        wacc=0.20, terminal={"method": "perpetuity", "growth": 0.05},
        segments={"A": [500] * 5, "B": [100] * 5}), synthetic_data(revenue=2000.0))
    joined = " | ".join(loose.warnings)
    check("out-of-bounds inputs flagged",
          "WACC" in joined and "terminal growth" in joined and "segments sum" in joined
          and "base revenue" in joined, joined)

    for label, model in [("missing model", None),
                         ("growth at the WACC",
                          synthetic_model(terminal={"method": "perpetuity", "growth": 0.095})),
                         ("wrong-length series", synthetic_model(ebit_margin=[0.2, 0.2]))]:
        try:
            valuation.compute(model, data)
            check(f"uncomputable model rejected: {label}", False, "no exception")
        except valuation.ModelInputError:
            check(f"uncomputable model rejected: {label}", True)

    md = valuation.model_outputs_markdown(v)
    check("tables rendered from the model, in the house order",
          "296.25" in md and "Cross-check" in md
          and md.index("### 1a.") < md.index("### 2.") < md.index("### 3.") < md.index("### 7."))


STATEMENTS = """### Income statement (values in millions of reporting currency)

| Line item | 2025-12-31 | 2024-12-31 | 2023-12-31 |
| --- | --- | --- | --- |
| Total Revenue | 45,183 | 39,001 | 33,723 |
| Operating Income | 13,327 | 10,418 | 6,954 |
| EBITDA | 30,255 | 26,315 | 21,508 |

### Cash flow statement (values in millions of reporting currency)

| Line item | 2025-12-31 | 2024-12-31 | 2023-12-31 |
| --- | --- | --- | --- |
| Operating Cash Flow | 10,149 | 7,361 | 7,274 |
| Capital Expenditure | -688 | -440 | -349 |
| Free Cash Flow | 9,461 | 6,922 | 6,926 |
"""


RETURNS_BLOCK = """| Industry | Software - Application |

### Income statement (values in millions of reporting currency)

| Line item | 2026-12-31 | 2025-12-31 |
| --- | --- | --- |
| Total Revenue | 1,000 | 900 |
| Operating Income | 180 | 150 |

### Balance sheet (values in millions of reporting currency)

| Line item | 2026-12-31 | 2025-12-31 |
| --- | --- | --- |
| Cash Cash Equivalents And Short Term Investments | 300 | 200 |
| Total Debt | 500 | 600 |
| Goodwill And Other Intangible Assets | 400 | 400 |
| Stockholders Equity | 800 | 600 |

### Quarterly income statement (values in millions of reporting currency)

| Line item | 2026-12-31 | 2026-09-30 | 2026-06-30 | 2026-03-31 |
| --- | --- | --- | --- | --- |
| Operating Income | 50 | 45 | 45 | 40 |
| Interest Expense | 10 | 10 | 10 | 10 |
"""


def test_shipped_2026_10_11():
    """SPOT's DRAFT of 2026-10-11: every fix it led to."""
    values = {"segment_growth.Premium:FY2030": "9.4%", "segment_cagr.Premium": "10.4%",
              "driver.Premium subscribers:FY2030": "390mn",
              "driver_cagr.Premium subscribers": "6.1%"}
    found = [k for k, _, _ in facts._model_candidates(
        "Premium subscriber compounding at 9% CAGR to FY2030", values, 12)]
    check("a subscriber CAGR is not a segment's one-year revenue growth",
          "segment_growth.Premium:FY2030" not in found and "segment_cagr.Premium" not in found
          and "driver_cagr.Premium subscribers" in found, str(found))

    text, _ = facts.strip_typed("### (2) Gross margin expands to 38%+ as marketplace grows\n\n"
                                "### (1) Subscribers compounding at 9% CAGR to {{driver.Subs:FY2030}}\n")
    check("a heading loses its figure with the word that led to it",
          "to as" not in text and "expands as marketplace" in text
          and "compounding to {{driver.Subs:FY2030}}" in text, text)

    misused: list[str] = []
    body, _ = pitch_mod._fill_placeholders(
        "{{segment_base.Ad-Supported}} million ad-supported MAUs, and {{segment_base.Premium}} "
        "from subscribers", {"segment_base.Ad-Supported": "EUR 1.5bn",
                             "segment_base.Premium": "EUR 15.6bn"},
        missing=pitch_mod.MISSING, misused=misused)
    check("an amount followed by a count is refused; one from subscribers is not",
          len(misused) == 1 and "MAUs" in misused[0] and "EUR 15.6bn from" in body, body)

    body, gone = pitch_mod._drop_missing(
        "- **FCF:** Capex at " + pitch_mod.MISSING + " of revenue. Margin rises.\n\n"
        "| a | " + pitch_mod.MISSING + " |")
    check("a sentence citing a figure with no value goes; a table prints n.a.",
          "Capex at" not in body and "Margin rises." in body and "| a | n.a. |" in body
          and gone and gone[0][1] == pitch_mod.MISSING_WHY, body)

    saved_path = Path(__file__).parent / "replays" / "SPOT_20261011T152829.json.gz"
    if not saved_path.exists():
        return  # the saved pitch is not in this copy
    saved = replay.load(saved_path)
    # As the bot ran it: the house WACC on.
    house, config.HOUSE_WACC = config.HOUSE_WACC, True
    try:
        shipped = replay.shipped(saved)[1]
    finally:
        config.HOUSE_WACC = house
    prose = "\n".join(l for l in shipped.body.split("## Model Outputs")[0].split("\n")
                      if not l.startswith("|"))
    check("SPOT 2026-10-11 replayed: no n.a., no amount as MAUs, conviction capped",
          " n.a. " not in prose and "bn ad-supported MAUs" not in prose
          and shipped.headline.get("conviction") == "Low"
          and "**Conviction:** low (" in shipped.body, prose[:300])


def test_shipped_2026_10_07():
    """SPOT's pitch of 2026-10-07: every fix it led to, on the sentences it shipped."""
    # Signposts keep their label; only an empty one goes.
    text = ("**Against.** A risk.\n\n**What would tell.** Net adds by region.\n\n"
            "**What would tell.**\n\nNext.")
    cleaned = pitch_mod._clean_text_artifacts(text)
    check("a signpost keeps its label; only an empty one is removed",
          cleaned.count("What would tell.") == 1 and "**What would tell.** Net adds" in cleaned,
          cleaned)

    # An assumption whose basis uses the WACC as a yardstick is not the WACC.
    marg = facts.build("## [PRIMARY] filing_tables: latest 8-K, filed 2026-07-28\n\n### M\n"
                       "- Tax rate — Three Months 2026: 21.0%\n", "")
    declared, _ = facts.assumptions_from({"assumptions": [
        {"id": "new_capital_return", "value": "18%",
         "basis": "set to 18% (approx 1.6x WACC) reflecting an asset-light model with pricing "
                  "power but royalty-dependent margins"},
        {"id": "discount", "value": "9%", "basis": "a discount rate for a mature franchise"}]})
    kept, cite_as, why = facts.reconcile_assumptions(
        declared, marg, {"wacc": "11.2%", "new_capital_return": "18.0%"})
    check("a basis that compares with the WACC is not refused as the WACC; the id decides",
          cite_as.get("new_capital_return") == "new_capital_return"
          and not any("new_capital_return" in w for w in why), f"{kept} {cite_as} {why}")

    # The revision that answered the reviewer beats a draft with one sentence fewer cut.
    first = pitch_mod.Pitch(headline={}, body="", valuation=object(), removed=[("s", [])] * 49)
    second = pitch_mod.Pitch(headline={}, body="", valuation=object(), removed=[("s", [])] * 50)
    check("a draft with seven more problems and the reviewer's points open loses",
          pitch_mod._rank(second, ["a", "b"]) < pitch_mod._rank(first, list("abcdefghi"), 5))
    check("a revision that adds a code problem loses to the reviewed draft it replaces",
          pitch_mod._rank(first, [], 1) < pitch_mod._rank(first, ["contradiction"]))

    # The bridge and a DCF that is not weighted cite the base case.
    body = ("**EV-to-equity bridge:** {{ev}} + {{cash}} = {{equity}} / {{shares}} = "
            "{{intrinsic}}. The probability-weighted DCF gives {{intrinsic}}.\n"
            "**Valuation evidence: DCF implies {{upside}} upside; probability-weighted value "
            "{{weighted_intrinsic}}.**\n"
            "Spotify (NYSE: SPOT) | Current Price: {{price}} | Intrinsic Price: {{intrinsic}}")
    out = pitch_mod._base_case_cites(body, converted=True)
    check("the bridge and an unweighted DCF cite the base case; weighted text and the cover "
          "line keep the recommendation",
          "= {{base_intrinsic}} (at {{fx_rate}})." in out
          and "weighted DCF gives {{intrinsic}}" in out
          and "DCF implies {{base_upside}} upside" in out
          and out.endswith("Intrinsic Price: {{intrinsic}}"), out)

    # Comparisons between model figures must go the way the numbers do.
    wrong = pitch_mod._comparison_problems(
        "compound at {{terminal_roic}} on new capital, far above the {{new_capital_return}} "
        "the model allows. Margins reach {{a}}, above {{b}}.",
        {"terminal_roic": "22.1%", "new_capital_return": "25.0%", "a": "21.0%", "b": "12.8%"})
    check("a comparison the numbers contradict is caught; one they support is not",
          len(wrong) == 1 and "far above" in wrong[0], str(wrong))

    # A share of revenue printed as a share of something else.
    fx = facts.build("## [PRIMARY] filing_tables: 10-Q\n\n### Income\n"
                     "- Revenue — Three Months 2026: USD 4,777mn\n"
                     "- Revenue — Six Months 2026: USD 9,310mn\n", "")
    ids = sorted(fx.by_id)
    share = "{{share:%s/%s}}" % (ids[0], ids[1])
    found = facts.misused(f"Android sits on {share} of smartphones. It is {share} of revenue. "
                          f"Users come from a {share} MAU base.", fx)
    check("a share placeholder printed as a share of smartphones or an MAU base is caught",
          len(found) == 2 and all("prints it as" in why for _, why in found), str(found))

    # A bold lead that carries a typed figure goes with its sentence.
    stripped, removed = facts.strip_typed(
        "**FCF margin sustains ~22%: capex rises.** FY2025 FCF was 4% of revenue. Then more.")
    check("a bold lead with a typed figure is removed with its sentence",
          "22%" not in stripped and "Then more." in stripped, stripped)


def test_spot_model_and_tables():
    """SPOT 2026-10-07: base-year segments and drivers, ROIC on capital near zero, a
    foreign issuer's 6-K tables in euros, and its 6-Ks found among the others."""
    model = synthetic_model(
        revenue=None, segments={"A": [880, 968, 1065, 1171, 1288],
                                "B": [220, 242, 266, 293, 322]},
        segment_base={"A": 800, "B": 200},
        drivers={"Subs": {"unit": "mn", "base": 100, "values": [110, 121, 133, 146, 161]}})
    model.pop("revenue")
    v = valuation.compute(model, synthetic_data())
    values = valuation.placeholders(v)
    check("a segment's base year is citable and its CAGR runs from it",
          values.get("segment.A:FY2026") == "USD 800mn"
          and values.get("segment_cagr.A") == "10.0%", str({k: values.get(k) for k in (
              "segment.A:FY2026", "segment_cagr.A")}))
    check("a driver's base year and its own CAGR are citable",
          values.get("driver.Subs:FY2026") == "100 mn" or values.get("driver.Subs:FY2026",
                                                                       "").startswith("100")
          and values.get("driver_cagr.Subs") == "10.0%",
          str({k: values.get(k) for k in ("driver.Subs:FY2026", "driver_cagr.Subs")}))
    table = valuation.financials_markdown(v)
    check("the income statement shows segments in the base year",
          "| A | USD bn | 0.8 |" in table, table[:400])
    off = dict(model, segment_base={"A": 500, "B": 200})
    check("a segment base that does not add up to base revenue is flagged",
          any("segment_base" in w for w in valuation.compute(off, synthetic_data()).warnings))

    near_zero = RETURNS_BLOCK.replace("| Stockholders Equity | 800 | 600 |",
                                      "| Stockholders Equity | -250 | -340 |")
    data = synthetic_data(prompt_block=near_zero, cash=100.0, debt=700.0, ttm_ebitda=300.0,
                          ttm_net_income=100.0)
    v = valuation.compute(synthetic_model(capex_pct_revenue=0.07), data)
    year = v.returns.year("FY2026") if v.returns else None
    check("ROIC on capital near zero is not meaningful, and says why",
          year is not None and year.roic is None and year.capital_near_zero
          and "capital near zero" in valuation.returns_table(v),
          str(year))

    html = ("<p>Interim condensed consolidated statement of operations (in € millions, except "
            "share and per share data)</p><table><tr><td></td><td>Three months ended June 30, "
            "2026</td><td>Three months ended June 30, 2025</td></tr>"
            "<tr><td>Revenue</td><td>4,777</td><td>4,193</td></tr>"
            "<tr><td>Operating income</td><td>655</td><td>406</td></tr>"
            "<tr><td>Cost of revenue</td><td>3,181</td><td>2,873</td></tr></table>")
    from data_pipeline import filing_tables as dp_tables, release as dp_release
    from data_pipeline.facts_db import FactDB
    read = dp_tables.tables(html)
    check("a table in € millions reads as EUR in millions",
          read and read[0].cells[0].text == "EUR 4,777mn", str(read[0].cells[:2] if read else read))
    db = FactDB("SPOT", "Spotify", "EUR", "USD", 12)
    dp_release.from_release(db, read, "financial report", "2026-08-04")
    revenue = next((f for f in db.facts.values() if f.metric == "revenue"), None)
    check("'except per share data' does not make a statement a per-share table",
          revenue is not None and revenue.unit == "EUR" and revenue.value == 4777e6,
          str(revenue))

    # The 6-Ks: a cover furnishing the results deck, and the quarter's report itself.
    pages = {
        "a1-index.htm": "<table><tr><td>1</td><td>6-K</td><td>c1.htm</td><td>6-K</td><td>9000</td></tr>"
                        "<tr><td>2</td><td>EX</td><td>ex99.htm</td><td>EX-99.1</td><td>5000</td></tr></table>",
        "c1.htm": "<p>announcing an increase in its share repurchase program</p>",
        "a2-index.htm": "<table><tr><td>1</td><td>6-K</td><td>rep.htm</td><td>6-K</td><td>1285783</td></tr></table>",
        "a3-index.htm": "<table><tr><td>1</td><td>6-K</td><td>c3.htm</td><td>6-K</td><td>8000</td></tr>"
                        "<tr><td>2</td><td>EX</td><td>deck.htm</td><td>EX-99.1</td><td>90000</td></tr></table>",
        "c3.htm": "<p>released its results for the quarter ended June 30, 2026</p>",
    }
    saved = research_tools._sec_get
    research_tools._sec_get = lambda url: NS(text=pages.get(url.rsplit("/", 1)[-1], ""))
    try:
        found = research_tools.foreign_quarterlies(1, [
            ("4", "2026-09-01", "x", "f4.xml", "2026-09-01"),
            ("6-K", "2026-08-20", "a1", "c1.htm", "2026-08-20"),
            ("6-K", "2026-08-04", "a2", "rep.htm", "2026-06-30"),
            ("6-K", "2026-08-04", "a3", "c3.htm", "2026-08-04")])
    finally:
        research_tools._sec_get = saved
    check("a foreign issuer's results deck and quarterly report are found among its 6-Ks",
          found.get("release", ("",))[-1].endswith("/deck.htm")
          and found.get("interim", ("",))[-1].endswith("/rep.htm"), str(found))


def test_returns_and_leverage():
    """2026-10-04: ROIC against the WACC and what borrowing does to EPS, asked for after
    the NOK pitch had neither."""
    saved = config.GLIDE_YEARS
    config.GLIDE_YEARS = 0
    try:
        data = synthetic_data(prompt_block=RETURNS_BLOCK, cash=100.0, debt=700.0,
                              ttm_ebitda=300.0, ttm_net_income=100.0)
        v = valuation.compute(synthetic_model(capex_pct_revenue=0.07), data)
        values = valuation.placeholders(v)
        # FY2026: NOPAT 180 x 0.75 = 135 on capital of 1,000 both years; 600 without goodwill.
        check("reported ROIC is NOPAT over average equity + debt - cash",
              values.get("roic:FY2026") == "13.5%"
              and values.get("roic_ex_goodwill:FY2026") == "22.5%", str(values.get("roic:FY2026")))
        # FY2027 adds capex 77 less D&A 55: capital 1,022; NOPAT 165 over 1,011.
        check("forecast capital grows by what the model reinvests",
              values.get("invested_capital:FY2027") == "USD 1.0bn"
              and values.get("roic:FY2027") == "16.3%", str(values.get("roic:FY2027")))
        # 2% growth on 2% of revenue reinvested out of a 15% NOPAT margin: 15% on new capital.
        check("the terminal value's return on new capital is growth over reinvestment",
              values.get("terminal_roic") == "15.0%", str(values.get("terminal_roic")))
        kd = (config.RISK_FREE_DEFAULT + config.DEBT_SPREAD) * (1 - config.WACC_TAX_RATE)
        eps = ((100 - 300 * kd) / (1e7 - 300e6 / 250)) / (100 / 1e7) - 1
        check("leverage: net debt over EBITDA, interest cover and the buyback's EPS effect",
              values.get("net_debt_to_ebitda") == "2.0x"
              and values.get("interest_coverage") == "4.5x"
              and values.get("earnings_yield") == "4.0%"
              and values.get("buyback_eps_change") == f"{eps * 100:+.1f}%",
              f"{values.get('buyback_eps_change')} vs {eps * 100:+.1f}%")
        md = valuation.model_outputs_markdown(v)
        check("the Returns on Capital and Leverage tables are appended",
              "#### Returns on Capital" in md and "| FY2026A | 1,000 | 135 | 13.5% | 22.5% |" in md
              and "#### Leverage" in md, md[-900:])
        problems = pitch_mod._returns_problems(v, "No such paragraphs.")
        check("a pitch without the Returns on capital and Leverage paragraphs is sent back",
              len(problems) == 2 and "Returns on capital" in problems[0]
              and "2.0x" in problems[1], str(problems))
        check("the two paragraphs satisfy the check",
              pitch_mod._returns_problems(
                  v, "**Returns on capital.** Fine.\n\n**Leverage.** Fine.") == [])
        check("a ROIC without its year is explained, with the years there are",
              "FY2026" in (pitch_mod._missing_reason("roic", v, False) or ""),
              str(pitch_mod._missing_reason("roic", v, False)))

        low = valuation.compute(synthetic_model(), synthetic_data(prompt_block=RETURNS_BLOCK))
        check("net cash needs no Leverage paragraph",
              not low.leverage.needs_paragraph
              and len(pitch_mod._returns_problems(low, "")) == 1)
        config.REINVESTMENT_CHECK = True
        try:
            # 2% growth forever at the best reported ROIC (22.5% without goodwill) needs
            # 8.9% of NOPAT reinvested: capex 5% + 8.9% x 15% NOPAT margin = 6.3% of revenue.
            sent = [w for w in valuation.compute(synthetic_model(), synthetic_data(
                prompt_block=RETURNS_BLOCK)).warnings if "NOPAT reinvested" in w]
            check("growth forever with nothing reinvested is sent back, with the capex it needs",
                  len(sent) == 1 and "about 6.3%" in sent[0] and "best reported ROIC" in sent[0],
                  str(sent))
            funded = valuation.compute(synthetic_model(capex_pct_revenue=0.07),
                                       synthetic_data(prompt_block=RETURNS_BLOCK))
            check("growth funded at the return on new capital passes",
                  not any("NOPAT reinvested" in w for w in funded.warnings), str(funded.warnings))
            claimed = valuation.compute(
                synthetic_model(terminal={"method": "perpetuity", "growth": 0.02,
                                          "new_capital_return": 0.5}),
                synthetic_data(prompt_block=RETURNS_BLOCK)).warnings
            check("a claimed return on new capital is held to 3x WACC and needs its evidence",
                  any("held to that range" in w for w in claimed)
                  and any("new_capital_rationale" in w and "needs" in w for w in claimed),
                  str(claimed))
            rounded_v = valuation.compute(synthetic_model(),
                                          synthetic_data(prompt_block=RETURNS_BLOCK))
            best_roic = max(max(y.roic, y.roic_ex_goodwill or y.roic)
                            for y in rounded_v.returns.years if y.reported and y.roic is not None)
            _, _, rounding_warnings = valuation.new_capital_return(
                rounded_v, {"new_capital_return": round(best_roic, 3),
                            "new_capital_rationale": "The installed base supports durable pricing "
                                                     "power and low incremental capital needs."})
            check("rounded best reported ROIC does not trigger a boundary warning",
                  not any("outside" in w for w in rounding_warnings), str(rounding_warnings))
            # Goodwill of 1,200 against capital of 1,000: SPGI's shape. 45% is inside 5x WACC.
            light = valuation.compute(
                synthetic_model(terminal={"method": "perpetuity", "growth": 0.02,
                                          "new_capital_return": 0.45,
                                          "new_capital_rationale": "growth is funded through "
                                          "data and R&D already expensed in operating costs"}),
                synthetic_data(prompt_block=RETURNS_BLOCK.replace(
                    "| Goodwill And Other Intangible Assets | 400 | 400 |",
                    "| Goodwill And Other Intangible Assets | 1,200 | 1,200 |")))
            check("a business with no tangible capital may argue up to 5x WACC, cited by name",
                  not any("held to that range" in w for w in light.warnings)
                  and valuation.placeholders(light).get("new_capital_return") == "45.0%"
                  and "n.m. (no tangible capital)" in valuation.returns_table(light),
                  str(light.warnings))
            exit_model = synthetic_model(terminal={"method": "exit_multiple",
                                                   "exit_ev_ebitda": 15})
            sent = [w for w in valuation.compute(exit_model, synthetic_data(
                prompt_block=RETURNS_BLOCK)).warnings if "NOPAT reinvested" in w]
            capex = re.search(r"capex_pct_revenue` to about ([\d.]+)%", sent[0] if sent else "")
            multiple = re.search(r"toward about ([\d.]+)x", sent[0] if sent else "")
            check("an exit multiple pricing unfunded growth is sent back with a consistent pair",
                  bool(capex and multiple), str(sent))
            if capex and multiple:
                fixed = synthetic_model(capex_pct_revenue=float(capex.group(1)) / 100,
                                        terminal={"method": "exit_multiple",
                                                  "exit_ev_ebitda": float(multiple.group(1))})
                again = valuation.compute(fixed, synthetic_data(prompt_block=RETURNS_BLOCK))
                check("taking the suggested capex and multiple clears the check",
                      not any("NOPAT reinvested" in w for w in again.warnings),
                      str(again.warnings))
        finally:
            config.REINVESTMENT_CHECK = False
        try:
            valuation.compute(synthetic_model(), synthetic_data(
                prompt_block=RETURNS_BLOCK.replace("Software - Application", "Banks - Regional")))
            check("an FCFF model for a bank is sent back for bank drivers", False)
        except valuation.ModelInputError as exc:
            check("an FCFF model for a bank is sent back for bank drivers",
                  "model.bank" in str(exc))
        plain = valuation.compute(synthetic_model(), synthetic_data())
        shared = valuation.compute(synthetic_model(), synthetic_data(minority_interest=100.0))
        check("minority interests come off the equity value with the debt",
              abs(plain.per_share - shared.per_share - 10.0) < 1e-6
              and "(-) Minority Interests" in valuation.intrinsic_valuation_markdown(shared)
              and "minority_interest" in valuation.placeholders(shared)
              and "(-) Minority Interests" not in valuation.intrinsic_valuation_markdown(plain),
              f"{plain.per_share:.2f} vs {shared.per_share:.2f}")
        old = valuation.compute(synthetic_model(), synthetic_data(prompt_block=STATEMENTS))
        check("no balance sheet, no ROIC and no paragraph asked for",
              old.returns is None and pitch_mod._returns_problems(old, "") == [])
    finally:
        config.GLIDE_YEARS = saved


def test_glide_and_implied_growth():
    """2026-10-02: a glide instead of a cliff after the forecast, what the price assumes,
    drivers that must build revenue, and cash conversion against the statements."""
    plain = valuation.compute(synthetic_model(), synthetic_data())
    config.GLIDE_YEARS = 5
    try:
        glided = valuation.compute(synthetic_model(), synthetic_data())
        check("a grower is worth more with a glide than with a cliff to 2%",
              glided.per_share > plain.per_share and len(glided.glide_rows) == 5,
              f"{glided.per_share:.2f} vs {plain.per_share:.2f}")
        g = glided.glide_rows
        check("the glide steps revenue growth down to the terminal rate",
              abs(g[0].revenue / glided.rows[-1].revenue - 1 - (0.10 - 0.016)) < 1e-6
              and abs(g[-1].revenue / g[-2].revenue - 1 - 0.02) < 1e-9
              and g[0].label == "FY2032" and g[-1].label == "FY2036",
              f"{[round(r.revenue) for r in g]}")
        flat = synthetic_model(revenue=[1020, 1040.4, 1061.208, 1082.43216, 1104.0808])
        a = valuation.compute(flat, synthetic_data())
        config.GLIDE_YEARS = 0
        b = valuation.compute(flat, synthetic_data())
        config.GLIDE_YEARS = 5
        check("a business already at the terminal rate is valued the same either way",
              abs(a.per_share - b.per_share) < 0.01, f"{a.per_share:.2f} vs {b.per_share:.2f}")
        check("the sensitivity grid uses the glide too",
              abs(valuation.value_at(glided, glided.wacc, growth=glided.growth)
                  - glided.per_share) < 0.01)
        exit_model = synthetic_model(terminal={"method": "exit_multiple", "exit_ev_ebit": 10})
        check("an exit multiple has no glide",
              valuation.compute(exit_model, synthetic_data()).glide_rows == [])
        check("the DCF table shows the glide",
              "glide" in valuation.dcf_table(glided).lower(), valuation.dcf_table(glided))
    finally:
        config.GLIDE_YEARS = 0

    v = valuation.compute(synthetic_model(), synthetic_data(analyst_target=400.0))
    check("the model's own revenue gives the model's own value",
          abs(valuation._revalue(v, [r.revenue for r in v.rows]) - v.per_share) < 0.01)
    cagr = v.implied_cagr
    n = len(v.rows)
    at = valuation._revalue(v, [v.base_revenue * (1 + cagr) ** (i + 1) for i in range(n)])
    check("the implied revenue CAGR gives today's price", cagr is not None and abs(at - 250.0) < 0.05,
          f"{cagr} -> {at}")
    check("a lower price implies slower growth than the model's 10%",
          cagr < v.model_cagr and abs(v.model_cagr - 0.10) < 1e-6, f"{cagr:.3f}")
    check("the Street's target implies faster growth than today's price",
          v.street_cagr is not None and v.street_cagr > cagr, str(v.street_cagr))
    values = valuation.placeholders(v)
    check("what the price assumes is a placeholder",
          values.get("implied_cagr") == f"{cagr:.1%}" and values.get("model_cagr") == "10.0%",
          str({k: values.get(k) for k in ("implied_cagr", "model_cagr", "street_cagr")}))

    nflx = synthetic_model(base_revenue=45183, revenue=[51200, 56320, 60826, 64475, 67054],
                           drivers={"Paid subscribers (mn)": {"unit": "mn", "base": 325,
                                                              "values": [345, 365, 382, 395, 405]},
                                    "ARPU (USD)": {"unit": "USD", "base": 13.9,
                                                   "values": [14.5, 15.1, 15.7, 16.2, 16.6]}})
    v = valuation.compute(nflx, synthetic_data(revenue=45183.0))
    warned = [w for w in v.warnings if "should build revenue" in w]
    check("subscribers x ARPU that do not make revenue are sent back",
          len(warned) == 1 and "x 12 months" in warned[0] and "54,210" in warned[0], str(warned))
    fixed = json.loads(json.dumps(nflx))
    fixed["drivers"]["ARPU (USD)"] = {"unit": "USD", "base": 11.585,
                                      "values": [round(r / s / 12, 3) for r, s in zip(
                                          nflx["revenue"], [345, 365, 382, 395, 405])]}
    v = valuation.compute(fixed, synthetic_data(revenue=45183.0))
    check("drivers that multiply out to revenue pass",
          not any("should build revenue" in w for w in v.warnings), str(v.warnings))

    netflix = synthetic_model(base_revenue=45183, revenue=[51200, 56320, 60826, 64475, 67054],
                              ebit_margin=0.315, tax_rate=0.21, da_pct_revenue=0.05,
                              capex_pct_revenue=0.045)
    v = valuation.compute(netflix, synthetic_data(revenue=45183.0, prompt_block=STATEMENTS))
    basis = [w for w in v.warnings if "add back D&A" in w]
    jump = [w for w in v.warnings if "jump in cash conversion" in w]
    check("D&A far below the statements' is sent back (NFLX's content amortisation)",
          len(basis) == 1 and "37%" in basis[0], str(v.warnings))
    check("a first-year FCF margin far above the reported one is sent back",
          len(jump) == 1 and "20.9%" in jump[0], str(v.warnings))
    content = synthetic_model(base_revenue=45183, revenue=[51200, 56320, 60826, 64475, 67054],
                              ebit_margin=0.315, tax_rate=0.21, da_pct_revenue=0.37,
                              capex_pct_revenue=0.40)
    v = valuation.compute(content, synthetic_data(revenue=45183.0, prompt_block=STATEMENTS))
    check("content amortisation in D&A and its cash in capex passes",
          not any("add back D&A" in w or "cash conversion" in w for w in v.warnings),
          str(v.warnings))
    check("the reported years are read from the market data block",
          market_data.reported_years(STATEMENTS)[0] == {
              "label": "FY2025", "revenue": 45183.0, "ebit": 13327.0, "ebitda": 30255.0,
              "fcf": 9461.0, "capex": -688.0}, str(market_data.reported_years(STATEMENTS)[0]))


def test_saved_pitches():
    """Every saved real pitch flags exactly the figures reviewed as wrong, no more, no less.

    Run `python replay.py golden` to see a difference, and `--update` only after reading
    each line it adds or drops against its pitch.
    """
    diff = replay.golden_diff()
    check("the checks on every saved pitch match the reviewed list", not diff,
          "; ".join(f"{name}: -{len(lost)} +{len(new)}" for name, (lost, new) in diff.items()))


def test_growth_rates_and_repeats():
    """2026-10-02: stated growth rates held to the model's; a figure printed twice."""
    model = synthetic_model(drivers={"Paid memberships": {"unit": "mn", "base": 325,
                                                         "values": [340, 355, 370, 385, 400]}},
                            segments={"Ads": [100, 110, 121, 133.1, 146.41],
                                      "Subscription": [1000, 1100, 1210, 1330.9, 1464.1]})
    v = valuation.compute(model, synthetic_data())
    wrong = wrong_figures("Paid memberships: 325m (FY2026) to 400m (FY2031), a 3.5% CAGR.", v,
                          model=model)
    check("a stated CAGR the model does not have is caught", wrong == [("3.5%", "growth")],
          str(wrong))
    check("the model's own CAGR passes, rounded",
          wrong_figures("Paid memberships: 325m to 400m, a 4.2% CAGR.", v, model=model) == [])
    check("'ad revenue' is the Ads segment's",
          wrong_figures("Ad revenue reaches USD 146mn by FY2031 (25% CAGR).", v, model=model)
          == [("25%", "growth")])
    check("a lower bound the model meets is not a contradiction",
          wrong_figures("Subscription revenue of >USD 1.4bn by FY2031.", v, model=model) == [])
    check("a segment's margin is not read as its growth",
          wrong_figures("By FY2031: Ads 35% (high incremental margin).", v, model=model) == [])
    # SPGI's of 2026-10-02: a price rise is part of a segment's growth, not all of it, and a
    # CAGR from the last reported year can be checked against the amounts beside it.
    check("a price increase is not read as the segment's growth",
          wrong_figures("Ads fee increases of 1.5-2.0% annually support pricing.", v,
                        model=model) == []
          and wrong_figures("Ads revenue grows on 1-2% annual price increases.", v,
                            model=model) == [])
    check("a CAGR between amounts written beside it passes",
          wrong_figures("Ads revenue grows from USD 80mn in FY2026 to USD 146mn in FY2031, "
                        "a 12.8% CAGR.", v, model=model) == [])
    check("a CAGR the amounts beside it do not give is still caught",
          wrong_figures("Ads revenue grows from USD 80mn in FY2026 to USD 146mn in FY2031, "
                        "a 15% CAGR.", v, model=model) == [("15%", "growth")])
    pool = provenance.build_pool([], [], v, synthetic_data(), model)
    found = provenance.outcomes("Ads revenue reaches USD 146mn by FY2031. Our moat is 99% "
                                "retention.", pool)
    check("the ledger lists every figure with its outcome, passed ones included",
          [(f.text, o) for f, _, o in found] == [("USD 146mn", "sourced"), ("99%", "untraced")],
          str([(f.text, o) for f, _, o in found]))
    text = "The price implies a 14.8% CAGR (14.8%), and margins reach 34.5% (FY2030) (34.5%)."
    check("a figure printed twice a few words apart keeps one copy",
          pitch_mod._drop_repeats(text) == "The price implies a 14.8% CAGR, and margins reach "
          "34.5% (FY2030).", pitch_mod._drop_repeats(text))
    check("a different figure in brackets stays",
          pitch_mod._drop_repeats("FCF reaches 25% (27.8%).") == "FCF reaches 25% (27.8%).")


def test_filing_tables():
    """An SEC table read cell by cell: merged headings, "$" cells, years as headings,
    bracketed negatives, per-share rows and the table's scale."""
    html = """<p>Condensed Consolidated Statements of Income</p>
    <p>Three and six months ended June 30, 2026 and 2025 (dollars in millions, except per
    share data)</p><table>
    <tr><td colspan="3"></td><td colspan="6">Three Months</td><td colspan="6">Six Months</td></tr>
    <tr><td colspan="3"></td><td colspan="3">2026</td><td colspan="3">2025</td>
        <td colspan="3">2026</td><td colspan="3">2025</td></tr>
    <tr><td colspan="3">Revenue</td><td>$</td><td>4,146</td><td></td><td>$</td><td>3,755</td>
        <td></td><td>$</td><td>8,318</td><td></td><td>$</td><td>7,532</td><td></td></tr>
    <tr><td colspan="3">Other income, net</td><td colspan="2">(4)</td><td></td>
        <td colspan="2">(28)</td><td></td><td colspan="2">(6)</td><td></td>
        <td colspan="2">(23)</td><td></td></tr>
    <tr><td colspan="3">Earnings per share:</td></tr>
    <tr><td colspan="3">Diluted</td><td>$</td><td>4.12</td><td></td><td>$</td><td>3.50</td>
        <td></td><td>$</td><td>8.81</td><td></td><td>$</td><td>7.04</td><td></td></tr>
    </table>"""
    found = filing_tables.tables(html)
    text = found[0].render() if found else ""
    check("a filing table reads each figure with its row, column and scale",
          "- Revenue — Three Months 2026: USD 4,146mn; Three Months 2025: USD 3,755mn; "
          "Six Months 2026: USD 8,318mn" in text, text)
    check("bracketed negatives and per-share rows read right",
          "Other income, net — Three Months 2026: USD -4mn" in text
          and "Earnings per share: Diluted — Three Months 2026: USD 4.12;" in text, text)
    # SPGI's 2Q26 release stated "in millions" on most tables, not on its pro forma one.
    scaled = ("<p>Revenue (in millions)</p><table><tr><td></td><td>2026</td></tr>"
              + "".join(f"<tr><td>Row {i}</td><td>$</td><td>{i},100</td></tr>" for i in range(1, 9))
              + "</table>")
    bare = ("<p>Pro Forma Organic Revenue</p><table><tr><td></td><td>2026</td></tr>"
            + "".join(f"<tr><td>Line {i}</td><td>$</td><td>7,39{i}</td></tr>" for i in range(1, 9))
            + "</table>")
    doc = filing_tables.tables(scaled + scaled + bare)
    unscaled = doc[-1].render() if doc else ""
    check("a table that states no scale takes the one the document's tables state",
          "Line 1 — value: USD 7,391mn" in unscaled, unscaled)
    check("the table is titled by the words before it",
          "Statements of Income Three and six months ended June 30, 2026" in text[:120],
          text[:120])


def test_sec_xbrl():
    """The company's tagged figures: the latest periods of each length, from the tag it
    still reports, labelled like any filing table."""
    def entry(start, end, val, form="10-K", filed="2026-02-11"):
        e = {"end": end, "val": val, "form": form, "filed": filed}
        if start:
            e["start"] = start
        return e
    company = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [entry("2017-01-01", "2017-12-31", 6.0e9, filed="2018-02-09")]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            entry("2025-01-01", "2025-12-31", 15.336e9), entry("2024-01-01", "2024-12-31", 14.208e9),
            entry("2026-01-01", "2026-03-31", 4.171e9, "10-Q", "2026-04-28")]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
            entry("2009-01-01", "2009-12-31", 69e6, filed="2010-02-24")]}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": [entry("2025-01-01", "2025-12-31", 14.66)]}},
    }}}
    text = sec_xbrl.render(company)
    check("XBRL: the tag still reported wins, annual and quarterly apart",
          "- Revenue — 2025-12-31: USD 15,336mn (10-K filed 2026-02-11); 2024-12-31: USD 14,208mn"
          in text and "2026-03-31: USD 4,171mn (10-Q filed 2026-04-28)" in text
          and "6,000mn" not in text, text)
    check("XBRL: a metric the company stopped reporting is left out, EPS kept per share",
          "Capital expenditure" not in text and "Diluted EPS — 2025-12-31: USD 14.66" in text, text)
    fx = facts.build("## [PRIMARY] sec_xbrl: company facts\n\n" + text, "")
    first = fx.by_id["F1"]
    check("XBRL figures are labelled and credited to the SEC",
          (first.metric, first.period_key, first.source)
          == ("Revenue", ("12M", "2025-12"), "SEC XBRL company facts"), str(first))


def test_facts_mode(data):
    """Facts mode: the writer cites figures by id, never types them."""
    digest = ("## [PRIMARY] fetch_page: https://www.sec.gov/Archives/x/10q.htm\n\n"
              "[latest 10-Q, filed 2026-07-28]\nRevenue rose 10% on the year.\n\n"
              "## [PRIMARY] filing_tables: latest 10-Q, filed 2026-07-28\n\n"
              "### Revenue by segment Three months ended June 30, 2026 (dollars in millions)\n"
              "- Total revenue — Three Months 2026: USD 4,146mn\n"
              "- Ratings — Three Months 2026: USD 1,212mn\n\n"
              "## [SNIPPETS] web_search: market share\n\n1. A blog\n"
              "   https://www.blog.example.com/a\n   Fitch holds about 15% of the market.")
    fx = facts.build(digest, "| Net debt | 11,343 |")
    ids = {f.text: f.id for f in fx.by_id.values()}
    check("only the tables and the market data are facts; web and running text are not",
          set(ids) == {"USD 4,146mn", "USD 1,212mn", "11,343"}, str(ids))
    check("a table's figure is credited to its filing",
          fx.by_id[ids["USD 1,212mn"]].source == "10-Q, filed 2026-07-28, tables"
          and fx.by_id[ids["11,343"]].source == "market data",
          str({f.text: f.source for f in fx.by_id.values()}))
    check("the writer reads the ids in the fact database, and the notes as context",
          f"USD 1,212mn [#{ids['USD 1,212mn']}]" in fx.database
          and "Fitch holds about 15% of the market." in fx.digest
          and "[#" not in fx.digest and "filing_tables" not in fx.digest, fx.digest)

    rev, rat = ids["USD 4,146mn"], ids["USD 1,212mn"]
    headline = json.loads(json.dumps(HEADER_OBJ))
    headline["assumptions"] = [{"id": "leverage_target", "value": "2.0x",
                                "basis": "the low end of management's range"}]
    headline["one_liner"] = "A ratings franchise at {{share:" + rat + "/" + rev + "}} of revenue."
    headline["thesis_pillars"] = ["Fitch is far behind at ~15%", "B"]
    body = BODY.replace(
        "Spotify is the #1 music streaming service.",
        "Ratings revenue was {{" + rat + "}} of {{" + rev + "}}, a {{share:" + rat + "/" + rev
        + "}} share. Moody's and S&P hold ~80% of the market. I target leverage of "
        "{{assume:leverage_target}}. Peers trade at {{F999}}.")
    result, problems = pitch_mod._finalise(headline, body, data, digest=digest, fx=fx)
    text = result.body
    check("a cited figure prints as its source wrote it, with the source once",
          "Ratings revenue was USD 1,212mn of USD 4,146mn (10-Q, filed 2026-07-28, tables), a "
          "29.2% (computed from 10-Q, filed 2026-07-28, tables) share." in text, text[:600])
    check("a typed figure stays visible with a dagger and goes back to the writer",
          "~80%†" in text and any("80%" in p for p in problems), text[:600])
    check("typed figures go back to the writer",
          any(p.startswith("these figures are typed") for p in problems), str(problems))
    check("an id that is not in the notes is a problem",
          any("{{F999}}" in p for p in problems), str(problems))
    check("an assumption prints as one, and is listed with its basis",
          "2.0x (my assumption)" in text and "### Assumptions" in text
          and "the low end of management's range" in text)
    check("the cover cites too: a computed share stays, a typed pillar keeps a dagger",
          "29.2%" in result.get("one_liner")
          and result.get("thesis_pillars") == ["Fitch is far behind at ~15%†", "B"],
          f"{result.get('one_liner')} | {result.get('thesis_pillars')}")
    check("every cited fact is recorded, for the reviewer and the ledger",
          set(result.cited) == {rat, rev}, str(list(result.cited)))
    with tempfile.TemporaryDirectory() as tmp:
        pdf = pdf_render.build(result, data, Path(tmp))
        check("a facts-mode pitch renders", pdf.stat().st_size > 1000)
    # Labels from tables: a filing table row and the market data's markdown tables.
    notes = ("## [PRIMARY] filing_tables: latest earnings release (8-K exhibit 99), filed "
             "2026-07-28\n\n### Operating Results by Segment Three and six months ended June "
             "30, 2026 and 2025 (dollars in millions)\n- Ratings — Three Months Revenue 2026: "
             "USD 1,339mn; Three Months Revenue 2025: USD 1,148mn; Three Months Revenue % "
             "Change: 17%\n\n### 2026 Guidance\n- Revenue growth — Adjusted: 5.9% to 7.9%\n\n"
             "See Exhibit 99.1 at https://www.sec.gov/x/2026/ex99.htm for details.")
    market = ("## Annual financials\n\n### Income statement (values in millions)\n\n"
              "| Line item | 2025-12-31 | 2024-12-31 |\n| --- | --- | --- |\n"
              "| Total Revenue | 15,336 | 14,208 |\n")
    fx = facts.build(notes, market)
    by_text = {f.text: f for f in fx.by_id.values()}
    q = by_text["USD 1,339mn"]
    check("a filing table's figure is labelled by its row and column",
          (q.metric, q.period_key, q.data_type) == ("Ratings", ("3M", "2026-06"), "reported")
          and q.source == "earnings release (8-K exhibit 99), filed 2026-07-28, tables",
          f"{q.metric} {q.period_key} {q.data_type} {q.source}")
    y = by_text["15,336"]
    check("a market data figure is labelled by its row and column",
          (y.metric, y.period_key) == ("Total Revenue", ("12M", "2025-12")), str(y))
    check("guidance is labelled as guidance", by_text["5.9%"].data_type == "guidance")
    check("exhibit numbers and URLs are not figures",
          "99.1" not in by_text and not any("2026/" in f.context[:5] for f in fx.by_id.values()))
    found = facts.misused(f"Ratings revenue was {{{{{q.id}}}}} in 4Q25. Ratings grew to "
                          f"{{{{{q.id}}}}} in 2Q26. Revenue grew {{{{{by_text['5.9%'].id}}}}}. "
                          f"Management guides to {{{{{by_text['5.9%'].id}}}}} growth.", fx)
    check("a quarter's figure given for another quarter is caught, and guidance as a result",
          [s for s, _ in found] == [f"Ratings revenue was {{{{{q.id}}}}} in 4Q25.",
                                    f"Revenue grew {{{{{by_text['5.9%'].id}}}}}."],
          str(found))
    segs = facts.build("## [PRIMARY] filing_tables: latest 10-Q, filed 2026-07-28\n\n### Revenue "
                       "by segment Three months ended June 30, 2026 and 2025\n- Ratings — Three "
                       "Months 2026: USD 1,339mn\n- Indices — Three Months 2026: USD 534mn\n", "")
    r_id, i_id = ([f.id for f in segs.by_id.values() if f.metric == name][0]
                  for name in ("Ratings", "Indices"))
    found = facts.misused(f"Ratings revenue was {{{{{r_id}}}}} while Indices reached {{{{{i_id}}}}}. "
                          f"Indices revenue was {{{{{r_id}}}}}.", segs, ["Ratings", "Indices"])
    check("one segment's figure in the words about another is caught",
          [s[:30] for s, _ in found] == ["Indices revenue was {{F1}}."], str(found))
    tagged = facts.adopt_tags("Ratings grew to $1,339mn [#F1], and Indices to $999mn [#F2].", segs)
    check("a copied tag becomes a citation when the figure is the fact's, and only then",
          tagged == "Ratings grew to {{F1}}, and Indices to $999mn.", tagged)
    hint = facts.describe_typed([("Ratings were $1,339mn.", ["$1,339mn"])], fx=segs,
                                values={"segment_cagr.Ratings": "4.8%"})[0]
    check("a typed figure the notes have is sent back with its id", "{{F1}}" in hint, hint)
    hint = facts.describe_typed([("Ratings grow 4.8% a year.", ["4.8%"])], fx=segs,
                                values={"segment_cagr.Ratings": "4.8%"})[0]
    check("a typed figure the model has is sent back with its placeholder",
          "{{segment_cagr.Ratings}}" in hint, hint)
    # 2026-10-03: a typed figure is accepted only when it is one labelled fact or model
    # value the sentence names, for the period it names.
    check("the fact database opens with its rule and lists the table figures by id",
          segs.database.startswith("# Fact database")
          and "- Ratings — Three Months 2026: USD 1,339mn [#F1]" in segs.database, segs.database)
    adopted = facts.adopt_typed("In 2Q26 Ratings revenue was $1,339mn. Ratings was $1,339mn. "
                                "Energy grows 4-5%. The DCF uses a 3% perpetuity growth rate.",
                                segs, {"g": "3.0%", "segment.Energy:FY2030": "USD 5.0bn"})
    check("a typed figure is cited when the sentence pins its metric and period",
          adopted.startswith("In 2Q26 Ratings revenue was {{F1}}. Ratings was {{F1}}. "),
          adopted)
    check("a range, or a short figure with no period, stays typed; a specific model phrase "
          "is cited", "Energy grows 4-5%." in adopted and "a {{g}} perpetuity" in adopted,
          adopted)
    own, marked = facts.mark_own("We assume Energy grows 4.5% a year. Fitch holds 15%. "
                                 "I model 3% retention gains (company disclosure, 2025 10-K).")
    check("the writer's own estimate is kept and marked; a sourced one is not",
          "We assume Energy grows 4.5% a year (my assumption)." in own and len(marked) == 1
          and "(company disclosure, 2025 10-K)." in own, own)
    laundered = facts.credited_assumptions("Retention is {{assume:nrr}} (company disclosure, "
                                           "2025 10-K). Energy grows {{assume:energy}}.")
    check("an assumption credited to a source is caught",
          [s[:12] for s, _ in laundered] == ["Retention is"], str(laundered))
    margin, _ = facts.fill("EBITDA margins near {{ratio:F2/F1}}.", segs, {})
    check("a ratio named as a margin prints as a percentage", "39.9%" in margin, margin)
    rng = facts.assumptions_from({"assumptions": [{"id": "mi", "value": "2-3%", "basis": "b"}]})
    check("an assumption may be a range", rng[1] == [] and rng[0]["mi"].text == "2-3%", str(rng))
    check("fiscal quarters follow the company's year end",
          facts.mentioned_periods("1Q26, H1 2026 and FY2025", 5)
          == [("3M", "2025-08"), ("6M", "2025-11"), ("12M", "2025-05")])
    check("an assumption with two figures that are not a range is refused",
          facts.assumptions_from({"assumptions": [{"id": "x", "value": "2x and 5%",
                                                   "basis": "b"}]})[1] != [])
    mixed = facts.build("## [PRIMARY] filing_tables: latest 8-K, filed 2026-07-28\n\n### Segments\n"
                        "- Ratings — Six Months 2026: USD 2,641mn\n- Pro forma revenue — Six "
                        "Months 2026: USD 7,397\n", "")
    share, why = facts.fill("Ratings is {{share:F1/F2}} of revenue.", mixed, {})
    check("a share of an amount in millions over one in units is refused, not printed",
          "n.a." in share and "different scales" in " ".join(why), share + str(why))
    # SPGI 2026-10-03: the 2Q26 margin declared as an assumption with the release quoted.
    marg = facts.build("## [PRIMARY] filing_tables: latest 8-K, filed 2026-07-28\n\n### Margins\n"
                       "- Adjusted operating margin — Three Months 2026: 54.3%\n"
                       "- Tax rate — Three Months 2026: 21.0%\n", "")
    declared, _ = facts.assumptions_from({"assumptions": [
        {"id": "adj_margin_2q26", "value": "54.3%", "basis": "2Q26 earnings release"},
        {"id": "buyback_2026", "value": "USD 7bn", "basis": "management guided >$7bn"},
        {"id": "energy_growth", "value": "5%", "basis": "energy transition demand"
                                                         " outpaces the group"}]})
    kept, as_fact, why = facts.reconcile_assumptions(declared, marg)
    check("an assumption that is a fact becomes that fact; one credited to a source is "
          "dropped; the writer's own stays",
          as_fact.get("adj_margin_2q26") == "F1" and list(kept) == ["energy_growth"]
          and len(why) == 1 and "buyback_2026" in why[0], f"{kept} {as_fact} {why}")
    doubled, _ = facts.fill("Amortisation of ~{{F1}}mn and ${{F1}} again.", mixed, {})
    check("a unit typed around a fact that carries one is dropped",
          "~USD 2,641mn and USD 2,641mn" in doubled and "USD USD" not in doubled, doubled)
    trig, marked = facts.mark_own("Upgrade to HOLD if organic growth sustains >7% for four "
                                  "quarters.")
    check("the writer's threshold for changing the call is its own figure",
          len(marked) == 1 and "(my assumption)" in trig, trig)
    model_vals = {"g": "3.0%", "ebit_margin:FY2030": "48.0%", "ev_ebitda": "15.0x",
                  "implied_cagr": "10.9%"}
    own3, marked3 = facts.mark_own("My model embeds 5% terminal growth for the pair. "
                                   "I model a 3% perpetuity growth rate.", model_vals)
    check("the writer's own figure that contradicts the model is not kept as its own",
          len(marked3) == 1 and marked3[0].startswith("I model"), str(marked3))
    moved = facts.adopt_typed("EBIT margin expands to 48% in FY2030 ({{ebit_margin:FY2030}}). "
                              "Today the stock trades at 15.0x trailing and pays for 11% growth.",
                              facts.Facts({}, "", ""), model_vals)
    check("a typed figure beside its citation gives way to it, in its place; today's multiple "
          "and the implied growth are recognised",
          moved.startswith("EBIT margin expands to {{ebit_margin:FY2030}} in FY2030.")
          and "{{ev_ebitda}}" in moved and "{{implied_cagr}}" in moved, moved)
    series = facts.adopt_typed("D&A falls from 8% to 3.5% of revenue as intangibles roll off "
                               "({{da_pct_revenue:FY2030}}).", facts.Facts({}, "", ""),
                               {"da_pct_revenue:FY2026": "8.0%", "da_pct_revenue:FY2030": "3.5%",
                                "capex_pct_revenue:FY2030": "3.5%"})
    check("typed figures in a sentence citing a model series become that series' years",
          series == "D&A falls from {{da_pct_revenue:FY2026}} to {{da_pct_revenue:FY2030}} of "
                    "revenue as intangibles roll off.", series)
    heading, _ = facts.strip_typed("### (2) Market Intelligence (37% of revenue) faces AI risk\n")
    check("a heading's bracket about the removed figure goes with it",
          heading.strip() == "### (2) Market Intelligence faces AI risk", heading)
    check("the assumption is cited as its fact",
          facts.cite_instead("Margin was {{assume:adj_margin_2q26}}.", as_fact)
          == "Margin was {{F1}}.")
    more, _ = facts.assumptions_from({"assumptions": [
        {"id": "terminal_growth", "value": "2%", "basis": "a mature franchise growing with GDP"},
        {"id": "perpetuity_growth", "value": "2.5%", "basis": "a mature franchise with GDP"},
        {"id": "debt_growth", "value": "4%", "basis": "IMF/IIF estimates of global debt"},
        {"id": "mix", "value": "55%", "basis": "estimated"},
        {"id": "attach", "value": "30%", "basis": "pricing power on new AI products lifts it"}]})
    kept, cite_as, why = facts.reconcile_assumptions(more, marg, {"g": "2.5%"})
    check("an assumption is valid only with a reason of its own, no source, and the model's "
          "value for a model input",
          list(kept) == ["attach"] and cite_as == {"perpetuity_growth": "g"} and len(why) == 3
          and "{{g}}" in why[0] and "IMF" in why[1] and "no reason" in why[2], f"{kept} {cite_as} {why}")
    past = facts.historical_assumptions("Margin was {{assume:attach}} in 2Q26. Margin reaches "
                                        "{{assume:attach}} in FY2028, up from 2Q26.", marg)
    check("an assumption given for a reported period is caught; one for the forecast is not",
          [s[:10] for s, _ in past] == ["Margin was"], str(past))
    # NOK 2026-10-04: a target the path heads toward from a reported quarter is not a
    # figure for that quarter.
    toward = facts.historical_assumptions("Margin moves from the 2Q26 level toward the "
                                          "{{assume:attach}} target as savings flow through.", marg)
    check("an assumption the text heads toward from a reported period is not the past",
          toward == [], str(toward))

    # NOK 2026-10-04: figures in the company's own sentences, which no table holds.
    said = facts.build(
        "## [PRIMARY] fetch_page: https://www.nokia.com/newsroom/q1-report\n\n"
        "In Nokia Technologies we continued to execute well and maintained a contracted net "
        "sales run-rate of EUR 1.4 billion.\n\n"
        "We expect Optical Networks and IP Networks combined to grow between 18% and 20%.\n\n"
        "We now expect the addressable market in AI & Cloud to grow at a 27% CAGR "
        "(2025–2028).\n\n"
        "We anticipate around 50 percent of the incremental tariff costs will be in "
        "Construction Industries, 20 percent in Resource Industries and 30 percent in Power "
        "& Energy.\n\n## [SECONDARY] fetch_page: https://example.com/a\n\nAnalysts see 9% "
        "growth in the optical transport market.\n", "")
    stated = facts.adopt_typed(
        "Nokia Technologies maintains a contracted net sales run-rate of approximately EUR "
        "1.4bn. Management expects Optical and IP Networks to grow 18-20% in FY2026. "
        "Segments are Construction Industries (35%), Power & Energy (39%) and Resource "
        "Industries (20%) of revenue. I assume 50% of incremental tariff costs are mitigated. "
        "The optical transport market grows 9% a year.", said)
    printed, _ = facts.fill(stated, said, {})
    check("a figure the company states in its own words is cited from its sentence",
          "run-rate of approximately EUR 1.4 billion (nokia.com)." in printed
          and "grow 18%-20% (nokia.com) in FY2026" in printed, printed)
    check("segment names alone, the writer's own estimate and a web page's figure stay typed",
          "Industries (20%) of revenue" in stated and "I assume 50% of" in stated
          and "grows 9% a year" in stated, stated)
    claimed, _ = facts.assumptions_from({"assumptions": [
        {"id": "ai_cycle_years", "value": "3-4", "basis": "order visibility runs to 2028 on "
         "management's 27% CAGR for the AI & Cloud market; inference adds a second wave"},
        {"id": "nrr", "value": "100%", "basis": "Company disclosure, 2025 10-K"},
        {"id": "ai_cloud_cagr", "value": "27%", "basis": "management guidance for the AI & "
         "Cloud addressable market to 2028"}]})
    kept, cite_as, why = facts.reconcile_assumptions(claimed, said)
    check("an assumption reasoned from a source stays; the company's own figure is cited; "
          "one that is only a citation is refused",
          list(kept) == ["ai_cycle_years"] and cite_as.get("ai_cloud_cagr", "").startswith("S")
          and len(why) == 1 and "nrr" in why[0], f"{kept} {cite_as} {why}")

    # SPGI 2026-10-05: sentences of the company's that the checks deleted.
    listed = facts.build(
        "## [PRIMARY] fetch_page: https://www.sec.gov/recast.htm\n\nPercentages may not sum "
        "to 100% due to rounding.\n·\nS&P Global Ratings\n–\n34% of revenue\n–\n43% of "
        "adjusted operating profit (65% margin)\n·\nS&P Dow Jones Indices\n–\n13% of revenue\n"
        "–\n18% of adjusted operating profit (70% margin)\n·\nS&P Global Energy\n–\n18% of "
        "revenue\n–\n16% of adjusted operating profit (46% margin)\n", "")
    cited = facts.adopt_typed("Per the July 6 2026 recast, Ratings operates at a 65% adjusted "
                              "margin and Indices at 70%. Energy grew 18% in the quarter.", listed)
    check("an item of the company's list is cited by its label and its own measure",
          "Ratings operates at a {{S" in cited and "Indices at {{S" in cited
          and "Energy grew 18% in" in cited, cited)
    check("a figure is the model value whose words sit beside it",
          facts.adopt_typed("The FY2030 capex funds the 3% terminal growth at the 28% return on "
                            "new capital.", listed,
                            {"g": "3.0%", "revenue_growth:FY2030": "3.2%",
                             "new_capital_return": "28.0%", "terminal_roic": "28.1%"})
          == "The FY2030 capex funds the {{g}} terminal growth at the {{new_capital_return}} "
             "return on new capital.")
    check("a typed figure beside the assumption it repeats cites the assumption",
          facts.adopt_typed("Issuance falls 30% or more, consistent with the "
                            "{{assume:cyclical_floor}} assumption.", listed,
                            {"assume:cyclical_floor": "30%"})
          .startswith("Issuance falls {{assume:cyclical_floor}} or more"))
    led = facts.misused(f"Revenue rose, led by {{{{{r_id}}}}} in Ratings and {{{{{i_id}}}}} in "
                        "Indices.", segs, ["Ratings", "Indices"])
    check("a segment named right after its figure is that figure's", led == [], str(led))
    check("intent to repurchase is guidance in words", bool(facts.GUIDANCE.search(
        "The release signaled intent to repurchase more than $7 billion in 2026.")))
    origin = facts.historical_assumptions("This embeds a normalization from the 2Q26 cyclical "
                                          "peak and the {{assume:drop}} downside scenario.", marg)
    check("a reported period the scenario starts from is not the assumption's period",
          origin == [], str(origin))


def test_model_rules():
    """The skill's quality assessments, period rule and method revisions."""
    import model_rules
    model = {"quality": QUALITY, "years": ["FY2026", "FY2027"]}
    check("a complete quality block passes", model_rules.quality_problems(model, {"O1"}) == [],
          str(model_rules.quality_problems(model, {"O1"})))
    check("quality is required",
          "`model.quality` is missing" in " ".join(model_rules.quality_problems({"years": []})))
    bad = json.loads(json.dumps(QUALITY))
    bad["balance_sheet"] = {"rating": "excellent", "summary": "Net debt is 2.5x EBITDA and "
                            "falling every year since 2021.", "claims": ["Z9"]}
    got = " ".join(model_rules.quality_problems({"quality": bad}, {"O1"}))
    check("quality: rating, typed numbers and unknown claims are caught",
          "strong, adequate or weak" in got and "2.5x" in got and "`Z9`" in got, got)
    table = model_rules.quality_table({"quality": QUALITY})
    check("quality table has the four areas",
          all(x in table for x in ("Business quality", "Earnings outlook", "Balance sheet",
                                   "Competitive position")) and "| Adequate |" in table, table)

    # AVGO, 2026-09-24: Q3's AI revenue went in as FY2025.
    model = {"years": ["FY2026"], "segments": {"AI": [65000], "Other": [30000]},
             "segment_base": {"AI": 16700, "Other": 29000},
             "drivers": {"Units": {"unit": "mn", "base": 10, "values": [41]},
                         "Growth": {"unit": "%", "base": 5, "values": [40]}}}
    got = model_rules.period_problems(model)
    check("a quarter standing for a year is caught in segments and drivers",
          len(got) == 2 and "`AI`" in got[0] and "`Units`" in got[1]
          and "never describes a forecast year" in got[0], str(got))

    old = {"terminal": {"method": "perpetuity", "growth": 0.03}, "beta": 1.1,
           "years": ["FY2026"] * 5,
           "scenario_probabilities": {"bull": .25, "base": .5, "bear": .25}}
    new = json.loads(json.dumps(old))
    new["terminal"]["growth"] = 0.025
    new["beta"] = 0.9
    check("unchanged method choices need no revision",
          model_rules.revision_problems(old, old) == [])
    got = model_rules.revision_problems(old, new)
    check("changed method choices need a logged reason",
          len(got) == 2 and "terminal growth" in got[0] and "beta" in got[1], str(got))
    new["method_revisions"] = [
        {"choice": "terminal growth", "from": "3%", "to": "2.5%",
         "reason": "the 10-K shows volumes flat for five years, so 3% outruns the market"},
        {"choice": "beta", "from": "1.1", "to": "0.9",
         "reason": "the lower beta gives a BUY rating, which fits the thesis better"}]
    got = model_rules.revision_problems(old, new)
    check("a revision argued from the rating is refused",
          len(got) == 1 and "argues from the result" in got[0], str(got))
    table = model_rules.revisions_table(new)
    check("revisions table lists every change", table.count("\n| ") == 4 and "2.5%" in table,
          table)


def test_revenue_build(data):
    """Revenue built in code from volume x price, rebuilt against the base year."""
    import revenue_build
    model = synthetic_model()
    model.pop("revenue")
    model["revenue_build"] = [
        {"line": "Premium", "volume_unit": "mn subscribers", "base_volume": 10,
         "base_price": 80, "volume_growth": 0.1, "price_growth": [0, 0, 0.05, 0.05, 0.05],
         "basis": "sourced", "definition": "paying subscribers at the year end times "
                                           "revenue per subscriber"},
        {"line": "Other", "base_revenue": 200, "growth": 0.02}]
    built, build = revenue_build.apply(model, 1000, 5)
    check("lines multiply out into segments",
          abs(built["segments"]["Premium"][0] - 880) < 1e-6
          and abs(built["segments"]["Premium"][2] - 10 * 1.1 ** 3 * 84) < 1e-6
          and built["segment_base"] == {"Premium": 800.0, "Other": 200.0}
          and "revenue" not in built, str(built["segments"]))
    check("each line's volume becomes its segment's driver",
          built["drivers"]["Premium volume"]["segment"] == "Premium"
          and built["drivers"]["Premium volume"]["base"] == 10)
    check("the model given is not changed", "segments" not in model and "revenue_build" in model)

    short = json.loads(json.dumps(model))
    short["revenue_build"][0]["base_price"] = 70
    try:
        revenue_build.apply(short, 1000, 5)
        check("a build that misses the base year by more than 2% is a stop", False)
    except revenue_build.BuildError as exc:
        check("a build that misses the base year by more than 2% is a stop",
              "within 2%" in str(exc) and "-10.0%" in str(exc), str(exc))

    # TSLA of 2026-10-13: a price per GWh given in millions lost the whole valuation.
    slipped = json.loads(json.dumps(model))
    slipped["revenue_build"][0]["base_price"] = 80e-6
    built, build = revenue_build.apply(slipped, 1000, 5)
    check("a price off by a power of 1,000 on one line is rescaled with a warning",
          abs(built["segment_base"]["Premium"] - 800) < 1e-6
          and abs(built["segments"]["Premium"][0] - 880) < 1e-6
          and abs(build["lines"][0]["base_price"] - 80) < 1e-9
          and any("rescaled by 1,000,000" in w for w in build["warnings"]),
          str(build["warnings"]))
    unfixed = json.loads(json.dumps(model))
    unfixed["revenue_build"][0]["base_price"] = 0.5
    try:
        revenue_build.apply(unfixed, 1000, 5)
        check("a miss no power of 1,000 explains stays a stop", False)
    except revenue_build.BuildError:
        check("a miss no power of 1,000 explains stays a stop", True)

    chained = json.loads(json.dumps(model))
    chained["revenue_build"][0].pop("volume_growth")
    chained["revenue_build"][0]["market_chain"] = [
        {"name": "Smartphone users", "unit": "mn", "base": 100,
         "values": [110, 120, 130, 140, 150], "label": "sourced", "definition": "GSMA"},
        {"name": "Penetration", "unit": "", "base": 0.2, "values": 0.2, "label": "analyst"},
        {"name": "Share", "unit": "", "base": 0.5, "values": [0.5, 0.5, 0.5, 0.5, 0.6],
         "label": "analyst"}]
    built, build = revenue_build.apply(chained, 1000, 5)
    check("a market chain sets the line's volume",
          abs(built["drivers"]["Premium volume"]["values"][0] - 11) < 1e-9
          and abs(built["drivers"]["Premium volume"]["values"][4] - 18) < 1e-9,
          str(built["drivers"]["Premium volume"]["values"]))
    table = revenue_build.chain_table(build["chains"], ["FY2027", "FY2028", "FY2029",
                                                        "FY2030", "FY2031"])
    check("the market chain is printed link by link",
          "Premium: Penetration" in table and "20.0%" in table and "GSMA" in table, table)
    chained["revenue_build"][0]["market_chain"][1]["base"] = 20
    try:
        revenue_build.apply(chained, 1000, 5)
        check("a chain that does not produce the base volume is a stop", False)
    except revenue_build.BuildError as exc:
        check("a chain that does not produce the base volume is a stop",
              "fractions" in str(exc), str(exc))

    v = valuation.compute(model, data)
    check("the valuation reads the built revenue",
          abs(v.rows[0].revenue - 1084) < 1e-6 and v.revenue_build is not None
          and "Premium volume" in v.drivers, str(v.rows[0].revenue))
    check("the scenarios cut the built revenue",
          not v.downside_rows or v.downside_rows[0].revenue <= v.rows[0].revenue)
    check("a DCF without a build or a reason is sent back",
          revenue_build.missing_problems({"years": []})
          and not revenue_build.missing_problems(
              {"revenue_build_reason": "The company reports no unit or price measure at all."}))


def test_conglomerate(data):
    """Margins by segment, parts tied to the forecast, the discount and the primary method."""
    import conglomerate
    model = synthetic_model()
    model.pop("revenue")
    model.pop("ebit_margin")
    model["segments"] = {"Aero": [600, 660, 726, 798.6, 878.46],
                         "Industrial": [500, 550, 605, 665.5, 732.05]}
    model["segment_base"] = {"Aero": 545.45, "Industrial": 454.55}
    model["segment_margins"] = {"Aero": 0.25, "Industrial": [0.10, 0.10, 0.12, 0.12, 0.12]}
    model["corporate_costs"] = {"amount": 22, "multiple": 10}
    v = valuation.compute(model, data)
    check("the group margin is built from the segments, less corporate costs",
          abs(v.rows[0].ebit_margin - (600 * 0.25 + 500 * 0.10 - 22) / 1100) < 1e-9
          and abs(v.rows[2].ebit_margin - (726 * 0.25 + 605 * 0.12 - 22) / 1331) < 1e-9,
          f"{v.rows[0].ebit_margin:.4f}")
    table = valuation.volume_build_table(v)
    check("margins by segment are printed", "Margins by segment" in table
          and "Group operating margin" in table, table[-600:])

    bad = json.loads(json.dumps(model))
    bad["segment_margins"].pop("Industrial")
    try:
        valuation.compute(bad, data)
        check("segment margins must cover every segment", False)
    except valuation.ModelInputError as exc:
        check("segment margins must cover every segment", "missing Industrial" in str(exc),
              str(exc))

    model["parts"] = {"Aero": {"metric": "EBIT", "amount": 150, "year": "FY2027",
                               "multiple": 16, "source": "aerospace peers"},
                      "Industrial": {"metric": "EBIT", "amount": 80, "year": "FY2027",
                                     "multiple": 12, "source": "industrial peers"}}
    v = valuation.compute(model, data)
    got = " ".join(conglomerate.problems(model, v))
    check("parts must agree with the segment's forecast EBIT",
          "`Industrial` is valued on 80" in got and "`Aero`" not in got, got)
    check("parts need an argued conglomerate discount", "conglomerate_discount" in got, got)

    model["parts"]["Industrial"]["amount"] = 50
    model["conglomerate_discount"] = {"rate": 0.15, "reason": "The group has traded below "
                                      "its parts for a decade and peers that split re-rated."}
    v = valuation.compute(model, data)
    check("a tied, argued sum of the parts has no problems",
          conglomerate.problems(model, v) == [], str(conglomerate.problems(model, v)))
    check("the discount comes off the sum of the parts",
          abs(v.parts["ev"] - 0.85 * v.parts["gross_ev"]) < 1e-6
          and "Conglomerate discount (15%)" in valuation.parts_markdown(v))
    check("the DCF sets the rating unless the parts are argued as primary",
          v.primary == "dcf" and v.recommendation_value == (v.weighted_per_share or v.per_share))

    model["valuation_method"] = "sum_of_parts"
    model["valuation_method_reason"] = "The segments share no customers, capital or cycle, so " \
                                       "each is valued as its peers are."
    v = valuation.compute(model, data)
    check("an argued sum of the parts sets the value and the rating",
          v.primary == "sum_of_parts" and v.recommendation_value == v.parts["per_share"]
          and v.rating == valuation.rating_for(v.recommendation_upside)
          and "primary method" in valuation.parts_markdown(v), v.rating)
    del model["valuation_method_reason"]
    check("sum of the parts as the method needs its reason",
          "valuation_method_reason" in " ".join(conglomerate.problems(model, v)))
    import model_rules
    check("switching to the sum of the parts is a logged method revision",
          any("valuation method" in p for p in model_rules.revision_problems(
              synthetic_model(), model)))


def test_claim_types():
    """The skill's six claim types: what each must carry, and opinion only where typed."""
    def section(name, *points):
        return {"section": name, "points": list(points)}

    def point(pid, kind, claim, **extra):
        return {"id": pid, "kind": kind, "claim": claim, "confidence": "medium",
                "evidence": ["a"], "figures": ["b"], "depends_on": [], **extra}

    view = dict(counterargument="Rivals may cut price.",
                distinguishing_evidence="Share holding while rivals cut price favours my view")
    plan = {"outline": [
        section("Opening",
                point("O1", "fact", "I believe revenue grew in 2Q26"),
                point("O2", "fact", "Revenue grew because pricing rose"),
                point("O3", "fact", "Management attributes the growth to pricing"),
                point("O4", "inference", "Pricing power is durable", figures=[])),
        section("Revenue Growth and Margins",
                point("R1", "hypothesis", "Price rises cut churn", test=""),
                point("R2", "hypothesis", "Price rises may cut churn",
                      test="churn falls in the quarters after each rise", **view),
                point("R3", "assumption", "I expect margins to widen")),
        section("Conclusions",
                point("K1", "conclusion", "The stock is mispriced", depends_on=["O1"]))]}
    got = outline_mod.kind_problems(plan)
    joined = " | ".join(got)
    check("a fact that gives an opinion is caught", "`O1` is a fact but gives an opinion" in joined,
          joined)
    check("a fact that states a cause is caught unless attributed",
          "`O2` is a fact but states a cause" in joined and "`O3`" not in joined, joined)
    check("an inference on one support is caught", "`O4` is an inference on one support" in joined)
    check("a hypothesis needs a test and hedged wording",
          "`R1` is a hypothesis and needs `test`" in joined
          and "`R1` is a hypothesis worded as a fact" in joined
          and "`R2` is a hypothesis" not in joined, joined)
    check("a forecast driver view needs its counterargument and distinguishing evidence",
          "`R3` in Revenue Growth and Margins is an assumption" in joined
          and "`R3` in Revenue Growth and Margins needs `distinguishing_evidence`" in joined
          and "`R2` in" not in joined, joined)
    check("a conclusion needs two points under it, and the section two conclusions",
          "`K1` is a conclusion and needs `depends_on`" in joined
          and "at least two `conclusion` points" in joined, joined)
    rendered = outline_mod.render(plan)
    check("the prose request shows each point's kind and a hypothesis's test",
          "*hypothesis* Price rises may cut churn" in rendered
          and "Test: churn falls" in rendered and "What would tell them apart:" in rendered,
          rendered)


def test_review_evidence_and_history(data):
    """A reviewer's factual objection needs its quote in the notes; earlier runs are kept."""
    digest = "The 10-Q reports net revenue of $29.6 billion for the quarter ended August 2."
    review = {"verdict": "revise", "issues": [
        {"severity": "high", "section": "Revenue", "claim": "FY2026 revenue of $106bn",
         "problem": "The 10-Q reports total revenue of $33bn for FY2026.", "fix": "",
         "evidence": "The 10-Q reports total revenue of $33bn for FY2026"},
        {"severity": "high", "section": "Revenue", "claim": "Q3 revenue",
         "problem": "The quarter was $29.6bn.", "fix": "",
         "evidence": "net revenue of $29.6 billion for the quarter ended August 2"},
        {"severity": "high", "section": "Margins", "claim": "54% EBIT margin",
         "problem": "No evidence for the margin path.", "fix": "", "evidence": ""}]}
    kept = pitch_mod._verified(review, digest, data, "Body.")["issues"]
    check("a reviewer's quote that is not in the notes drops its objection",
          [i["claim"] for i in kept] == ["Q3 revenue", "54% EBIT margin"], str(kept))

    runs = history.parse(history.dump([
        history.Run("AVGO", "2026-09-24", "USD", 354.99, 253.66, -29.0, "SELL"),
        history.Run("AVGO", "2026-10-01", "USD", 351.19, 346.54, -1.0, "HOLD"),
        history.Run("NFLX", "2026-10-01", "USD", 68.33, 45.15, -34.0, "SELL")]))
    runs = history.merge(runs, runs[:1])
    earlier = history.earlier(runs, "avgo")
    check("earlier runs of a ticker are kept once each, oldest first",
          [r.value for r in earlier] == [253.66, 346.54], str(earlier))
    lines = history.describe(earlier, 300.0)
    check("each earlier run is listed, then the spread of values",
          lines[0].startswith("24 Sep 2026: USD 253.66 SELL") and "-15% to +16%" in lines[-1],
          str(lines))
    check("an unreadable history is empty, not an error",
          history.parse(b"not json") == [] and history.parse(None) == [])
    if history.SEED.exists():
        check("the seed holds the runs before the history existed",
              any(r.symbol == "AVGO" and r.date == "2026-09-24" for r in history.load_local()))


def wrong_figures(text, v, data=None, model=None):
    """The figures provenance.audit finds wrong in `text`, as (figure, kind)."""
    pool = provenance.build_pool([], [], v, data or synthetic_data(), model or {})
    return [(fl.figure.text, fl.kind) for fl in provenance.audit(text, pool) if fl.wrong]


def test_consistency():
    """The prose may not contradict the computed model."""
    v = valuation.compute(synthetic_model(), synthetic_data())
    clean = ("I arrive at an intrinsic price of USD 296.25, an upside of +19%. "
             "Recommendation: HOLD. The exit-multiple method gives an implied share price of "
             "USD 305.00. Free cash flow reaches $242mn by FY2031. Revenue of $1.6bn in FY2031. "
             "Consensus expects revenue of $3bn in FY2031.")
    problems = consistency.check(clean, v)
    check("consistent prose passes", problems == [], str(problems))

    # The NVDA pitch's failures, replayed against this model.
    nvda = ("Intrinsic Price = $164. That is 29% downside. I rate the shares a SELL. "
            "Free cash flow reaches $0.5bn by FY2031. Operating margin of 25% in FY2029.")
    problems = " | ".join(consistency.check(nvda, v))
    check("wrong intrinsic price caught", "164" in problems, problems)
    check("wrong upside caught", "29% downside" in problems, problems)
    check("wrong rating caught", "SELL" in problems, problems)
    wrong = wrong_figures(nvda, v)
    check("wrong forecast FCF caught", ("$0.5bn", "year") in wrong, str(wrong))
    check("wrong forecast margin caught", ("25%", "year") in wrong, str(wrong))

    ranged = "Revenue grows from $1.1bn in FY2027 to $1.6bn by FY2031, a steady climb."
    check("a range's two ends are not paired with the wrong year",
          consistency.check(ranged, v) == [] and wrong_figures(ranged, v) == [],
          str(wrong_figures(ranged, v)))

    quarterly = ("Data center revenue kept compounding (Q2 FY2031: $0.4bn); revenue "
                 "reached $0.8bn in 1H FY2031.")
    check("a quarter's figure is not read as the year's",
          wrong_figures(quarterly, v) == [], str(wrong_figures(quarterly, v)))
    check("the full year is still checked",
          wrong_figures("Revenue of $0.4bn in FY2031.", v) == [("$0.4bn", "year")])

    table_only = "| FCF | FY2031 | $999bn |"
    check("generated tables are not re-checked", consistency.check(table_only, v) == [])


def test_placeholders_and_tables():
    v = valuation.compute(synthetic_model(), synthetic_data())
    body, unknown = pitch_mod._fill_placeholders(
        "Worth {{intrinsic}} ({{ upside }}); FCF {{fcf:FY31}} and {{bogus}}.",
        valuation.placeholders(v))
    check("placeholders filled, year labels normalised",
          "USD 296.25" in body and "+18%" in body and "USD 242mn" in body, body)
    check("unknown placeholder reported", unknown == ["{{bogus}}"], str(unknown))

    body = ("Intro.\n\n## Model Outputs\n\n### Definitions\n\nTAM text.\n\n"
            "### Financial Projections\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n")
    out = pitch_mod._insert_tables(body, "### DCF Build\n\nGENERATED")
    check("typed tables removed, definitions kept, generated appended",
          "| 1 | 2 |" not in out and "TAM text." in out and out.rstrip().endswith("GENERATED"),
          out)
    out = pitch_mod._insert_tables("Intro only.", "GENERATED")
    check("missing Model Outputs section created",
          "## Model Outputs" in out and "GENERATED" in out)


def test_share_count_and_cover(data):
    """GOOG and CEG of 2026-09-29: a one-class share count, and a placeholder on the cover."""
    # GOOG: Yahoo counted the class C shares alone, half of what the market cap covers.
    v = valuation.compute(synthetic_model(), synthetic_data(market_cap=250.0 * 2e7))
    check("a one-class share count is replaced by market cap / price",
          v.shares == 2e7 and v.shares_from_market_cap
          and any("share class" in n for n in v.notes), f"{v.shares} {v.notes}")
    check("the per-share value halves with twice the shares",
          abs(v.per_share - valuation.compute(synthetic_model(), synthetic_data()).per_share / 2)
          < 0.01, f"{v.per_share:.2f}")
    table = valuation.model_outputs_markdown(v)
    check("the valuation table says where the share count came from",
          "market cap / price, all share classes" in table)
    v = valuation.compute(synthetic_model(), synthetic_data(market_cap=250.0 * 1.03e7))
    check("a share count within 5% of the market cap is kept",
          v.shares == 1e7 and not v.shares_from_market_cap
          and not [n for n in v.notes if "house WACC" not in n], str(v.notes))
    v = valuation.compute(synthetic_model(), synthetic_data(market_cap=250.0 * 1e7 / 100))
    check("a 100x gap (minor units) is not taken for a missing share class", v.shares == 1e7)
    v = valuation.compute(synthetic_model(), synthetic_data(shares_outstanding=None,
                                                            market_cap=250.0 * 1e7))
    check("no provider count falls back to market cap / price", v.shares == 1e7)

    # CEG: the one-liner reached the PDF as "downside to {{intrinsic}} intrinsic value".
    headline = json.loads(json.dumps(HEADER_OBJ))
    headline["one_liner"] = "Priced for perfection; downside to {{intrinsic}} intrinsic value."
    headline["thesis_pillars"] = ["Worth {{intrinsic}} ({{upside}})", "B", "C"]
    result, problems = pitch_mod._finalise(headline, BODY, data)
    one_liner, pillar = result.get("one_liner"), result.get("thesis_pillars")[0]
    check("placeholders on the cover are filled",
          "{{" not in one_liner and data.currency in one_liner
          and "{{" not in pillar and "%" in pillar, f"{one_liner} | {pillar}")
    check("the caller's header is not changed",
          headline["thesis_pillars"][0] == "Worth {{intrinsic}} ({{upside}})")
    headline["one_liner"] = "Downside to {{nonsense}}."
    result, problems = pitch_mod._finalise(headline, BODY, data)
    check("an unknown placeholder on the cover is a problem",
          any("{{nonsense}}" in p for p in problems) and "{{" not in result.get("one_liner"),
          str(problems))


def test_downside_drivers_and_typed_beside(data):
    """2026-10-01: downside margins flagged as base-case errors, a driver read as revenue,
    and typed figures contradicting the placeholder printed beside them."""
    model = synthetic_model(
        downside={"revenue_change": -0.1, "ebit_margin": 0.12},
        drivers={"AI revenue": {"unit": "USD mn", "base": 200,
                                "values": [300, 400, 500, 600, 700]}})
    v = valuation.compute(model, synthetic_data())
    risk = ("**Downside case.** Inputs: revenue 10% below base each year, EBIT margin 12% "
            "by FY2031 (vs 20% base).")
    check("a downside-case margin in the downside paragraph is not a contradiction",
          wrong_figures(risk, v, model=model) == [], str(wrong_figures(risk, v, model=model)))
    base = "Margins expand on scale.\n\nI model EBIT margin of 12% by FY2031."
    check("the same margin outside the downside paragraph is still caught",
          wrong_figures(base, v, model=model) == [("12%", "year")],
          str(wrong_figures(base, v, model=model)))
    driver = "AI revenue reaches $300mn in FY2027, then $700mn by FY2031."
    check("a driver's own figure for the year is not read as total revenue",
          wrong_figures(driver, v, model=model) == [], str(wrong_figures(driver, v, model=model)))
    check("a driver's figure is held to the driver",
          wrong_figures("AI revenue reaches $450mn in FY2027.", v, model=model)
          == [("$450mn", "year")], str(wrong_figures("AI revenue reaches $450mn in FY2027.",
                                                     v, model=model)))

    values = valuation.placeholders(v)
    check("the downside case's inputs have placeholders",
          values.get("downside_revenue_cut") == "10%"
          and values.get("downside_ebit_margin:FY2031") == "12.0%" and "downside_wacc" in values,
          str({k: x for k, x in values.items() if k.startswith("downside")}))
    exit_model = json.loads(json.dumps(model))
    exit_model["terminal"] = {"method": "exit_multiple", "exit_ev_ebit": 10, "growth": 0.02}
    exit_values = valuation.placeholders(valuation.compute(exit_model, synthetic_data()))
    check("an exit-multiple model with a growth cross-check has both downside terminal "
          "placeholders", "downside_g" in exit_values and "downside_exit_multiple" in exit_values,
          str([k for k in exit_values if k.startswith("downside")]))
    fx_empty = facts.Facts({}, "", "")
    adopted = facts.adopt_typed("**Downside case.** EBIT margin falls to 12.0% by FY2031. "
                                "EBIT margin reaches 12.0% by FY2031.", fx_empty, values)
    check("a downside input typed in a sentence about the downside case is cited; elsewhere not",
          adopted.startswith("**Downside case.** EBIT margin falls to "
                             "{{downside_ebit_margin:FY2031}} by FY2031.")
          and adopted.endswith("reaches 12.0% by FY2031."), adopted)
    raw = ("FY2031 FCF margin ~9% ({{fcf_margin:FY2031}}), FCF $1.4bn ({{fcf:FY2031}}). "
           "EBIT margin 20% ({{ebit_margin:FY2031}}) by FY2031 ({{revenue:FY2031}}).")
    problems = pitch_mod._typed_beside_problems(raw, values)
    check("typed figures that disagree with the placeholder beside them are caught",
          len(problems) == 2 and "~9%" in problems[0] and "$1.4bn" in problems[1],
          str(problems))

    headline = json.loads(json.dumps(HEADER_OBJ))
    body = BODY.replace("Free cash flow reaches",
                        "FCF margin is ~1% ({{fcf_margin:FY2030}}). Free cash flow reaches")
    result, problems = pitch_mod._finalise(headline, body, data)
    margin = result.valuation.row("FY2030").fcf_margin
    check("a typed figure beside its placeholder is dropped, the code's figure kept",
          "~1%" not in result.body and f"FCF margin is {margin:.1%}." in result.body
          and not any("~1%" in p for p in problems), result.body[:300])
    check("an agreeing duplicate is dropped too",
          pitch_mod._drop_typed_beside("margin 10.5% ({{ebit_margin:FY2030}})")
          == "margin {{ebit_margin:FY2030}}")
    filled, _ = pitch_mod._fill_placeholders("subscribers reach {{driver.Subs:FY2031}}M",
                                             {"driver.Subs:FY2031": "405mn"})
    check("a unit typed after a placeholder that has one is dropped", filled ==
          "subscribers reach 405mn", filled)


def test_beta_floor_and_finance_arm():
    """ESLT and CAT, 2026-10-01: a 0.11 beta, and a finance arm's debt in the bridge."""
    eslt = synthetic_data(beta=0.11, beta_source="5-year monthly vs the S&P 500",
                          risk_free=0.053)
    cost = valuation.cost_of_capital(eslt)
    check("a beta below the floor is held at it",
          abs(cost.beta - config.BETA_FLOOR) < 1e-9
          and abs(cost.cost_of_equity - (0.053 + config.BETA_FLOOR * config.EQUITY_RISK_PREMIUM))
          < 1e-9, f"{cost.beta} {cost.cost_of_equity}")
    check("the build says the floor was applied",
          "0.11 raw, adjusted to 0.40, held at the 0.60 floor" in cost.describe(), cost.describe())
    nee = valuation.cost_of_capital(synthetic_data(beta=0.61, risk_free=0.053))
    check("a utility's beta above the floor is untouched",
          abs(nee.beta - (0.67 * 0.61 + 0.33)) < 1e-9 and "floor" not in nee.describe(),
          nee.describe())

    data = synthetic_data(debt=2000.0)
    base = valuation.compute(synthetic_model(), data)
    v = valuation.compute(synthetic_model(captive_finance_debt=1500.0,
                                           captive_finance_source="10-Q, Financial Products"),
                          data)
    check("a finance arm's debt is left out of the bridge",
          abs(v.per_share - base.per_share - 1500.0 * 1e6 / 1e7) < 0.01
          and v.debt == 2000.0 and v.bridge_debt == 500.0, f"{base.per_share} {v.per_share}")
    table = valuation.model_outputs_markdown(v)
    check("the bridge table shows the finance arm's debt and its source",
          "Finance Arm's Debt" in table and "10-Q, Financial Products" in table)
    check("the WACC weighs only the bridge debt",
          valuation.cost_of_capital(data, {"captive_finance_debt": 1500.0}).debt_weight
          < valuation.cost_of_capital(data).debt_weight)
    v = valuation.compute(synthetic_model(captive_finance_debt=5000.0), data)
    check("finance-arm debt above total debt is held to it, with a source asked for",
          v.finance_debt == 2000.0 and len(v.warnings) == 2, str(v.warnings))
    try:
        valuation.compute(synthetic_model(captive_finance_debt=-1.0), data)
        check("negative finance-arm debt is rejected", False)
    except valuation.ModelInputError:
        check("negative finance-arm debt is rejected", True)

    notes = "Financial Products segment revenues rose. Financial Products debt was $1,500 million."
    no_key = valuation.compute(synthetic_model(), data)
    problems = pitch_mod._finance_arm_problems(synthetic_model(), no_key, data, notes)
    check("notes naming a finance arm ask for its debt",
          len(problems) == 1 and "captive_finance_debt" in problems[0], str(problems))
    model = synthetic_model(captive_finance_debt=1500.0, captive_finance_source="10-Q")
    check("a finance-arm debt the notes give passes",
          pitch_mod._finance_arm_problems(model, valuation.compute(model, data), data, notes) == [])
    model = synthetic_model(captive_finance_debt=1200.0, captive_finance_source="10-Q")
    problems = pitch_mod._finance_arm_problems(model, valuation.compute(model, data), data, notes)
    check("a finance-arm debt not in the notes is sent back",
          len(problems) == 1 and "not a figure in the research notes" in problems[0],
          str(problems))
    model = synthetic_model(captive_finance_debt=0)
    check("0 confirms there is no finance-arm debt",
          pitch_mod._finance_arm_problems(model, valuation.compute(model, data), data, notes) == [])
    check("no finance arm in the notes asks nothing",
          pitch_mod._finance_arm_problems(synthetic_model(), no_key, data, "Software sales.") == [])


def test_shipped_pitch_errors(data):
    """What the NFLX, NEE and AVGO pitches of 2026-09-23/24 shipped with, replayed."""
    v = valuation.compute(HEADER_OBJ["model"], data)
    values = valuation.placeholders(v)
    body, unknown = pitch_mod._fill_placeholders(
        "Subscribers reach {{drivers.Premium subscribers:FY2030}} million and ad revenue "
        "{{segments.Ad-Supported:FY2030}} million by FY2030.", values)
    check("segment and driver placeholders filled by name",
          unknown == [] and "reach 400mn and" in body
          and values["segment.Ad-Supported:FY2030"] + " by" in body, body)

    body, unknown = pitch_mod._fill_placeholders("{{drivers.Global Subscribers:FY2030}}", values)
    check("a placeholder with spaces and dots is reported, not printed raw",
          unknown == ["{{drivers.Global Subscribers:FY2030}}"] and "{{" not in body, body)
    problem = pitch_mod._placeholder_problem(unknown[0], v, True)
    check("a missing driver names the ones that exist",
          "Premium subscribers" in problem, problem)

    body, _ = pitch_mod._fill_placeholders(
        "A terminal value of USD {{tv}} million, EV of ${{ev}}, a {{wacc}}% WACC, "
        "{{exit_multiple}}x EBIT, {{nothing}}x peers, {{shares}} million shares.", values)
    check("units the value already carries are not doubled",
          "USD USD" not in body and "bn million" not in body and "$USD" not in body
          and "%%" not in body and "xx" not in body and "n.a.x" not in body
          and "mn million" not in body, body)
    body, _ = pitch_mod._fill_placeholders("FCF {{fcf:FY2030}} and USD {{upside}}", values)
    check("a word before a placeholder is not taken for a currency",
          body.startswith("FCF EUR") or body.startswith("FCF USD"), body)

    plain = valuation.compute(synthetic_model(terminal={"method": "perpetuity", "growth": 0.02}),
                              synthetic_data())
    for name, wanted in (("{{peer_pe}}", "no peer multiples"),
                         ("{{cross_check}}", "only one terminal method"),
                         ("{{vs_consensus}}", "no sell-side target"),
                         ("{{fcf:FY2040}}", "not a forecast year")):
        problem = pitch_mod._placeholder_problem(name, plain, False)
        check(f"{name} without a value says why", "has no value" in problem
              and wanted in problem, problem)
    problem = pitch_mod._placeholder_problem("{{exit_ev}}", plain, False)
    check("an invented name is called unknown", problem.startswith("unknown placeholder"), problem)

    direction = consistency.check("My value sits -9% above the consensus target.", plain)
    check("a negative figure 'above' is caught", any("-9% above" in p for p in direction),
          str(direction))
    fine = consistency.check("My value sits 9% below the target and +18% above the price.",
                             plain)
    check("sign and direction that agree pass", not any("direction" in p for p in fine),
          str(fine))

    nflx = valuation.compute(synthetic_model(da_pct_revenue=0.375, capex_pct_revenue=0.015),
                             synthetic_data(price=2000.0))
    check("D&A far above capex in the final year is sent back",
          any("D&A" in w and "capex" in w for w in nflx.warnings), str(nflx.warnings))
    fading = valuation.compute(
        synthetic_model(da_pct_revenue=[0.12, 0.10, 0.08, 0.07, 0.06], capex_pct_revenue=0.05),
        synthetic_data())
    check("amortisation that runs off by the final year passes",
          not any("D&A" in w for w in fading.warnings), str(fading.warnings))

    nee = synthetic_data(price=1500.0, analyst_target=1900.0)
    far = valuation.compute(synthetic_model(), nee)
    check("a value far from both the price and the Street is sent back",
          any("Street average" in w for w in far.warnings), str(far.warnings))
    street_agrees = valuation.compute(synthetic_model(), synthetic_data(price=100.0,
                                                                        analyst_target=250.0))
    check("a big upside the Street shares is left alone",
          not any("implied price" in w for w in street_agrees.warnings),
          str(street_agrees.warnings))
    check("the synthetic model trips neither", not valuation.compute(
        synthetic_model(), synthetic_data()).warnings)



def test_terminal_and_downside(data):
    """EBITDA exits, unread terminal keys, and the computed downside case."""
    near = lambda a, b, tol=0.01: a is not None and abs(a - b) <= tol
    base = valuation.compute(synthetic_model(), synthetic_data())
    last = base.rows[-1]
    ebitda = valuation.compute(synthetic_model(terminal={"method": "exit_multiple",
                                                         "exit_ev_ebitda": 12}), synthetic_data())
    check("an EV/EBITDA exit multiplies final-year EBITDA",
          near(ebitda.terminal_value, 12 * (last.ebit + last.da))
          and ebitda.exit_basis == "EBITDA", f"{ebitda.terminal_value}")
    md = valuation.model_outputs_markdown(ebitda)
    check("tables label the EBITDA exit",
          "| Exit EV/EBITDA | x | 12.0x |" in md and "WACC by exit EV/EBITDA |" in md, md[:400])
    check("sensitivity reprices on EBITDA",
          near(valuation.value_at(ebitda, ebitda.wacc, multiple=12), ebitda.per_share))
    both = valuation.compute(synthetic_model(terminal={"method": "perpetuity", "growth": 0.02,
                                                       "exit_ev_ebitda": 12}), synthetic_data())
    check("an EBITDA exit becomes the cross-check", both.cross_check_per_share is not None
          and near(both.per_share, base.per_share))
    try:
        valuation.compute(synthetic_model(terminal={"exit_ev_ebit": 10, "exit_ev_ebitda": 8}),
                          synthetic_data())
        check("two exit multiples refused", False)
    except valuation.ModelInputError as exc:
        check("two exit multiples refused", "not both" in str(exc))

    nee = valuation.compute(synthetic_model(terminal={"method": "perpetuity", "growth": 0.02,
                                                      "exit_ev_ebitda_multiple": 12}),
                            synthetic_data())
    check("an unread terminal key is reported, not ignored",
          any("exit_ev_ebitda_multiple" in w for w in nee.warnings), str(nee.warnings))
    check("a rationale in terminal is allowed", not valuation.compute(
        synthetic_model(terminal={"growth": 0.02, "rationale": "GDP"}), synthetic_data()).warnings)
    check("implied terminal EV/EBITDA exposed",
          pitch_mod._fill_placeholders("{{tv_ev_ebitda}}", valuation.placeholders(base))[0]
          == f"{base.terminal_value / (last.ebit + last.da):.1f}x")

    peers = [market_data.PeerMultiples(symbol=t, name=t, ev_ebitda=12.0, pe=None)
             for t in ("AAA", "BBB")]
    far = valuation.compute(synthetic_model(),
                            synthetic_data(price=1500.0, analyst_target=1900.0,
                                           ttm_ebitda=400.0), peers)
    check("the divergence warning names the terminal multiple against the peers",
          any("pays 7.7x FY2031 EBITDA against a peer median of 12.0x" in w
              for w in far.warnings), str(far.warnings))

    down = valuation.compute(synthetic_model(downside={"revenue_change": -0.2, "ebit_margin": 0.15,
                                                       "wacc": 0.11, "rationale": "x"}),
                             synthetic_data())
    check("downside case priced below the base",
          down.downside_per_share is not None and down.downside_per_share < down.per_share
          and not down.warnings, f"{down.downside_per_share} {down.warnings}")
    check("downside inputs described",
          down.downside_changes == ["revenue 20% below the base case in every forecast year",
                                    "EBIT margin 15.0% by FY2031", "WACC 11.0%"],
          str(down.downside_changes))
    check("the base case is untouched by the downside", near(down.per_share, base.per_share))
    md = valuation.scenarios_table(down)
    check("bear column in the scenarios table", "| Bear |" in md
          and "| Discount rate | % | 11.0% |" in md, md)
    vals = valuation.placeholders(down)
    text = (f"My downside case is an implied price of {vals['downside']}, "
            f"{vals['downside_upside']} downside.")
    check("the downside price and % in the prose are not contradictions",
          consistency.check(text, down) == [], str(consistency.check(text, down)))

    better = valuation.compute(synthetic_model(downside={"ebit_margin": 0.3}), synthetic_data())
    check("a downside above the base case is sent back",
          any("not below the base case" in w for w in better.warnings), str(better.warnings))
    no_risks = BODY[:BODY.index("## Risks and Downside Case")] + BODY[BODY.index("## Model Outputs"):]
    headline = json.loads(json.dumps(HEADER_OBJ))
    del headline["model"]["downside"]
    _, problems = pitch_mod._finalise(headline, no_risks, data)
    check("a pitch without the Risks section or a downside model is sent back",
          any("Risks and Downside Case" in x for x in problems)
          and any("distinct `bull` and `bear` scenario" in x for x in problems), str(problems))
    _, problems = pitch_mod._finalise(HEADER_OBJ, BODY, data)
    check("the full fixture passes those checks", problems == [], str(problems))

    for bad, wanted in (({"revenue_change": 0.1}, "must be a cut"),
                        ({"price": 10}, "does not read"),
                        ("worse", "must be an object")):
        v = valuation.compute(synthetic_model(downside=bad), synthetic_data())
        check(f"bad downside {bad!r} reported", any(wanted in w for w in v.warnings),
              str(v.warnings))

def test_shipped_2026_09_25(data):
    """What the AVGO, NEE, ANF, LHX and NFLX pitches of 2026-09-24/25 shipped with."""
    check("a rounded zero has no sign", valuation.pct(-0.13) == "0%" and valuation.pct(0.4) == "0%"
          and valuation.pct(-12.6) == "-13%" and valuation.pct(5) == "+5%")

    base = valuation.compute(synthetic_model(), synthetic_data())
    later = valuation.compute(synthetic_model(downside={"revenue_change": [0, 0, -0.2, -0.2, -0.2]}),
                              synthetic_data())
    check("a downside revenue cut can start in a later year",
          later.downside_per_share is not None and later.downside_per_share < base.per_share
          and later.downside_changes[0]
          == "revenue below the base case by 20% in FY2029, 20% in FY2030, 20% in FY2031",
          f"{later.downside_per_share} {later.downside_changes} {later.warnings}")
    short = valuation.compute(synthetic_model(downside={"revenue_change": [-0.1, -0.1]}),
                              synthetic_data())
    check("a per-year downside cut of the wrong length is reported",
          any("2 values for 5" in w for w in short.warnings), str(short.warnings))

    body = ("Intro.\n\n## Model Outputs\n\n### Definitions\n\nTAM text.\n\n"
            "The following tables are generated from the model inputs above.\n\n"
            "### Sensitivity Analysis\n\n| | | Terminal Growth | | |\n" + "| | | | |\n" * 40
            + "\n### Sensitivity (Intrinsic Price vs WACC & Exit Multiple)\n\n"
            "| WACC | 11.5x |\n| --- | --- |\n| 5.5% | -- |\n")
    out = pitch_mod._insert_tables(body, "### DCF Build\n\nGENERATED")
    check("typed sensitivity tables and bare pipes dropped from Model Outputs",
          "|" not in out and "Sensitivity" not in out and "TAM text." in out
          and out.rstrip().endswith("GENERATED"), out)

    peers = [market_data.PeerMultiples(symbol=t, name=t, ev_ebitda=12.0, pe=None)
             for t in ("AAA", "BBB")]
    v = valuation.compute(synthetic_model(), synthetic_data(ttm_ebitda=400.0), peers)
    pv = v.peers.value
    text = (f"Applying the median EV/EBITDA yields USD {pv:,.2f} per share "
            f"({valuation.pct((pv / v.price - 1) * 100)} upside).")
    check("the peer-implied upside is not read as the DCF's",
          consistency.check(text, v) == [], str(consistency.check(text, v)))
    trigger = f"I would change my recommendation to SELL if margins fall. Recommendation: {v.rating}."
    check("a recommendation-change trigger is not a contradiction",
          consistency.check(trigger, v) == [], str(consistency.check(trigger, v)))
    check("the rating is still checked outside a trigger",
          consistency.check("I rate the shares a SELL.", v) != [])

    wrong, right = ("below", "above") if v.per_share > pv else ("above", "below")
    said = f"The DCF sits {wrong} the peer multiple because the market prices near-term cyclicality."
    problems = consistency.check(said, v)
    check("a DCF-versus-peers comparison pointing the wrong way is caught",
          any("Reverse the comparison" in p for p in problems), str(problems))
    said = said.replace(wrong, right)
    check("the right direction passes", consistency.check(said, v) == [],
          str(consistency.check(said, v)))
    street = v.per_share * 1.5
    said = "My value is above the consensus target because I use a lower WACC."
    check("a DCF-versus-consensus comparison is checked against the target",
          any("consensus target" in p for p in consistency.check(said, v, street)))
    check("no target, no consensus comparison", consistency.check(said, v) == [])

    checked, problems = pitch_mod._finalise(HEADER_OBJ, BODY, data)
    rating = checked.valuation.rating
    other = next(r for r in ("BUY", "HOLD", "SELL") if r != rating)
    for view, flagged in ((rating, False), (other, True), ("", False)):
        headline = {**HEADER_OBJ, "view": view}
        _, problems = pitch_mod._finalise(headline, BODY, data)
        check(f"a `view` of {view or 'nothing'} against a computed {rating} "
              f"{'is' if flagged else 'is not'} sent back",
              any("your `view`" in p for p in problems) == flagged, str(problems))

    summary = pitch_mod._computed_summary(v, synthetic_data(analyst_target=street))
    check("the re-ask tells the model where its value landed",
          ("is below that" in summary or "is above that" in summary)
          and "lower than the Street's" in summary, summary)
    request = pitch_mod._fix_request([], computed=summary, issues=[
        {"section": "", "claim": "c", "problem": "p", "fix": ""}])
    check("the revision request carries the computed numbers and the reviewer's issue",
          summary in request and "Problem: p" in request and "has problems" not in request)


def test_shipped_2026_09_26():
    """What the NEE pitch of 2026-09-26 shipped with."""
    near = lambda a, b, tol=0.01: abs(a - b) <= tol
    exit_model = synthetic_model(terminal={"method": "exit_multiple", "exit_ev_ebitda": 20,
                                           "growth": 0.02},
                                 drivers={"Rate base": {"unit": "USD bn", "base": 55.0,
                                                        "values": [60, 65, 70, 75, 85.0]},
                                          "Backlog": {"unit": "GW", "base": 35.1,
                                                      "values": [38, 41, 44, 47, 50.0]}})
    v = valuation.compute(exit_model, synthetic_data(ttm_ebitda=400.0))
    last = v.rows[-1]
    g = v.implied_growth
    check("the growth an exit multiple implies prices the same terminal value",
          g is not None and near(last.fcf * (1 + g) / (v.wacc - g), v.terminal_value), str(g))
    check("an implied growth above the perpetuity cap is noted for the reviewer",
          g > valuation.MAX_TERMINAL_GROWTH
          and any("implies" in n and "growth forever" in n for n in v.notes), str(v.notes))
    bridge = valuation.intrinsic_valuation_markdown(v)
    tv_row = next(line for line in bridge.splitlines() if line.startswith("| Exit EV/"))
    check("the terminal value note names the implied growth, not the multiple twice",
          "implies" in tv_row and tv_row.count("EBITDA") == 1, tv_row)

    vals = valuation.placeholders(v)
    filled, unknown = pitch_mod._fill_placeholders(
        "From $55bn to {{driver.Rate base:FY2031}}; backlog {{driver.Backlog:FY2031}}, "
        "{{driver.Backlog:FY2030}} GW a year earlier; exit growth {{tv_implied_growth}}.", vals)
    check("driver placeholders carry their unit, once",
          filled.startswith("From $55bn to USD 85.0bn; backlog 50.0 GW, 47 GW a year earlier")
          and not unknown, filled)
    table = valuation.financials_markdown(v)
    backlog = next(l for l in table.split("\n") if l.startswith("| Backlog |"))
    check("drivers are key metrics with their own growth columns",
          backlog.count("%") >= 3 and "FY26-FY31" in table, table)
    # SPGI 2026-10-02: a growth driver at +7% over a revenue line that falls 5% (a spun-off
    # business still in the base). A forecast fall with no driver saying otherwise is fine.
    spun = synthetic_model(revenue=[950, 1016.5, 1087.7, 1163.8, 1245.3], drivers={
        "Organic revenue growth": {"unit": "%", "base": 6.9, "values": [7.0, 7.0, 7.0, 7.0, 7.0]}})
    warned = valuation.compute(spun, synthetic_data()).warnings
    check("a growth driver the revenue line contradicts is caught",
          any('driver "Organic revenue growth" is 7% for FY2027' in w for w in warned), str(warned))
    falling = synthetic_model(revenue=[920, 900, 910, 930, 950])
    check("a forecast fall with no driver against it is not",
          not any("driver" in w or "below the base" in w
                  for w in valuation.compute(falling, synthetic_data()).warnings))
    pro_forma = dict(spun, base_revenue=888, pro_forma_base={"reason": "Mobility spun off"})
    warned = valuation.compute(pro_forma, synthetic_data()).warnings
    check("a declared pro-forma base is the base the checks use",
          not any("does not match" in w or 'driver "Organic' in w for w in warned), str(warned))
    # SPGI's second facts-mode rewrite based on FY2026 "per guidance": a pro-forma base
    # restates a reported year.
    block = ("### Income statement (values in millions)\n\n| Line item | 2026-12-31 | 2025-12-31 |\n"
             "| --- | --- | --- |\n| Total Revenue | 1,000 | 900 |\n")
    unreported = dict(pro_forma, base_year="FY2027",
                      years=["FY2028", "FY2029", "FY2030", "FY2031", "FY2032"])
    warned = valuation.compute(unreported, synthetic_data(prompt_block=block)).warnings
    check("a pro-forma base in a year not yet reported is caught",
          any("FY2027 is not reported yet (the latest is FY2026)" in w for w in warned),
          str(warned))
    warned = valuation.compute(pro_forma, synthetic_data(prompt_block=block)).warnings
    check("a pro-forma base in the latest reported year passes",
          not any("not reported yet" in w for w in warned), str(warned))
    pct = valuation.compute({**v_model_for_pct_driver()}, synthetic_data())
    row = next(l for l in valuation.financials_markdown(pct).split("\n") if l.startswith("| Margin"))
    check("a percentage driver moves in points, not a CAGR",
          row.split("|")[-2].strip().endswith("pts"), row)

    market = v.current_ev_ebitda
    check("today's EV/EBITDA from price, shares, net debt and trailing EBITDA",
          near(market, (250.0 * 1e7 / 1e6 + 200.0 - 500.0) / 400.0), str(market))
    check("today's multiple is a placeholder", vals["ev_ebitda"] == f"{market:.1f}x")
    wrong = consistency.check("Intro.", v, cover="Fully valued at 19x EV/EBITDA on 8% growth.")
    check("a wrong trading multiple on the cover is caught",
          any("trades at" in p for p in wrong), str(wrong))
    quoted = valuation.compute(exit_model, synthetic_data(ttm_ebitda=400.0,
                                                          quoted_ev_ebitda=19.0))
    check("Yahoo's own EV/EBITDA from the market data block is not an error",
          consistency.check("Intro.", quoted, cover="Fully valued at 19x EV/EBITDA.") == [],
          str(consistency.check("Intro.", quoted, cover="Fully valued at 19x EV/EBITDA.")))
    for fine in (f"The stock trades at {market:.1f}x EV/EBITDA.",
                 "My exit is valued at 19x EV/EBITDA.",
                 "Peers trade at 19x EV/EBITDA."):
        check(f"not a contradiction: {fine!r}", consistency.check(fine, v) == [],
              str(consistency.check(fine, v)))

    text = (f"FCF margin reaches ~12% by {last.label} (USD 8.7bn FCF on USD 38.4bn revenue).")
    wrong = wrong_figures(text, v, synthetic_data(ttm_ebitda=400.0))
    check("an FCF margin is checked in a sentence that also names revenue",
          ("12%", "year") in wrong, str(wrong))
    right = text.replace("~12%", f"~{last.fcf_margin * 100:.0f}%")
    check("the right FCF margin there passes",
          ("12%", "year") not in wrong_figures(right, v, synthetic_data(ttm_ebitda=400.0)),
          str(wrong_figures(right, v, synthetic_data(ttm_ebitda=400.0))))
    mine = text.replace("FCF margin reaches ~12%", "FCF margin reaches my estimate of ~12%")
    check("calling a contradicted figure 'my estimate' does not reconcile it",
          ("12%", "year") in wrong_figures(mine, v, synthetic_data(ttm_ebitda=400.0)))

    summary = pitch_mod._computed_summary(v, synthetic_data(ttm_ebitda=400.0))
    check("the writer is told today's multiple and the implied growth",
          "trailing EBITDA today" in summary and "growth forever" in summary, summary)


def _norm(label):
    return valuation._norm_year(label)


def test_provenance(data):
    """A figure passes only when a source the writer was shown accounts for it."""
    got = {f.text: (f.kind, round(f.value, 6), f.approx) for f in provenance.figures(
        "Fully valued at 19x EV/EBITDA; FCF of $8.7bn, a ~12% margin, 18–20% share, a 50bps "
        "cut, +90k customers, 3.6 GW added, $12–13bn capex, per the 10-K for FY2030 and "
        "through 2032 and 9%+, 0.8138 shares, 35 of 67 counties, S&P 500.")}
    check("figures take their kind and value from how they are written",
          got.get("19x") == ("x", 19.0, False) and got.get("$8.7bn") == ("money", 8.7e9, False)
          and got.get("12%") == ("%", 12.0, True) and got.get("18") == ("%", 18.0, False)
          and got.get("50bps") == ("pp", 0.5, False) and got.get("90k") == ("count", 9e4, False)
          and got.get("3.6 GW") == ("W", 3.6e9, False) and got.get("$12") == ("money", 12e9, False)
          and got.get("0.8138") == ("bare", 0.8138, False), str(got))
    check("years, filings, counts and names are not figures",
          not {"2030", "2032", "10", "35", "67", "500"} & set(got), str(sorted(got)))

    pool = provenance.Pool()
    pool.add_model({"fcf": [8661, 9000]})
    pool.add_text("Today the market pays 15.0x trailing EBITDA.")
    pool.add_notes("FPL added more than 90,000 customers in the quarter. Revenue grew 31% "
                   "on the year. Analysts see 19.2 of upside in the stock.")
    def flagged(sentence):
        return [f.text for f, *_ in provenance.unsourced(sentence, pool)]
    check("a computed amount matches however it is rounded",
          flagged("FCF reaches USD 8.7bn, or $8,661mn.") == [])
    check("a multiple matches only a multiple", flagged("It trades at 15.0x EBITDA.") == []
          and flagged("It is fully valued at 19x EV/EBITDA.") == ["19x"])
    check("a note's figure counts only near the same words",
          flagged("FPL added >90,000 customers.") == []
          and flagged("The FCF margin reaches 31% by FY2030.") == ["31%"]
          and flagged("Revenue rose 31%.") == [])
    check("the writer's own estimate needs no source, unless it is a multiple",
          flagged("I assume a 50bps ROE cut, worth ~$425mn a year.") == []
          and flagged("A 50bps ROE cut is worth ~$425mn a year.") == ["50bps", "$425mn"]
          and flagged("I assume consensus implies 18x EBITDA.") == ["18x"])

    # Replayed from NEE's local run of 2026-09-27, whose model had FY2030 revenue of
    # $39.2bn and EBITDA of $22.2bn, and which shipped with no check flagged.
    nee = valuation.compute(synthetic_model(), synthetic_data(ttm_ebitda=400.0))
    last = nee.rows[-1]
    year = last.label
    npool = provenance.build_pool(
        [], ["Free Cash Flow | 3,211 | 4,746\nEBITDA | 16,043 | 14,033 | 16,763 | 9,207\n"
             "the settlement sets base rates through 2029 and a 10.6% midpoint ROE; "
             "own 110 gigawatts (GW) of generation"],
        nee, synthetic_data(ttm_ebitda=400.0), {})
    def nflag(sentence):
        return [(f.text, y) for f, _, y in provenance.unsourced(sentence, npool)]
    wrong_revenue = f"${last.revenue * 0.75 / 1e3:.2f}bn"
    check("a figure written against a forecast year must be the model's for that year",
          nflag(f"Even at {year}, {wrong_revenue} revenue.") == [(wrong_revenue, _norm(year))],
          str(nflag(f"Even at {year}, {wrong_revenue} revenue.")))
    right = f"${last.revenue / 1e3:.2f}bn"
    check("the model's own figure for that year passes, in any phrasing",
          nflag(f"Even at {year}, {right} revenue.") == []
          and nflag(f"{year} EBITDA (${last.ebitda / 1e3:.2f}bn) grows.") == [])
    check("a margin written against a year is the model's margin",
          nflag(f"EBITDA margin rises to 52% ({year}).") != []
          and nflag(f"EBITDA margin rises to {last.ebitda / last.revenue * 100:.1f}% ({year}).") == [])
    check("an 'I project' does not excuse a figure the model contradicts",
          nflag(f"I project {year} revenue of {wrong_revenue}.") != [])
    check("figures that agree only with each other are not worked out",
          {"$12.9bn", "$181bn"} <= {t for t, _ in nflag(
              "The terminal value at 10x EBITDA ($12.9bn) = $181bn EV.")})
    check("statements say Free Cash Flow where prose says FCF",
          nflag("FY2025 FCF was $3.2bn.") == [])
    check("a quarter is not a fiscal year, and 'with a' does not bind a rate to a year",
          nflag("Q2 2027: 3,211 of free cash flow.") == []
          and nflag("Rates are set through 2029 with a 10.6% midpoint ROE.") == [])
    check("units spelled out in the notes", nflag("It owns 110 GW of generation.") == [])
    check("declared assumptions, per-unit illustrations and checkable sums pass",
          nflag("Forecast assumes 7 GW/year of net additions.") == []
          and nflag("Each 1 GW delay matters.") == []
          and nflag("Interest on $3,211mn at 10% = $321mn a year.") == [])

    # The whole pipeline: a typed figure is sent back, a sourced one is not.
    digest = BODY
    _, problems = pitch_mod._finalise(HEADER_OBJ, BODY, data, digest=digest)
    check("a pitch whose figures are all in its notes passes",
          not any("come from no placeholder" in p for p in problems), str(problems))
    typed = BODY.replace("## Investment Thesis",
                         "A cut in the allowed return costs ~$427mn a year.\n\n"
                         "## Investment Thesis", 1)
    result, problems = pitch_mod._finalise(HEADER_OBJ, typed, data, digest=digest)
    check("a figure from nowhere is not sent back for a rewrite",
          not any("$427mn" in p for p in problems), str(problems))
    pitch_mod._repair(result)
    check("it is marked with a dagger instead",
          "~$427mn† a year" in result.body and result.unverified >= 1,
          str(result.unverified))
    wrong = typed.replace("A cut in the allowed return costs ~$427mn a year.",
                          "FCF margin reaches 3% by FY2030. A cut costs ~$427mn a year.")
    result, problems = pitch_mod._finalise(HEADER_OBJ, wrong, data, digest=digest)
    check("a figure that contradicts the model for its year is sent back",
          any("given for a forecast year" in p and '"3%"' in p for p in problems), str(problems))
    pitch_mod._repair(result)
    check("and if it survives, its sentence is deleted and listed with the reason",
          "FCF margin reaches 3% by FY2030." not in result.body
          and result.removed and "FY2030" in result.removed[0][1]
          and not any("given for a forecast year" in c for c in result.checks),
          str(result.removed))
    check("no notes given, no provenance check (the offline fixtures)",
          not any("come from no placeholder" in p
                  for p in pitch_mod._finalise(HEADER_OBJ, typed, data)[1]))

    saved = replay.record(data, digest, {("AAA",): [market_data.PeerMultiples(
        symbol="AAA", name="A", ev_ebitda=12.0, pe=20.0)]}, [HEADER + BODY])
    with tempfile.TemporaryDirectory() as tmp:
        back = replay.load(replay.save(saved, Path(tmp)))
    check("a replay file round-trips the market data, peers and writes",
          back["data"] == data and back["peers"]["AAA"][0].pe == 20.0
          and back["writes"] == [HEADER + BODY], str(back["peers"]))
    check("a replay reruns today's checks on each saved write",
          [label for label, _ in replay.check(back)] == ["write 1"])


SHEET_NOTES = (
    "## [PRIMARY] filing_tables: latest earnings release (8-K exhibit 99), filed 2026-07-28\n\n"
    "### Statements of Income Three months ended June 30, 2026 and 2025 (dollars in millions)\n"
    "- Revenue — Three Months 2026: USD 4,146mn; Three Months 2025: USD 3,755mn\n"
    "- Operating profit — Three Months 2026: USD 1,812mn; Three Months 2025: USD 1,551mn\n\n"
    "### Operating Results by Segment Three months ended June 30, 2026 and 2025 (dollars in "
    "millions)\n"
    "- Ratings — Three Months Revenue 2026: USD 1,339mn; Three Months Revenue 2025: USD 1,148mn\n"
    "- Indices — Three Months Revenue 2026: USD 534mn; Three Months Revenue 2025: USD 446mn\n"
    "- Market Intelligence — Three Months Revenue 2026: USD 2,273mn; Three Months Revenue 2025: "
    "USD 2,161mn\n"
    "- Total revenue — Three Months Revenue 2026: USD 4,146mn; Three Months Revenue 2025: USD 3,755mn\n"
    "- Ratings (a) — Three Months Revenue 2026: USD 913mn; Three Months Revenue 2025: USD 780mn\n\n"
    "### Full-Year 2026 Outlook (in millions)\n"
    "- Revenue growth — GAAP: 5.9% to 7.9%; Adjusted: 5.9% to 7.9%\n\n"
    "## [PRIMARY] fetch_page: https://www.sec.gov/x/10q.htm\n\n[latest 10-Q, filed 2026-07-28]\n"
    "Ratings grew on refinancing.")
SHEET_MARKET = ("## Annual financials\n\n### Income statement (values in millions)\n\n"
                "| Line item | 2025-12-31 | 2024-12-31 |\n| --- | --- | --- |\n"
                "| Total Revenue | 15,336 | 14,208 |\n| Operating Income | 6,177 | 5,478 |\n")


def test_figure_sheet_and_outline(data):
    """The figure sheet names the figures an analyst writes with, computed by code; the
    outline is checked before any prose (2026-10-04)."""
    fx = figure_sheet.attach(facts.build(SHEET_NOTES, SHEET_MARKET), data)
    sheet = fx.sheet.figures
    check("the sheet names the latest quarter, its growth and margin",
          sheet["revenue_2Q26"].text == "USD 4,146mn" and sheet["revenue_growth_2Q26"].text == "10.4%"
          and sheet["operating_margin_2Q26"].text == "43.7%", str(list(sheet)))
    check("the sheet gives each segment's revenue, growth and share, and stops at the "
          "footnoted block",
          sheet["ratings_revenue_growth_2Q26"].text == "16.6%"
          and sheet["ratings_share_of_revenue_2Q26"].text == "32%"
          and "market_intelligence_revenue_2Q26" in sheet
          and not any("913" in f.text for f in sheet.values()), str(list(sheet)))
    check("the annual years come with growth and margin, and guidance as a range, once",
          sheet["revenue_growth_FY2025"].text == "7.9%" and sheet["operating_margin_FY2025"].text == "40.3%"
          and sheet["guidance_revenue_growth_FY2026"].text == "5.9% to 7.9%"
          and not any(n.startswith("guidance_revenue_growth_FY2026_") for n in sheet), str(list(sheet)))
    check("every sheet figure carries its period and source",
          sheet["ratings_revenue_2Q26"].period_key == ("3M", "2026-06")
          and sheet["ratings_revenue_growth_2Q26"].source.startswith("computed from earnings release"))
    check("the writer is shown the sheet, by name and value",
          "`{{ratings_revenue_growth_2Q26}}` Ratings revenue growth" in fx.sheet.render())
    filled, problems = facts.fill("Ratings grew {{ratings_revenue_growth_2Q26}}, to "
                                  "{{share:ratings_revenue_2Q26/revenue_2Q26}} of revenue, "
                                  "against {{wacc}}.", fx, {})
    check("a sheet name prints its value and source; a model placeholder is left for its filler",
          filled.startswith("Ratings grew 16.6%, to 32.3% (computed from earnings release")
          and "{{wacc}}" in filled and problems == [], filled + str(problems))
    pct_of_pct, why = facts.fill("Together {{share:ratings_share_of_revenue_2Q26/"
                                 "indices_share_of_revenue_2Q26}}.", fx, {})
    check("a share of two percentages is refused, not printed as 246%",
          "n.a." in pct_of_pct and "percentages already" in " ".join(why), pct_of_pct + str(why))
    check("a computation from a computed figure says 'computed from' once",
          "computed from computed" not in facts.fill(
              "{{ratio:fcf_FY2025/revenue_FY2025}}", fx, {})[0] if "fcf_FY2025" in sheet else True)
    adopted = facts.adopt_typed("In 2Q26 Ratings revenue was USD 1,339mn.", fx, {})
    check("a typed figure that is a sheet figure is cited by its name",
          "{{ratings_revenue_2Q26}}" in adopted, adopted)
    # A segment table that does not add up to the quarter's revenue is left out (NKE).
    partial = SHEET_NOTES.replace("USD 2,273mn; Three Months Revenue 2025: USD 2,161mn",
                                  "USD 273mn; Three Months Revenue 2025: USD 161mn").replace(
        "- Total revenue — Three Months Revenue 2026: USD 4,146mn; Three Months Revenue 2025: "
        "USD 3,755mn\n", "")
    odd = figure_sheet.build(facts.build(partial, SHEET_MARKET), data)
    check("segments that do not add up to the quarter's revenue are left out",
          not any(n.startswith("ratings_") for n in odd.figures), str(list(odd.figures)))

    model_names = {"ebit_margin:fy2030", "implied_cagr", "downside"}

    def is_model(name: str) -> bool:
        return name.lower() in model_names

    good = {"outline": [], "assumptions": [{"id": "price_rise", "value": "3-4%",
                                             "basis": "latest 10-Q: list prices have outrun inflation for five years with no churn response, and most contracts reprice every year"}]}
    claims = [
        ("Ratings is accelerating on a refinancing wave in 2Q26",
         ["ratings_revenue_growth_2Q26", "ratings_share_of_revenue_2Q26"]),
        ("Management guides to faster growth this year", ["guidance_revenue_growth_FY2026"]),
        ("Margins rise as pricing holds", ["ebit_margin:FY2030", "assume:price_rise"]),
        ("Moody's is the only rival of scale", ["implied_cagr"]),
    ]
    for s_index, section in enumerate(outline_mod.SECTIONS):
        # A single figure is a fact; two are enough for an inference (the skill's rule).
        points = [{"id": f"S{s_index + 1}P{p_index + 1}", "claim": claim,
                   "figures": figures, "depends_on": [],
                   "kind": "inference" if len(figures) > 1 else "fact",
                   "confidence": "medium"}
                  for p_index, (claim, figures) in enumerate(claims)]
        if section.lower() in ("investment thesis", "risks and downside case",
                               "revenue growth and margins"):
            for point in points:
                point.update(counterargument="Growth could slow if issuance weakens.",
                             counter_figures=["ratings_revenue_growth_2Q26"],
                             signpost="Quarterly issuance will show whether momentum persists.",
                             signpost_figures=["implied_cagr"],
                             distinguishing_evidence="Refinancing holding up while new issuance "
                                                     "stays flat would favour this view")
        if section.lower() == "conclusions":
            for point in points[2:]:
                point.update(kind="conclusion",
                             depends_on=[f"S{s_index + 1}P1", f"S{s_index + 1}P2"])
        good["outline"].append({"section": section, "points": points})
    problems, kept = outline_mod.validate(good, fx, is_model, {})
    check("a clean outline passes, its assumption kept", problems == [] and "price_rise" in kept,
          str(problems))
    bad = json.loads(json.dumps(good))
    bad["outline"][0]["points"] = [
        {"claim": "Ratings grew 17% on refinancing", "figures": []},
        {"claim": "Ratings revenue in 4Q25", "figures": ["ratings_revenue_2Q26"]},
        {"claim": "Revenue will grow", "figures": ["guidance_revenue_growth_FY2026"]},
        {"claim": "Peers trade higher", "figures": ["peer_multiple_2Q26"]}]
    problems, _ = outline_mod.validate(bad, fx, is_model, {})
    joined = " ".join(problems)
    check("an outline with a typed number, a figure for another period, guidance given as a "
          "result and a name that does not exist is sent back for each",
          "typed numbers" in joined and "17%" in joined and "peer_multiple_2Q26" in joined
          and "the sentence names the 3 months to 2025-12" in joined and "is guidance" in joined,
          joined[:900])
    rendered = outline_mod.render(good)
    check("the checked outline is shown to the prose step with its figures",
          "**Opening**" in rendered and "[{{ratings_revenue_growth_2Q26}}, "
          "{{ratings_share_of_revenue_2Q26}}]" in rendered, rendered[:300])


async def test_outline_step(data):
    """Facts mode with the two-pass write: the model and its outline in one call, then the
    prose. The outline had a call of its own until 2026-10-05."""
    outline = {"outline": [
        {"section": s, "points": [{"claim": "Ratings is accelerating in 2Q26",
                                   "figures": ["ratings_revenue_growth_2Q26"],
                                   "kind": "fact", "confidence": "high"}]}
        for s in outline_mod.SECTIONS],
        "assumptions": [{"id": "price_rise", "value": "3-4%",
                         "basis": "latest 10-Q: list prices have outrun inflation for five years with no churn response, and most contracts reprice every year"}]}

    def plan(**changes) -> str:
        obj = {**json.loads(json.dumps(HEADER_OBJ)), **json.loads(json.dumps(outline)), **changes}
        return "```json\n" + json.dumps(obj, indent=1) + "\n```\n"

    def scripted(*texts):
        return FakeClient([[delta(content=t), final_chunk()] for t in texts])

    def prose_ask(client) -> str:
        """The prose request: after the plan, and its re-ask when the outline had problems."""
        return next(c["messages"][-1]["content"] for c in client.completions.calls
                    if "Step 2 of 2" in c["messages"][-1]["content"])

    def prose_ask(client) -> str:
        """The prose request: after the plan, and its re-ask when the outline had problems."""
        return next(c["messages"][-1]["content"] for c in client.completions.calls
                    if "Step 2 of 2" in c["messages"][-1]["content"])

    config.TWO_PASS_WRITE, config.FACTS_MODE = True, True
    try:
        client = scripted(plan(), plan(), *[HEADER + BODY] * pitch_mod.WRITE_ATTEMPTS)
        result = await pitch_mod.write(data, SHEET_NOTES, [], client)
        calls = client.completions.calls
        ask = calls[0]["messages"][-1]["content"]
        check("facts mode: the model and the outline are asked for in one call",
              "Step 1 of 2" in ask and "The outline, in the same header" in ask, ask[-600:])
        system = calls[0]["messages"][0]["content"]
        user = calls[0]["messages"][1]["content"]
        check("facts mode: the writer gets the facts prompt, the sheet and a masked exemplar",
              "You are the analyst, not the calculator" in system and "{{…}}" in system
              and "# Figure sheet" in user and "[#F" not in user, user[:300])
        prose = prose_ask(client)
        check("facts mode: the prose is asked for next, from the checked outline and the "
              "computed valuation",
              "Step 2 of 2" in prose and "{{ratings_revenue_growth_2Q26}}" in prose
              and "The code valued your model" in prose and "without the\n`outline` field" in prose,
              prose[:600])
        check("facts mode: the outline's assumptions reach the pitch, and the outline the replay",
              "price rise" in result.body and result.replay.get("outline")
              and result.replay["outline"]["assumptions"][0]["id"] == "price_rise",
              result.body[-600:])

        bad = json.loads(json.dumps(outline["outline"]))
        bad[0]["points"][0]["claim"] = "Ratings grew 17%"
        client = scripted(plan(outline=bad), plan(outline=bad), *[HEADER + BODY] * pitch_mod.WRITE_ATTEMPTS)
        await pitch_mod.write(data, SHEET_NOTES, [], client)
        prose = prose_ask(client)
        replan = client.completions.calls[1]["messages"][-1]["content"]
        check("facts mode: an outline's problems are re-asked with the model, and those left "
              "go to the prose request",
              "That model has problems" in replan and "17%" in replan
              and "Step 2 of 2" in prose and "typed numbers" in prose and "17%" in prose
              and "fix them in the pitch" in prose, replan[-600:] + prose[-600:])

        client = scripted(HEADER, HEADER, *[HEADER + BODY] * pitch_mod.WRITE_ATTEMPTS)
        await pitch_mod.write(data, SHEET_NOTES, [], client)
        prose = prose_ask(client)
        check("facts mode: a header without an outline gets the plain prose request",
              "Step 2 of 2" in prose and "pitch for a" in prose, prose[-400:])
    finally:
        config.TWO_PASS_WRITE, config.FACTS_MODE = False, False


async def test_two_pass_write(data):
    """The model is valued first and the prose written with the computed numbers in hand."""
    def scripted(*write_texts):
        return FakeClient([[delta(content="DONE"), final_chunk()]]
                          + [[delta(content=t), final_chunk()] for t in write_texts])

    def header(**changes) -> str:
        obj = json.loads(json.dumps(HEADER_OBJ))
        for key, value in changes.items():
            if value is None:
                obj.pop(key, None)
            else:
                obj[key] = value
        return "```json\n" + json.dumps(obj, indent=1) + "\n```\n"

    saved_two_pass, saved_outline_step = config.TWO_PASS_WRITE, config.OUTLINE_STEP
    config.TWO_PASS_WRITE, config.OUTLINE_STEP = True, False
    try:
        client = scripted(HEADER, HEADER + BODY)
        result = await pitch_mod.generate(data, client)
        calls = client.completions.calls
        check("two-pass: research, the model, then the pitch", len(calls) == 3, str(len(calls)))
        plan_ask = calls[1]["messages"][-1]["content"]
        check("two-pass: the first write asks for the model alone",
              "Step 1 of 2" in plan_ask and "nothing after it" in plan_ask)
        prose = calls[2]["messages"]
        v = result.valuation
        check("two-pass: the prose is asked for with the computed numbers",
              len(prose) == 4 and prose[2]["role"] == "assistant" and v is not None
              and "Step 2 of 2" in prose[3]["content"]
              and f"`{{{{intrinsic}}}}` = {v.currency} {v.recommendation_value:,.2f}" in prose[3]["content"]
              and f"pitch for a {v.rating}" in prose[3]["content"], prose[-1]["content"][:600])
        check("two-pass: a clean pitch ships clean", result.checks == [], str(result.checks))
        check("two-pass: no turn is asked for when the view matches",
              "You expected" not in prose[3]["content"])

        other = next(r for r in ("BUY", "HOLD", "SELL") if r != v.rating)
        client = scripted(header(view=other), HEADER + BODY)
        await pitch_mod.generate(data, client)
        ask = client.completions.calls[2]["messages"][-1]["content"]
        check("two-pass: a view the inputs do not support is turned before the prose",
              f"You expected {other}; your inputs compute {v.rating}" in ask, ask[:400])

        no_model = header(model=None)
        client = scripted(no_model, HEADER, HEADER + BODY)
        result = await pitch_mod.generate(data, client)
        calls = client.completions.calls
        check("two-pass: an uncomputable model is re-asked as a model",
              len(calls) == 4 and "could not be computed" in calls[2]["messages"][-1]["content"]
              and "header block again" in calls[2]["messages"][-1]["content"]
              and "Step 2 of 2" in calls[3]["messages"][-1]["content"]
              and result.valuation is not None, str(len(calls)))

        client = scripted(no_model, no_model, HEADER + BODY)
        result = await pitch_mod.generate(data, client)
        calls = client.completions.calls
        check("two-pass: no computable model falls back to one pass",
              len(calls) == 4 and len(calls[3]["messages"]) == 2
              and "Step 1" not in calls[3]["messages"][1]["content"]
              and result.valuation is not None and result.checks == [], str(result.checks))

        worse = json.loads(json.dumps(HEADER_OBJ["model"]))
        worse["downside"] = {"ebit_margin": 0.6}
        warned = header(model=worse)
        client = scripted(warned, HEADER, HEADER + BODY)
        await pitch_mod.generate(data, client)
        calls = client.completions.calls
        ask = calls[2]["messages"][-1]["content"]
        check("two-pass: a scenario order violation is sent back during planning",
              len(calls) == 4 and "not below the base case" in ask
              and "Step 2 of 2" in calls[3]["messages"][-1]["content"], ask[:500])

        wrong = HEADER + BODY.replace(
            "Free cash flow reaches",
            "I arrive at an intrinsic price of US$9,999.00. Free cash flow reaches")
        patch = json.dumps({"patches": [{"section": "Intrinsic Valuation",
                                         "starts_with": "Discounting at",
                                         "replacement": "Discounting at {{wacc}} gives an enterprise "
                                                       "value of {{ev}} and an implied share price "
                                                       "of {{intrinsic}}, {{upside}} from here. Free "
                                                       "cash flow reaches {{fcf:FY2030}} by FY2030."}]})
        client = scripted(HEADER, wrong, patch)
        result = await pitch_mod.generate(data, client)
        calls = client.completions.calls
        reask = calls[3]["messages"]
        check("two-pass: a paragraph patch keeps the computed-numbers conversation",
              len(calls) == 4 and len(reask) == 6 and "Step 2 of 2" in reask[3]["content"]
              and "9,999" in reask[-1]["content"] and result.checks == [], str(result.checks))
    finally:
        config.TWO_PASS_WRITE, config.OUTLINE_STEP = saved_two_pass, saved_outline_step


async def test_write_checks(data):
    """Contradictions are sent back, then shipped flagged rather than hidden."""
    def scripted(*write_texts):
        return FakeClient([[delta(content="DONE"), final_chunk()]]
                          + [[delta(content=t), final_chunk()] for t in write_texts])

    wrong = HEADER + BODY.replace(
        "Free cash flow reaches",
        "I arrive at an intrinsic price of US$9,999.00. Free cash flow reaches")
    patch = json.dumps({"patches": [{"section": "Intrinsic Valuation",
                                     "starts_with": "Discounting at",
                                     "replacement": "Discounting at {{wacc}} gives an enterprise "
                                                   "value of {{ev}} and an implied share price "
                                                   "of {{intrinsic}}, {{upside}} from here. Free "
                                                   "cash flow reaches {{fcf:FY2030}} by FY2030."}]})
    client = scripted(wrong, patch)
    result = await pitch_mod.generate(data, client)
    reask = client.completions.calls[2]["messages"]
    check("contradiction triggers a paragraph patch", len(client.completions.calls) == 3)
    check("patch request names the contradiction and locator",
          "9,999" in reask[-1]["content"] and "starts_with" in reask[-1]["content"])
    check("patch request carries the full first draft",
          reask[-2]["role"] == "assistant" and "Model Outputs" in reask[-2]["content"])
    check("fixed rewrite ships clean", result.checks == [], str(result.checks))

    client = scripted(*[wrong] * pitch_mod.WRITE_ATTEMPTS)
    result = await pitch_mod.generate(data, client)
    check("at most two full drafts before shipping flagged",
          len(client.completions.calls) == 1 + pitch_mod.WRITE_ATTEMPTS,
          str(len(client.completions.calls)))
    # A wrong intrinsic price is a headline claim: delivery deletes its sentence and
    # lists it with the reason, so the check is resolved rather than left open.
    check("unfixed price contradiction deleted, not shipped",
          "9,999" not in result.body and result.checks == []
          and any("9,999" in s for s, _ in result.removed), str(result.checks))
    card = tg_format.summary_card(result, data)
    check("card has no open issue for a deleted sentence", "open issue" not in card,
          card[-400:])
    path = pdf_render.build(result, data)
    check("flagged pitch still renders", path.exists() and path.stat().st_size > 3000)


    no_model = json.dumps({k: v for k, v in HEADER_OBJ.items() if k != "model"})
    typed = ("```json\n" + no_model[:-1]
             + ', "intrinsic_price": 710, "recommendation": "BUY"}\n```\n' + BODY)
    result = await pitch_mod.generate(data, scripted(*[typed] * pitch_mod.WRITE_ATTEMPTS))
    check("uncomputable model never shows the model's own call",
          result.valuation is None and result.get("recommendation") is None
          and result.get("intrinsic_price") is None and "{{" not in result.body,
          str(result.headline))
    check("uncomputable model flagged", any("could not be computed" in c for c in result.checks))
    card = tg_format.summary_card(result, data)
    check("card survives a missing rating", "Rating" in card)

    client = scripted(typed, HEADER + BODY)
    result = await pitch_mod.generate(data, client)
    check("a computable rewrite beats an uncomputable first draft",
          result.valuation is not None and result.checks == [])


async def test_primary_floor(data):
    """Research is not done until it has read the company's own numbers."""
    monkey: dict = {}
    original = patch_tools(monkey)
    config.MIN_PRIMARY_FETCHES = 1
    try:
        search = [tool_delta(0, "c1", "web_search", '{"query": "Spotify Q2 2026"}'),
                  final_chunk("tool_calls")]
        filing = [tool_delta(0, "c2", "fetch_page",
                             '{"url": "https://www.sec.gov/Archives/spot-6k.htm"}'),
                  final_chunk("tool_calls")]
        done = [delta(content="DONE"), final_chunk()]
        write = [delta(content=HEADER + BODY), final_chunk()]

        client = FakeClient([search, done, filing, done, write])
        await pitch_mod.generate(data, client)
        research = client.completions.calls[0]["messages"]
        nudges = [m["content"] for m in research if m["role"] == "user"][1:]
        check("no filing read means research is sent back for one",
              len(nudges) == 1 and "10-Q/10-K" in nudges[0] and len(client.completions.calls) == 5,
              f"{len(client.completions.calls)} calls")

        client = FakeClient([done] * (pitch_mod.MAX_RESEARCH_NUDGES + 1) + [write])
        result = await pitch_mod.generate(data, client)
        check("the filings nudge is bounded too",
              len(client.completions.calls) == pitch_mod.MAX_RESEARCH_NUDGES + 2
              and result.get("ticker") == "SPOT")
    finally:
        config.MIN_PRIMARY_FETCHES = 0
        research_tools.dispatch = original

    no_consensus = HEADER + BODY.replace("**Versus consensus.**", "Separately,")
    patch = json.dumps({"patches": [{"section": "Intrinsic Valuation",
                                     "starts_with": "Separately,",
                                     "replacement": "**Versus consensus.** The Street's mean "
                                                   "target is {{consensus_target}} and implies "
                                                   "{{consensus_upside}}; my value sits "
                                                   "{{vs_consensus}} from it."}]})
    client = FakeClient([[delta(content="DONE"), final_chunk()],
                         [delta(content=no_consensus), final_chunk()],
                         [delta(content=patch), final_chunk()]])
    result = await pitch_mod.generate(data, client)
    reask = client.completions.calls[2]["messages"][-1]["content"]
    check("a pitch that ignores the Street is sent back",
          "Versus consensus" in reask and result.checks == [], reask[:200])


def test_v1_prompt_repairs():
    digest = ("## [PRIMARY] sec_xbrl: SEC company facts\n"
              "[latest 10-Q, MD&A]\nhttps://www.sec.gov/Archives/edgar/data/1/q.htm\n"
              "## [PRIMARY] fetch_page: https://sec.gov/earnings.htm\n"
              "[RTX Q2 earnings release]\n## [SECONDARY] fetch_page: "
              "https://www.hudson-labs.com/peers\n"
              "# RTX Competitors: RTX Top Peers in 2026 | Hudson Labs\n"
              "https://www.hudson-labs.com/peers\n"
              "## [SECONDARY] fetch_page: https://deloitte.com/outlook\n"
              "# 2026 Aerospace and Defense Industry Outlook | Deloitte Insights\n")
    check("source matching normalizes case, punctuation and whitespace",
          outline_mod.source_matches(" latest 10-Q MD&A ", digest))
    check("source matching accepts block kinds, URL domains, titles and market data",
          all(outline_mod.source_matches(x, digest) for x in (
              "sec_xbrl", "latest earnings release", "www.hudson-labs.com",
              "2026 Aerospace and Defense Industry Outlook",
              "market data block")))
    check("source matching rejects short fragments, irrelevant Notes, and blank values",
          not outline_mod.source_matches("x", digest + "\n## Notes\nhttps://rtx.com")
          and not outline_mod.source_matches("random blog notes", digest + "\n## Notes\nhttps://rtx.com")
          and not outline_mod.source_matches("", digest))
    check("source matching accepts exact page title and digest URL",
          outline_mod.source_matches("RTX Competitors: RTX Top Peers in 2026 | Hudson Labs", digest)
          and outline_mod.source_matches("https://www.hudson-labs.com/peers", digest))
    check("unmatched citation remains a warning",
          not outline_mod.source_matches("invented research", digest))
    check("Conclusions is a required V1 outline section",
          outline_mod.SECTIONS[-2:] == ["Conclusions", "Model Outputs"])

    invalid = {"outline": [{"section": "Opening", "points": [{
        "id": "O1", "claim": "Revenue and margins support the model", "evidence": ["market data"],
        "figures": ["bull_intrisic", "implied_carg", "bear_intrisic"]}]}]}
    names = ["bull_intrinsic", "implied_cagr", "bear_intrinsic"]
    problems = outline_mod.validate_structure(invalid, lambda n: n in names, digest, names)
    aggregated = [p for p in problems if "Invalid names" in p]
    check("invalid placeholder names are aggregated with nearest valid names",
          len(aggregated) == 1 and all(n in aggregated[0] for n in names), str(problems))
    check("plain FIELD can list the exact computed placeholder keys",
          all(f"`{n}`" in outline_mod.plain_field(names) for n in names))

    equal = {"bull": {}, "bear": {},
             "scenario_probabilities": {"bull": .25, "base": .50, "bear": .25}}
    check("equal prompt-example weights need a rationale",
          any("evidence-based weights" in p for p in pitch_mod._scenario_probability_problems(equal)))
    equal["scenario_probabilities_rationale"] = (
        "The balanced weights reflect the recent backlog conversion and disclosed risks in the notes.")
    check("evidence-based scenario rationale clears the check",
          pitch_mod._scenario_probability_problems(equal) == [])

    previous = config.AUTO_REPAIR_NO_REASK
    config.AUTO_REPAIR_NO_REASK = True
    try:
        draft = NS(repairable=["sentence removed", "untraced figure marked"])
        retry = pitch_mod._retryable_problems(
            draft, ["sentence removed", "untraced figure marked",
              "model input: terminal value was held to that range",
              "model input: WACC of 20% is outside 6%-14%", "missing conclusion"])
        check("bound input warnings retry despite automatic repair settings",
              retry == ["model input: WACC of 20% is outside 6%-14%", "missing conclusion"], str(retry))
    finally:
        config.AUTO_REPAIR_NO_REASK = previous

    original = ("```json\n{\"model\": {}}\n```\n\n## Intrinsic Valuation\n\n"
                "The old valuation paragraph explains the value.\n\n## Conclusions\n\nDone.")
    response = json.dumps({"patches": [{"section": "Intrinsic Valuation",
                                        "starts_with": "The old valuation paragraph",
                                        "replacement": "The revised paragraph uses the checked value."}]})
    merged = pitch_mod._apply_patches(original, response)
    check("a keyed paragraph patch merges and preserves other sections",
          merged is not None and "The revised paragraph" in merged
          and "## Conclusions\n\nDone." in merged and "\"model\": {}" in merged,
          str(merged))
    check("an ambiguous patch falls back cleanly",
          pitch_mod._apply_patches(original, json.dumps({"patches": [{
              "section": "Missing", "starts_with": "Nowhere", "replacement": "Text"}]})) is None)
    for label, text, start in (
            ("wrapped paragraph", "First line starts here\nand continues.\n\nNext block.", "First line starts"),
            ("bullet list", "- First bullet starts\n- Second bullet remains\n\nNext block.", "- First bullet"),
            ("last paragraph", "Opening block.\n\nLast paragraph to update.", "Last paragraph")):
        source = "```json\n{}\n```\n\n## Conclusions\n\n" + text
        patched = pitch_mod._apply_patches(source, json.dumps({"patches": [{
            "section": "Conclusions", "starts_with": start, "replacement": "Replacement whole block."}]}))
        check(f"paragraph patch replaces the whole {label}", patched is not None
              and "Replacement whole block." in patched and "continues." not in patched
              and "Second bullet remains" not in patched, str(patched))
    ambiguous = "```json\n{}\n```\n\n## Conclusions\n\nOpening one.\n\nOpening two."
    check("ambiguous paragraph starts return None", pitch_mod._apply_patches(ambiguous,
          json.dumps({"patches": [{"section": "Conclusions", "starts_with": "Opening",
                                   "replacement": "Changed."}]})) is None)

    old_select = pitch_mod.structured_brief.select
    try:
        pitch_mod.structured_brief.select = lambda db: {"revenue": ["shown", "not_shown"]}
        run = NS(notes=[NS(url="https://example.com/notes", title="note")], db=NS(facts={
            "shown": NS(usable=True, source_kind="sec_inline", data_type="reported",
                        source_url="https://sec.gov/shown", source="10-Q"),
            "not_shown": NS(usable=True, source_kind="market", data_type="reported",
                            source_url="https://sec.gov/not-shown", source="10-Q")}))
        merged_sources = pitch_mod._merge_sources([], run)
        check("structured source list includes only rendered fact URLs",
              [s["url"] for s in merged_sources] == ["https://sec.gov/shown"],
              str(merged_sources))
    finally:
        pitch_mod.structured_brief.select = old_select


def test_source_quality():
    primary = ["https://www.sec.gov/Archives/edgar/data/1/x.htm",
               "https://investor.nvidia.com/financial-info/financial-reports/",
               "https://nvidianews.nvidia.com/news/nvidia-announces-financial-results",
               "https://www.businesswire.com/news/home/2026/results",
               "https://www.example-co.com/investors/annual-report-2025"]
    secondary = ["https://sparkco.ai/blog/nvidia", "https://www.visualcapitalist.com/chart",
                 "https://www.reuters.com/technology/nvidia-results"]
    check("primary sources recognised", all(research_tools.is_primary(u) for u in primary),
          str([u for u in primary if not research_tools.is_primary(u)]))
    check("secondary sources not mistaken for primary",
          not any(research_tools.is_primary(u) for u in secondary),
          str([u for u in secondary if research_tools.is_primary(u)]))
    check("social and dump sites excluded",
          all(research_tools.is_excluded(u) for u in [
              "https://www.facebook.com/moomoosingapore/posts/1",
              "https://www.scribd.com/document/980691134/x",
              "https://www.linkedin.com/posts/someone"])
          and not research_tools.is_excluded("https://www.reuters.com/x"))
    page = research_tools.fetch_page("https://www.facebook.com/financialtimes/posts/1")
    check("excluded page not fetched", page.startswith("Skipped") and "not citable" in page)

    sent: dict = {}

    def tavily_post(url, headers=None, json=None, timeout=None):
        sent.update(json=json)
        return NS(raise_for_status=lambda: None, json=lambda: {"results": [
            {"title": "Example Q2 release", "url": "https://investor.example.com/q2", "content": "x"}]})

    class KeylessDDGS:
        def text(self, query, max_results=None):
            return [{"title": "FB post", "href": "https://www.facebook.com/p/1", "body": "x"},
                    {"title": "Reuters", "href": "https://www.reuters.com/a", "body": "Example posts Q2 results"}]

    original = config.TAVILY_API_KEY, research_tools.requests, research_tools.DDGS
    try:
        config.TAVILY_API_KEY = "tvly-test"
        research_tools.requests = NS(post=tavily_post)
        out = research_tools.web_search("Example Q2 2026")
        check("Tavily asked to exclude social and dump sites",
              "facebook.com" in sent["json"]["exclude_domains"])
        check("primary results tagged for the model", "[primary source]" in out)

        config.TAVILY_API_KEY = ""
        research_tools.DDGS = KeylessDDGS
        out = research_tools.web_search("Example Q2 2026")
        check("keyless results filtered too", "facebook" not in out and "Reuters" in out)
    finally:
        config.TAVILY_API_KEY, research_tools.requests, research_tools.DDGS = original


def verdict(*issues):
    """A reviewer's reply; each issue is (severity, problem) or (severity, problem, code)."""
    body = {"verdict": "revise" if any(i[0] in ("high", "medium") for i in issues) else "pass",
            "issues": [{"severity": i[0], "section": "Revenue Growth and Margins",
                        "code": i[2] if len(i) > 2 else "ASSUMPTION_ERROR",
                        "status": "FAIL" if i[0] == "high" else "PARTIAL",
                        "claim": "Premium grows 10% a year", "problem": i[1], "fix": "Anchor it."}
                       for i in issues]}
    return "```json\n" + json.dumps(body) + "\n```"


async def test_review(data):
    """A second model reviews the argument in rounds: each repair is reviewed again, and a
    repair is kept only if it passes the checks."""
    config.REVIEW_ENABLED = True
    saved_pause, config.REVIEW_RETRY_PAUSE_S = config.REVIEW_RETRY_PAUSE_S, 0
    try:
        def scripted(*turns):
            return FakeClient([[delta(content="DONE"), final_chunk()]]
                              + [[delta(content=t), final_chunk()] for t in turns])

        clean = HEADER + BODY
        client = scripted(clean, verdict())
        result = await pitch_mod.generate(data, client)
        calls = client.completions.calls
        check("clean review costs one extra call", len(calls) == 3, f"{len(calls)} calls")
        check("review runs on the review model", calls[2]["model"] == config.REVIEW_MODEL)
        check("reviewer sees the pitch, the inputs, consensus and notes",
              all(k in calls[2]["messages"][1]["content"] for k in
                  ("# The pitch", "wacc_rationale", "Sell-side consensus", "# Research notes")))
        check("passed review leaves no notes",
              result.review == [] and result.review_status == "passed", result.review_status)

        revised = json.dumps({"patches": [{
            "section": "Investment Thesis", "starts_with": "- **Label economics.**",
            "replacement": "- **Label economics.** Rates fell to c.52%. REVISED."}]})
        growth = ("high", "Growth path has no evidence.", "THIN_FORWARD_VIEW")
        client = scripted(clean, verdict(growth), revised, verdict())
        result = await pitch_mod.generate(data, client)
        calls = client.completions.calls
        check("a high issue triggers a repair, and the repair is reviewed again",
              len(calls) == 5 and calls[4]["model"] == config.REVIEW_MODEL, f"{len(calls)} calls")
        check("repair goes to the writer with the issue, its code and how to repair it",
              calls[3]["model"] == config.MODEL
              and "Growth path has no evidence." in calls[3]["messages"][-1]["content"]
              and "THIN_FORWARD_VIEW (FAIL)" in calls[3]["messages"][-1]["content"]
              and "rests on judgement" in calls[3]["messages"][-1]["content"]
              and calls[3]["messages"][-2]["role"] == "assistant")
        second = calls[4]["messages"][1]["content"]
        check("the second round's reviewer is shown the first round's points",
              "# Round 1 of the review" in second and "Growth path has no evidence." in second,
              second[:400])
        check("a repair the second round passes ships clean",
              "REVISED." in result.body and result.review == []
              and result.review_status == "passed in round 2", result.review_status)
        check("the review log has both rounds with codes and the repair",
              [(r["round"], r["fail"], r["codes"], r["repair"]) for r in result.review_log]
              == [(1, 1, {"THIN_FORWARD_VIEW": 1}, "paragraphs patched"), (2, 0, {}, None)]
              and result.replay["review_rounds"] == result.review_log, str(result.review_log))
        card = tg_format.summary_card(result, data)
        check("card has no open reviewer point after a clean round", "open reviewer" not in card)
        check("usage counts research, write, two reviews and the repair",
              result.usage["prompt_tokens"] == 500, str(result.usage))
        check("each stage's time goes in the replay file",
              [s for s, _ in result.replay["timings"]] == ["research", "draft 1", "review",
                                                           "draft 2", "review"], str(result.replay))
        path = pdf_render.build(result, data)
        check("PDF with review rounds renders", path.exists() and path.stat().st_size > 3000)

        both = clean.replace("{{intrinsic}}, {{upside}} from here",
                             "US$9,999.00, {{upside}} from here")
        client = scripted(both, verdict(growth), *[revised, verdict()] * 3)
        await pitch_mod.generate(data, client)
        ask = client.completions.calls[3]["messages"][-1]["content"]
        check("one re-ask carries the code's problem and the reviewer's issue together",
              "9,999" in ask and "Growth path has no evidence." in ask, ask[:600])

        broken = json.dumps({"patches": [{
            "section": "Investment Thesis", "starts_with": "- **Label economics.**",
            "replacement": "- **Label economics.** I arrive at an intrinsic price of "
                           "US$9,999.00. REVISED."}]})
        client = scripted(clean, verdict(growth), broken, verdict(growth),
                          broken, verdict(growth))
        result = await pitch_mod.generate(data, client)
        check("repairs that make no progress stop after two rounds in a row",
              len(client.completions.calls) == 7 and len(result.review_log) == 3,
              f"{len(client.completions.calls)} calls, {len(result.review_log)} rounds")
        check("a repair that breaks the numbers is thrown away",
              "REVISED." not in result.body and result.checks == [])
        check("the shipped draft's own open point is in Reviewer notes",
              [i["problem"] for i in result.review] == ["Growth path has no evidence."]
              and not result.review_addressed
              and result.review_status == "3 rounds; 1 point(s) open, 1 material",
              result.review_status)
        check("one open reviewer point keeps the rating (the skill withholds above three)",
              result.get("recommendation") == result.valuation.rating and not result.withheld,
              str(result.get("recommendation")))
        card = tg_format.summary_card(result, data)
        check("card points to the open reviewer issue", "open reviewer issue" in card)
        path = pdf_render.build(result, data)
        check("PDF with Reviewer notes renders", path.exists() and path.stat().st_size > 3000)

        four = [("high", f"Point {n} has no evidence.", "FACT_ERROR") for n in range(4)]
        client = scripted(clean, verdict(*four), revised, verdict(*four), revised, verdict(*four))
        result = await pitch_mod.generate(data, client)
        check("more than three material points still open withholds the rating",
              result.get("recommendation") == "NO RATING" and "4 material" in result.withheld,
              f"{result.get('recommendation')} {result.withheld}")

        client = scripted(clean, verdict(growth), revised, verdict(growth), revised,
                          verdict(growth))
        result = await pitch_mod.generate(data, client)
        check("the rounds stop at config.REVIEW_ROUNDS",
              len(result.review_log) == config.REVIEW_ROUNDS
              and len(client.completions.calls) == 1 + 2 * config.REVIEW_ROUNDS,
              f"{len(result.review_log)} rounds")

        client = scripted(clean, "I think it is mostly fine.", verdict())
        result = await pitch_mod.generate(data, client)
        calls = client.completions.calls
        check("an unusable verdict advances to the next configured reviewer",
              len(calls) == 4 and calls[3]["model"] in config.REVIEW_FALLBACK_MODELS
              and result.review_status == "passed", result.review_status)

        # GLW and GOOG, 2026-09-29: every reviewer failed and the PDF said only
        # "reviewer unavailable". Fast failures get one more pass before that.
        client = scripted(clean, "I think it is mostly fine.", "Looks good to me.", verdict())
        result = await pitch_mod.generate(data, client)
        calls = client.completions.calls
        check("a reviewer that answered without a verdict is tried once more",
              len(calls) == 5 and calls[4]["model"] == config.REVIEW_MODEL
              and result.review_status == "passed", result.review_status)

        client = scripted(clean, "I think it is mostly fine.", "Looks good to me.")
        result = await pitch_mod.generate(data, client)
        check("unusable reviewer output skips the review after one retry pass",
              len(client.completions.calls) == 6 and "skipped" in result.review_status
              and result.review == [], f"{len(client.completions.calls)} {result.review_status}")
        check("the skip names each reviewer's failure",
              all(m.rsplit("/", 1)[-1] in result.review_status
                  for m in (config.REVIEW_MODEL, config.REVIEW_LAST_RESORT_MODEL)),
              result.review_status)
        check("a review that did not finish withholds the rating",
              result.get("recommendation") == "NO RATING" and "did not finish" in result.withheld,
              f"{result.get('recommendation')} {result.withheld}")

        client = scripted(clean)  # the review call finds nothing to answer it
        result = await pitch_mod.generate(data, client)
        check("a failing reviewer never fails the pitch",
              result.get("ticker") == "SPOT" and "skipped" in result.review_status)

        client = scripted(clean, verdict(("low", "Minor."),
                                         ("medium", "Reads awkwardly.", "STYLE_ONLY")))
        result = await pitch_mod.generate(data, client)
        check("low and style-only issues do not trigger a repair",
              len(client.completions.calls) == 3 and result.review == [])

        client = scripted(clean, verdict(("medium", "Could cite the 10-Q.", "CITATION_ERROR")),
                          revised, verdict())
        result = await pitch_mod.generate(data, client)
        check("a medium issue is repaired like a high one, as the skill repairs PARTIAL",
              len(client.completions.calls) == 5 and result.review_status == "passed in round 2",
              result.review_status)

        wrong = HEADER + BODY.replace("{{intrinsic}}, {{upside}} from here",
                                      "US$9,999.00, {{upside}} from here")
        wrong_patch = json.dumps({"patches": [{
            "section": "Intrinsic Valuation", "starts_with": "Discounting at",
            "replacement": "Discounting at {{wacc}} gives an enterprise value of {{ev}} "
                           "and an implied share price of US$9,999.00, {{upside}} from here. "
                           "Free cash flow reaches {{fcf:FY2030}} by FY2030."}]})
        client = scripted(wrong, verdict(), wrong_patch, verdict(), wrong_patch, verdict())
        result = await pitch_mod.generate(data, client)
        check("a leftover prose mismatch does not stop the review; the repairs stop when "
              "two in a row leave it",
              len(client.completions.calls) == 1 + 2 * 3
              and result.review_status == "passed in round 3"
              and "9,999" not in result.body, result.review_status)

        no_model = json.dumps({k: v for k, v in HEADER_OBJ.items() if k != "model"})
        typed = "```json\n" + no_model + "\n```\n" + BODY
        client = scripted(*[typed] * pitch_mod.DRAFTS)
        result = await pitch_mod.generate(data, client)
        check("an uncomputable valuation is not reviewed",
              len(client.completions.calls) == 1 + pitch_mod.DRAFTS
              and "skipped" in result.review_status)

        class DownFor(FakeCompletions):
            def __init__(self, scripts, down):
                super().__init__(scripts)
                self.down = down

            async def create(self, **kwargs):
                if kwargs["model"] in self.down:
                    self.calls.append(kwargs)
                    raise TimeoutError("queued too long")
                return await super().create(**kwargs)

        saved = (config.GEMINI_API_KEY, config.REVIEW_FALLBACK_MODELS, pitch_mod._gemini_client)
        try:
            config.GEMINI_API_KEY = "test-key"
            config.REVIEW_FALLBACK_MODELS = ["gemini-2.5-flash"]
            client = scripted(clean, verdict())
            client.completions = DownFor(client.completions._scripts, {config.REVIEW_MODEL})
            client.chat = NS(completions=client.completions)
            pitch_mod._gemini_client = client
            result = await pitch_mod.generate(data, client)
            models = [c["model"] for c in client.completions.calls]
            check("a timed-out reviewer hands the review to the fallback",
                  models[-2:] == [config.REVIEW_MODEL, "gemini-2.5-flash"]
                  and result.review_status == "passed", f"{models} {result.review_status}")

            client = scripted(clean)
            client.completions = DownFor(client.completions._scripts,
                                         {config.REVIEW_MODEL, "gemini-2.5-flash"})
            client.chat = NS(completions=client.completions)
            pitch_mod._gemini_client = client
            result = await pitch_mod.generate(data, client)
            check("every reviewer down still ships the pitch unreviewed",
                  "skipped" in result.review_status, result.review_status)
            timed_out = [c["model"] for c in client.completions.calls].count(config.REVIEW_MODEL)
            check("a timed-out reviewer is not retried, and the skip says it timed out",
                  timed_out == 1 and "no answer in" in result.review_status,
                  f"{timed_out} {result.review_status}")
        finally:
            config.GEMINI_API_KEY, config.REVIEW_FALLBACK_MODELS, pitch_mod._gemini_client = saved

        config.REVIEW_ENABLED = False
        client = scripted(clean)
        result = await pitch_mod.generate(data, client)
        check("REVIEW_ENABLED off means no review call", len(client.completions.calls) == 2)
    finally:
        config.REVIEW_ENABLED = False
        config.REVIEW_RETRY_PAUSE_S = saved_pause


async def test_tool_timeout(data):
    """A hung search cannot hold research hostage (Brave, 11 minutes, 2026-09-23)."""
    original, saved = research_tools.dispatch, config.TOOL_TIMEOUT_S

    def hanging(name, args, log_to=None):
        time.sleep(2)
        return "too late"

    research_tools.dispatch, config.TOOL_TIMEOUT_S = hanging, 0.2
    try:
        search = [tool_delta(0, "c1", "web_search", '{"query": "x"}'), final_chunk("tool_calls")]
        client = FakeClient([search, [delta(content="DONE"), final_chunk()],
                             [delta(content=HEADER + BODY), final_chunk()]])
        start = time.monotonic()
        await pitch_mod.generate(data, client)
        elapsed = time.monotonic() - start
        check("a hung search is abandoned at the time limit", elapsed < 1.5, f"{elapsed:.1f}s")
        check("the model is told the search timed out",
              "timed out" in client.completions.calls[1]["messages"][-1]["content"])
    finally:
        research_tools.dispatch, config.TOOL_TIMEOUT_S = original, saved


def test_sensitivity_and_peers():
    """The sensitivity grid and the peer cross-check, against hand-checkable numbers."""
    data = synthetic_data(ttm_ebitda=400.0, ttm_net_income=250.0)
    v = valuation.compute(synthetic_model(), data)
    near = lambda a, b, tol=0.01: a is not None and abs(a - b) <= tol

    check("sensitivity centre is the base case",
          near(valuation.value_at(v, v.wacc, growth=v.growth), v.per_share))
    check("higher WACC, lower value",
          valuation.value_at(v, 0.12, growth=0.02) < v.per_share
          < valuation.value_at(v, 0.08, growth=0.02))
    check("terminal growth too close to WACC is not a number",
          valuation.value_at(v, 0.06, growth=0.058) is None)
    grid = valuation.sensitivity_markdown(v)
    check("two 5 x 5 grids, the base case bold in the first",
          grid.count("| WACC ") == 10 and f"**{v.per_share:,.2f}**" in grid
          and "exit EV/EBITDA (reference only)" in grid, grid)

    exit_v = valuation.compute(synthetic_model(terminal={"method": "exit_multiple",
                                                         "exit_ev_ebit": 10}), data)
    check("exit-multiple pitches get an exit-multiple grid",
          "terminal growth (reference only)" in valuation.sensitivity_markdown(exit_v))

    peer = market_data.PeerMultiples
    peers = [peer("AAA", "Alpha", 10.0, 20.0), peer("BBB", "Beta", 14.0, 30.0),
             peer("CCC", "Gamma", 12.0, None)]
    pv = valuation.compute(synthetic_model(), data, peers)
    pc = pv.peers
    # Median EV/EBITDA 12x on 400 EBITDA = 4,800 EV; +500 cash -200 debt = 5,100 equity
    # over 10mn shares = 510.00. Median P/E 25x on 250 net income = 6,250 / 10mn = 625.00.
    check("peer medians computed", pc.median_ev_ebitda == 12.0 and pc.median_pe == 25.0)
    check("peer EV/EBITDA value bridges through cash and debt",
          near(pc.value_ev_ebitda, 510.0), f"{pc.value_ev_ebitda}")
    check("peer P/E value", near(pc.value_pe, 625.0), f"{pc.value_pe}")
    check("peer check rendered with the DCF alongside",
          "Peers (trailing multiples)" in valuation.model_outputs_markdown(pv)
          and "Gamma" in valuation.model_outputs_markdown(pv))
    check("a DCF within 50% of the peers is not noted",
          not any("peer-multiple value" in n for n in pv.notes), str(pv.notes))
    check("a terminal value far below the peer multiple is noted with both multiples",
          any("7.7x FY2031 EBITDA" in n and "12.0x" in n for n in pv.notes), str(pv.notes))
    check("...but a note alone does not send the pitch back", pv.warnings == [],
          str(pv.warnings))
    rich = [peer("AAA", "Alpha", 30.0, None), peer("BBB", "Beta", 30.0, None)]
    far = valuation.compute(synthetic_model(), data, rich)   # peers imply 1,230.00
    check("a DCF far from the peers is noted for the reviewer",
          any("peer-multiple value" in n for n in far.notes), str(far.notes))
    check("peer placeholders available",
          valuation.placeholders(pv)["peer_value"] == "USD 510.00")
    check("the rating still follows the DCF", pv.rating == v.rating)

    one = valuation.compute(synthetic_model(), data, [peers[0]])
    check("one peer is not a peer check", one.peers is None)
    check("a sentence quoting the peer value is not a contradiction",
          consistency.check("The implied value on peer multiples is USD 510.00 a share.", pv)
          == [])


async def test_peer_lookup_in_pitch(data):
    """The model names peers; the code fetches them once, in the background, time-capped."""
    fetched: list = []

    def fake_peers(symbols):
        fetched.append(tuple(symbols))
        return [market_data.PeerMultiples("NFLX", "Netflix", 30.0, 40.0),
                market_data.PeerMultiples("WMG", "Warner Music", 14.0, 25.0)]

    header = dict(HEADER_OBJ)
    header["model"] = dict(HEADER_OBJ["model"], peers=["nflx", "WMG"])
    with_peers = "```json\n" + json.dumps(header) + "\n```\n" + BODY
    original = market_data.peer_multiples
    market_data.peer_multiples = fake_peers
    try:
        wrong = with_peers.replace("{{intrinsic}}, {{upside}} from here",
                                   "US$9,999.00, {{upside}} from here")
        patch = json.dumps({"patches": [{
            "section": "Intrinsic Valuation", "starts_with": "Discounting at",
            "replacement": "Discounting at {{wacc}} gives an enterprise value of {{ev}} "
                           "and an implied share price of {{intrinsic}}, {{upside}} from here. "
                           "Free cash flow reaches {{fcf:FY2030}} by FY2030."}]})
        client = FakeClient([[delta(content="DONE"), final_chunk()],
                             [delta(content=wrong), final_chunk()],
                             [delta(content=patch), final_chunk()]])
        result = await pitch_mod.generate(data, client)
        check("peers fetched once across a rewrite", fetched == [("NFLX", "WMG")], str(fetched))
        check("peer check reaches the pitch",
              result.valuation.peers is not None and "Peers (trailing multiples)" in result.body
              and "WACC by terminal growth" in result.body)

        def slow(symbols):
            time.sleep(2)
            return []

        market_data.peer_multiples, saved = slow, config.PEER_TIMEOUT_S
        config.PEER_TIMEOUT_S = 0.2
        try:
            client = FakeClient([[delta(content="DONE"), final_chunk()],
                                 [delta(content=with_peers), final_chunk()]])
            start = time.monotonic()
            result = await pitch_mod.generate(data, client)
            check("a slow peer lookup does not hold the pitch",
                  time.monotonic() - start < 1.5 and result.valuation.peers is None
                  and result.checks == [])
        finally:
            config.PEER_TIMEOUT_S = saved
    finally:
        market_data.peer_multiples = original


async def test_free_tier_resilience(data):
    """Gemini's 20-a-day quota and failing research models must not sink a pitch."""
    import httpx2
    import openai

    def rate_limited(daily: bool):
        response = httpx2.Response(
            429, request=httpx2.Request("POST", "https://generativelanguage.googleapis.com"))
        quota = ("GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit: 20" if daily
                 else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
        return openai.RateLimitError(f"Quota exceeded: {quota}", response=response, body=None)

    overloaded = openai.APIError("Service temporarily overloaded", request=None, body=None)

    class Scripted(FakeCompletions):
        """Answers per model from a plan: a list of outcomes consumed call by call."""

        def __init__(self, plan):
            super().__init__([])
            self.plan, self.models = plan, []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            model = kwargs["model"]
            self.models.append(model)
            outcome = self.plan.get(model, ["write"]).pop(0) if self.plan.get(model) else "write"
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == "search":
                return FakeStream([tool_delta(0, "c1", "web_search", '{"query": "NVDA Q2"}'),
                                   final_chunk("tool_calls")])
            text = "DONE" if "tools" in kwargs else HEADER + BODY
            return FakeStream([delta(content=text), final_chunk()])

    def client_for(plan):
        c = FakeClient([])
        c.completions = Scripted(plan)
        c.chat = NS(completions=c.completions)
        return c

    monkey: dict = {}
    original = patch_tools(monkey)
    saved = (config.GEMINI_API_KEY, config.RESEARCH_MODEL, pitch_mod._gemini_client,
             config.WRITE_FALLBACK_MODELS, config.RETRY_BASE_DELAY_S)
    config.GEMINI_API_KEY, config.RESEARCH_MODEL = "test-key", "gemini-2.5-flash"
    config.WRITE_FALLBACK_MODELS, config.RETRY_BASE_DELAY_S = ["gemini-2.5-flash"], 0.01
    pitch_mod._quota_exhausted.clear()
    try:
        # Gemini's daily quota runs out mid-research: research carries on on the 550B.
        client = client_for({"gemini-2.5-flash": ["search", rate_limited(daily=True)]})
        pitch_mod._gemini_client = client
        result = await pitch_mod.generate(data, client)
        research = [k["model"] for k in client.completions.calls if "tools" in k]
        check("spent Gemini quota hands research to the primary",
              research == ["gemini-2.5-flash", "gemini-2.5-flash", config.MODEL],
              " -> ".join(research))
        check("spent quota is remembered until the reset",
              not pitch_mod._available("gemini-2.5-flash"))

        client = client_for({})
        pitch_mod._gemini_client = client
        await pitch_mod.generate(data, client)
        check("a spent model is not called again that day",
              "gemini-2.5-flash" not in client.completions.models,
              " -> ".join(client.completions.models))
        check("nor offered as a write fallback",
              "gemini-2.5-flash" not in pitch_mod._write_candidates())
        pitch_mod._quota_exhausted.clear()

        # A per-minute limit is not a spent day.
        client = client_for({"gemini-2.5-flash": [rate_limited(daily=False)]})
        pitch_mod._gemini_client = client
        await pitch_mod.generate(data, client)
        check("a per-minute limit does not mark the day spent",
              pitch_mod._available("gemini-2.5-flash"))

        # Every research model fails after a round of searching: write from what was found.
        down = [overloaded] * 10
        client = client_for({"gemini-2.5-flash": ["search", overloaded],
                             config.MODEL: list(down[:3]) + ["write"] * 3,
                             config.FALLBACK_MODEL: list(down)})
        pitch_mod._gemini_client = client
        result = await pitch_mod.generate(data, client)
        write = [k for k in client.completions.calls if "tools" not in k][0]
        check("research that gives out still yields a pitch from what it found",
              result.get("ticker") == "SPOT"
              and "stub result for web_search" in write["messages"][1]["content"])

        # Nothing found at all: that is still a failure worth reporting.
        client = client_for({"gemini-2.5-flash": [overloaded],
                             config.MODEL: list(down), config.FALLBACK_MODEL: list(down)})
        pitch_mod._gemini_client = client
        try:
            await pitch_mod.generate(data, client)
            check("research with nothing found still fails clearly", False, "no exception")
        except pitch_mod.PitchError as exc:
            check("research with nothing found still fails clearly", "research" in str(exc))
    finally:
        (config.GEMINI_API_KEY, config.RESEARCH_MODEL, pitch_mod._gemini_client,
         config.WRITE_FALLBACK_MODELS, config.RETRY_BASE_DELAY_S) = saved
        pitch_mod._quota_exhausted.clear()
        research_tools.dispatch = original


def test_street_view(result, data):
    """Every pitch shows the Street average and the named brokers beside its own value."""
    import pandas as pd

    rows = pd.DataFrame(
        {"Firm": ["JP Morgan", "Morgan Stanley", "JPMorgan", "Citi"],
         "ToGrade": ["Overweight", "Equal-Weight", "Neutral", "Buy"],
         "currentPriceTarget": [0.0, 300.0, 280.0, 310.0]},
        index=pd.to_datetime([pd.Timestamp.now() - pd.Timedelta(days=d) for d in (1, 5, 30, 2)]))
    fake = NS(upgrades_downgrades=rows)
    found = market_data.firm_targets(fake, ["JPMorgan", "Morgan Stanley", "Goldman Sachs"])
    jpm = found[0]
    check("firm names matched however they are spelled", jpm.firm == "JP Morgan")
    check("a rating change without a target keeps the last target and the new rating",
          jpm.target == 280.0 and jpm.grade == "Overweight", str(jpm))
    check("a firm without coverage is left out", [f.firm for f in found] == ["JP Morgan", "Morgan Stanley"])

    class Refused:
        @property
        def upgrades_downgrades(self):
            raise RuntimeError("quote summary refused")

    check("a refused lookup is reported as unavailable, not empty",
          market_data.firm_targets(Refused(), ["JP Morgan"]) is None)

    saved = data.firm_targets, data.analyst_target, data.analyst_count, config.STREET_FIRMS
    try:
        config.STREET_FIRMS = ["JP Morgan", "Morgan Stanley", "Goldman Sachs"]
        data.firm_targets = found
        data.analyst_target, data.analyst_count = 600.0, 38
        view = dict(pitch_mod.street_view(data))
        check("street view carries the average and each broker",
              view["Street average"].startswith(f"{data.currency} 600.00")
              and "38 analysts" in view["Street average"]
              and view["Morgan Stanley"].startswith(f"{data.currency} 300.00")
              and "Goldman Sachs" in view, str(view))
        card = tg_format.summary_card(result, data)
        check("the card shows the Street", "Street average" in card and "Morgan Stanley" in card)
        check("the write prompt names the brokers",
              "Named brokers" in pitch_mod._consensus_line(data))
        path = pdf_render.build(result, data)
        check("PDF with the Street block renders", path.exists())

        data.firm_targets = None
        check("unavailable targets say so",
              any("unavailable" in what for _, what in pitch_mod.street_view(data)))
    finally:
        data.firm_targets, data.analyst_target, data.analyst_count, config.STREET_FIRMS = saved


def test_tool_error_handling():
    """A failing tool must return a message, never raise into the generation."""
    result = research_tools.fetch_page("not-a-url")
    check("bad URL handled", result.startswith("Invalid URL"))

    result = research_tools.fetch_page("https://nonexistent.invalid.example/page")
    check("unreachable host handled", result.startswith("Could not fetch"))

    check("unknown tool handled", research_tools.dispatch("nope", {}).startswith("Unknown tool"))


def test_rendering(result, data):
    path = pdf_render.build(result, data)
    check("PDF written", path.exists() and path.stat().st_size > 3000,
          f"{path.stat().st_size} bytes")

    body = pdf_render._strip_header_line(result.body)
    check("redundant header line stripped", not body.startswith("Spotify Technology S.A. (NYSE"))

    card = tg_format.summary_card(result, data)
    check("card fits one Telegram message", len(card) <= tg_format.TG_LIMIT, f"{len(card)} chars")
    check("primary writer named on the PDF but not flagged on the card",
          result.writer == config.MODEL and "backup model" not in card)
    check("card carries the computed call",
          result.get("recommendation") in card and f"{result.get('upside_pct'):+.0f}%" in card)

    long_text = "\n\n".join(f"Paragraph {i} " + "word " * 80 for i in range(60))
    chunks = tg_format.split_message(long_text)
    check("splitter respects the limit", all(len(c) <= tg_format.TG_LIMIT for c in chunks),
          f"{len(chunks)} chunks")
    check("splitter loses no words",
          sum(len(c.split()) for c in chunks) == len(long_text.split()))


def test_symbol_resolution():
    class LookupTicker:
        def __init__(self, symbol):
            self.info = {"currentPrice": 1.0} if symbol in ("SPOT", "AAPL") else {}

        def history(self, *args, **kwargs):
            return pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))

    original_yf = market_data.yf
    market_data.yf = NS(
        Ticker=LookupTicker,
        Search=lambda query, **kwargs: NS(quotes=(
            [{"quoteType": "EQUITY", "symbol": "PLTR"}] if query == "Palantir" else [])))
    try:
        check("plain symbol resolves", market_data.resolve_symbol("spot") == "SPOT")
        check("dollar prefix stripped", market_data.resolve_symbol("$AAPL") == "AAPL")
        check("company name resolves", market_data.resolve_symbol("Palantir").startswith("PLTR"))
    finally:
        market_data.yf = original_yf
    try:
        market_data.resolve_symbol("zzzznotarealcompanyzzz")
        check("junk query rejected", False, "no exception")
    except market_data.TickerNotFound:
        check("junk query rejected", True)


def test_blocked_quote_summary():
    """From Render, Yahoo returned TSM's info with no price; chart data must fill in."""
    class BlockedTicker:
        info = {"trailingPegRatio": None}
        fast_info = {"lastPrice": 433.24, "currency": "USD", "exchange": "NYQ",
                     "marketCap": 2.25e12, "shares": 5.19e9}
        income_stmt = balance_sheet = cashflow = quarterly_income_stmt = None

        def get_history_metadata(self):
            return {"longName": "Taiwan Semiconductor Manufacturing Company Limited",
                    "fullExchangeName": "NYSE"}

    original_resolve, original_yf = market_data.resolve_symbol, market_data.yf
    original_ccy, original_fx = sec_xbrl.reporting_currency, market_data._fx_rate
    market_data.resolve_symbol = lambda query: "TSM"
    # Search refused too, as it was for FIX from Render.
    market_data.yf = NS(Ticker=lambda symbol: BlockedTicker(),
                        Search=lambda *args, **kwargs: NS(quotes=[]))
    # The summary's financialCurrency is gone with it: the filings give it (SPOT 2026-10-07).
    sec_xbrl.reporting_currency = lambda symbol: "TWD"
    market_data._fx_rate = lambda reporting, trading: 0.031 if reporting == "TWD" else 1.0
    try:
        data = market_data.fetch("TSM")
        check("a refused summary takes the reporting currency from the filings, at a dated rate",
              data.reporting_currency == "TWD" and data.fx == 0.031 and data.fx_date
              and f"USD per TWD, {data.fx_date}" in data.prompt_block,
              f"{data.reporting_currency} {data.fx} {data.fx_date}")
        check("symbol resolves from chart data when search is refused too",
              original_resolve("FIX") == "FIX")

        data = market_data.fetch("TSM")
        check("price falls back to chart data",
              data.price == 433.24 and data.market_cap == 2.25e12, f"{data.price}")
        check("chart fallback flagged as a data gap", "chart data" in data.prompt_block)
        check("company name recovered from chart data",
              data.display == "Taiwan Semiconductor Manufacturing Company Limited (NYSE: TSM)",
              data.display)

        BlockedTicker.fast_info = {}
        try:
            original_resolve("QZXQ")
            check("made-up symbol still rejected", False, "resolved")
        except market_data.TickerNotFound:
            check("made-up symbol still rejected", True)

        try:
            market_data.fetch("TSM")
            check("no price anywhere raises TickerNotFound", False, "no exception")
        except market_data.TickerNotFound as exc:
            check("no price anywhere raises TickerNotFound", "limiting this server" in str(exc))
    finally:
        market_data.resolve_symbol, market_data.yf = original_resolve, original_yf
        sec_xbrl.reporting_currency, market_data._fx_rate = original_ccy, original_fx


def test_peer_dkk_currency_conversion():
    """A USD market cap divided by DKK EBITDA must convert to a common currency."""
    class DkkPeer:
        quarterly_income_stmt = pd.DataFrame({
            pd.Timestamp("2026-06-30"): [10_000e6, 500e6, 200e6, 2_000e6],
            pd.Timestamp("2026-03-31"): [10_000e6, 500e6, 200e6, 2_000e6],
            pd.Timestamp("2025-12-31"): [10_000e6, 500e6, 200e6, 2_000e6],
            pd.Timestamp("2025-09-30"): [10_000e6, 500e6, 200e6, 2_000e6],
        }, index=["Total Revenue", "EBITDA", "Reconciled Depreciation", "Net Income"])
        quarterly_balance_sheet = pd.DataFrame({
            pd.Timestamp("2026-06-30"): [0.0, 0.0, 0.0]},
            index=["Cash Cash Equivalents And Short Term Investments", "Total Debt",
                   "Minority Interest"])

    original = (market_data.yf, market_data._chart_snapshot, market_data._fx_rate,
                market_data.operating_leases, sec_xbrl.reporting_currency)
    market_data.yf = NS(Ticker=lambda symbol: DkkPeer())
    market_data._chart_snapshot = lambda ticker: {"marketCap": 14_000_000_000.0,
                                                    "currency": "USD",
                                                    "longName": "Danish Reporter"}
    market_data._fx_rate = lambda reporting, trading: 0.14
    market_data.operating_leases = lambda *args: 0.0
    sec_xbrl.reporting_currency = lambda symbol: "DKK"
    try:
        peer = market_data._peer("DKK")
        check("peer EV/EBITDA converts USD market cap to DKK before dividing by DKK EBITDA",
              peer is not None and abs(peer.ev_ebitda - 50.0) < 1e-6, str(peer))
        check("peer P/E converts USD market cap to DKK before dividing by DKK net income",
              peer is not None and abs(peer.pe - 12.5) < 1e-6, str(peer))
    finally:
        (market_data.yf, market_data._chart_snapshot, market_data._fx_rate,
         market_data.operating_leases, sec_xbrl.reporting_currency) = original


def test_tavily_search():
    """With a key, search goes to Tavily; when Tavily fails, keyless engines take over."""
    sent: dict = {}

    def tavily_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, headers=headers, json=json)
        return NS(raise_for_status=lambda: None, json=lambda: {"results": [
            {"title": "Comfort Systems Q2 2026 results", "url": "https://example.com/q2",
             "content": "Revenue rose 30%."}]})

    def tavily_down(*args, **kwargs):
        raise requests.ConnectionError("tavily unreachable")

    class KeylessDDGS:
        def text(self, query, max_results=None):
            return [{"title": "Keyless result", "href": "https://example.org/k",
                     "body": "Comfort Systems, from the keyless engines."}]

    original = config.TAVILY_API_KEY, research_tools.requests, research_tools.DDGS
    research_tools.DDGS = KeylessDDGS
    try:
        config.TAVILY_API_KEY = "tvly-test"
        research_tools.requests = NS(post=tavily_post)
        sources = research_tools.SourceLog()
        out = research_tools.web_search("Comfort Systems Q2 2026", sources)
        check("search uses Tavily when a key is set",
              sent.get("url") == research_tools.TAVILY_URL and "Revenue rose" in out)
        check("Tavily key sent as a bearer token",
              sent["headers"]["Authorization"] == "Bearer tvly-test")
        check("Tavily kept to basic depth (1 credit)", sent["json"]["search_depth"] == "basic")
        check("Tavily results logged as sources",
              [s["url"] for s in sources.sources] == ["https://example.com/q2"])

        research_tools.requests = NS(post=tavily_down)
        out = research_tools.web_search("Comfort Systems Q2 2026")
        check("failed Tavily search falls back to keyless engines", "Keyless result" in out)

        sent.clear()
        config.TAVILY_API_KEY = ""
        research_tools.requests = NS(post=tavily_post)
        out = research_tools.web_search("Comfort Systems Q2 2026")
        check("no key means keyless engines only", not sent and "Keyless result" in out)
    finally:
        config.TAVILY_API_KEY, research_tools.requests, research_tools.DDGS = original


FILING = """<?xml version="1.0" encoding="utf-8"?>
<html><head><title>rtx-20260630</title></head><body>
<div style="display:none"><ix:header>0000101829 us-gaap:ProductMember 2026-06-30</ix:header></div>
<p>Item 2. Management's Discussion and Analysis of Financial Condition 31</p>
<p>Item 3. Quantitative and Qualitative Disclosures About Market Risk 48</p>
<p>Item 2. Management's Discussion and Analysis of Financial Condition and Results</p>
<p>BUSINESS OVERVIEW We are a systems provider to aerospace and defense.</p>
<p>RESULTS OF OPERATIONS</p>
<p>Net sales rose 9% in the quarter.</p>
</body></html>"""


def test_sec_fetch():
    """EDGAR wants a declared agent; a filing opens at MD&A, not its hidden XBRL."""
    sent: dict = {}

    served = {"text": FILING}

    def fake_get(url, timeout=None, headers=None):
        sent.update(url=url, headers=headers)
        return NS(raise_for_status=lambda: None, headers={"content-type": "text/html"},
                  text=served["text"])

    filing_url = ("https://www.sec.gov/Archives/edgar/data/101829/"
                  "000010182926000027/rtx-20260630.htm")
    original = config.SEC_USER_AGENT, research_tools.requests
    research_tools.requests = NS(get=fake_get)
    try:
        config.SEC_USER_AGENT = "StockThesisBot test@example.com"
        page = research_tools.fetch_page(filing_url)
        check("sec.gov gets the declared agent",
              sent["headers"]["User-Agent"] == "StockThesisBot test@example.com")
        check("hidden XBRL data dropped", "us-gaap" not in page)
        check("filing opens at Results of Operations, past contents and overview",
              "Net sales rose 9%" in page and "Item 3." not in page
              and "BUSINESS OVERVIEW" not in page)

        # Apple's MD&A has no Results of Operations heading: open at MD&A itself.
        served["text"] = FILING.replace("RESULTS OF OPERATIONS", "Segment Operating Performance")
        page = research_tools.fetch_page(filing_url)
        check("filing without that heading opens at MD&A",
              "BUSINESS OVERVIEW" in page and "Item 3." not in page)
        served["text"] = FILING

        research_tools.fetch_page("https://example.com/q2")
        check("other sites keep the browser agent",
              sent["headers"]["User-Agent"] == research_tools.USER_AGENT)

        sent.clear()
        config.SEC_USER_AGENT = ""
        page = research_tools.fetch_page(filing_url)
        check("no SEC_USER_AGENT skips sec.gov", not sent and "SEC_USER_AGENT" in page)
    finally:
        config.SEC_USER_AGENT, research_tools.requests = original

    sources = research_tools.SourceLog()
    out = research_tools.web_search(
        "https://data.sec.gov/api/xbrl/companyfacts/CIK101829.json", sources)
    check("URL sent to web_search is pointed at fetch_page",
          "fetch_page" in out and sources.searches == 0)


def test_edgar_filings():
    """The latest release and 10-Q come from EDGAR, not from the model's luck in search."""
    base = "https://www.sec.gov/Archives/edgar/data/753308/"
    submissions = {"filings": {"recent": {
        "form": ["8-K", "4", "8-K", "10-Q", "8-K"],
        "filingDate": ["2026-09-03", "2026-08-01", "2026-07-24", "2026-07-30", "2026-04-23"],
        "accessionNumber": ["0000753308-26-000070", "0000753308-26-000066",
                            "0000753308-26-000058", "0000753308-26-000060",
                            "0000753308-26-000028"],
        "primaryDocument": ["a.htm", "f4.xml", "nee-20260724.htm", "nee-20260630.htm", "b.htm"],
        "items": ["7.01,9.01", "", "2.02,9.01", "", "2.02,9.01"],
    }}}
    index = ('<table><tr><td>1</td><td>8-K</td><td><a href="/ix?doc=/Archives/edgar/data/'
             '753308/000075330826000058/nee-20260724.htm">nee-20260724.htm</a></td>'
             '<td>8-K</td></tr><tr><td>2</td><td>EXHIBIT 99</td><td><a href="/Archives/edgar/'
             'data/753308/000075330826000058/neeq22026exhibit99.htm">ex</a></td>'
             '<td>EX-99</td></tr></table>')
    pages = {
        research_tools.SEC_TICKERS_URL: {"0": {"cik_str": 753308, "ticker": "NEE"}},
        "https://data.sec.gov/submissions/CIK0000753308.json": submissions,
        base + "000075330826000058/0000753308-26-000058-index.htm": index,
        base + "000075330826000058/neeq22026exhibit99.htm":
            "<html><title>Q2 2026 results</title><body><p>NEER backlog now totals "
            "approximately 35.1 GW.</p></body></html>",
        base + "000075330826000060/nee-20260630.htm": FILING,
    }
    asked: list = []

    def fake_get(url, timeout=None, headers=None):
        asked.append((url, headers))
        if url not in pages:
            raise requests.HTTPError(f"404 {url}")
        body = pages[url]
        return NS(raise_for_status=lambda: None, headers={"content-type": "text/html"},
                  text=body if isinstance(body, str) else json.dumps(body),
                  json=lambda: body)

    original = config.SEC_USER_AGENT, research_tools.requests, research_tools._ciks
    research_tools.requests = NS(get=fake_get, HTTPError=requests.HTTPError)
    try:
        config.SEC_USER_AGENT, research_tools._ciks = "StockThesisBot test@example.com", None
        found = research_tools.latest_filings("NEE")
        check("EDGAR: the earnings 8-K's exhibit, not a later 8-K without results",
              len(found) == 2 and found[0].url.endswith("neeq22026exhibit99.htm")
              and "filed 2026-07-24" in found[0].label, str(found))
        check("EDGAR: the latest 10-Q", found[1].url.endswith("000075330826000060/nee-20260630.htm")
              and "10-Q, filed 2026-07-30" in found[1].label, str(found))
        sources = research_tools.SourceLog()
        read = research_tools.read_latest_filings("NEE", sources)
        check("EDGAR: both read, labelled, and counted as primary",
              len(read) == 2 and sources.primary_fetches == 2 and "35.1 GW" in read[0][1]
              and read[0][1].startswith("[latest earnings release")
              and "Net sales rose 9%" in read[1][1], str([r[1][:80] for r in read]))
        check("EDGAR: every request declares the agent",
              all(h["User-Agent"] == "StockThesisBot test@example.com" for _, h in asked))
        check("EDGAR: a non-US listing is not looked up",
              research_tools.read_latest_filings("UMG.AS") == [])

        del pages["https://data.sec.gov/submissions/CIK0000753308.json"]
        check("EDGAR: a failed lookup leaves research to find the filings",
              research_tools.read_latest_filings("NEE") == [])

        asked.clear()
        config.SEC_USER_AGENT = ""
        check("EDGAR: nothing is asked without SEC_USER_AGENT",
              research_tools.read_latest_filings("NEE") == [] and not asked)

        pages["https://investor.example.com/q2-release"] = (
            "<html><body><p>" + "Segment detail. " * 1500 + "</p></body></html>")
        pages["https://blog.example.com/q2"] = pages["https://investor.example.com/q2-release"]
        primary = research_tools.fetch_page("https://investor.example.com/q2-release")
        other = research_tools.fetch_page("https://blog.example.com/q2")
        check("a primary page is read further than a blog",
              len(primary) > research_tools.MAX_PAGE_CHARS + 2000 and len(other) < len(primary),
              f"{len(primary)} vs {len(other)}")
    finally:
        config.SEC_USER_AGENT, research_tools.requests, research_tools._ciks = original

    front = "Risk factors: the pending merger may not close. " * 800
    notes = (front + "Symmetry Acquisition - NEER paid an aggregate purchase price of $1.1 "
             "billion for the acquisition. " + "Other notes. " * 200
             + "Proposed Business Combination - each Dominion Energy share will be converted "
             "into the right to receive 0.8138 shares of NEE common stock under the merger "
             "agreement. " + "Stock awards carry the right to receive shares. " * 300)
    terms = research_tools.deal_terms(notes)
    check("deal terms: the merger's consideration, not a closed purchase or stock awards",
          terms is not None and "0.8138" in terms and "Symmetry" not in terms, (terms or "")[:200])
    check("deal terms: none when no deal is priced",
          research_tools.deal_terms(front + "Other notes. " * 200
                                    + "Stock awards carry the right to receive shares. " * 50)
          is None)

    findings = [("web_search", "nee backlog", "S" * 400),
                ("fetch_page", "https://blog.example.com/nee", "BLOG"),
                ("fetch_page", "https://www.sec.gov/Archives/nee-10q.htm", "FILING")]
    digest = pitch_mod._build_digest(findings)
    check("digest puts filings first and snippets last",
          digest.index("FILING") < digest.index("BLOG") < digest.index("SSSS"), digest[:200])
    cap = config.DIGEST_MAX_CHARS
    config.DIGEST_MAX_CHARS = 300
    try:
        digest = pitch_mod._build_digest(findings)
    finally:
        config.DIGEST_MAX_CHARS = cap
    check("the cap cuts snippets, not the filing", "FILING" in digest and "SSSS" not in digest)


def test_foreign_latest_filings_include_annual_and_6k():
    """Foreign issuers need both the latest annual filing and their current 6-K."""
    cik, accession = 353278, "0000353278-26-000023"
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/000035327826000023/"
    recent = {"filings": {"recent": {
        "form": ["6-K", "20-F"],
        "filingDate": ["2026-08-04", "2026-03-20"],
        "accessionNumber": [accession, "0000353278-26-000007"],
        "primaryDocument": ["caq22026.htm", "nn-2025xannualreport.htm"],
        "items": ["", ""],
        "reportDate": ["2026-06-30", "2025-12-31"],
    }}}
    index = ('<table><tr><td>1</td><td>6-K</td><td><a href="caq22026.htm">caq22026.htm</a>'
             '</td><td>6-K</td><td>1730052</td></tr></table>')
    pages = {
        "https://data.sec.gov/submissions/CIK0000353278.json": recent,
        base + accession + "-index.htm": index,
        base + "caq22026.htm": "Novo Nordisk reports adjusted operating profit and raises "
                               "full-year outlook for Q2 2026.",
    }

    def fake_get(url, timeout=None, headers=None):
        if url not in pages:
            raise requests.HTTPError(f"404 {url}")
        body = pages[url]
        return NS(raise_for_status=lambda: None, headers={"content-type": "text/html"},
                  text=body if isinstance(body, str) else json.dumps(body),
                  json=lambda: body)

    original = research_tools.requests, research_tools._ciks
    try:
        research_tools.requests = NS(get=fake_get, HTTPError=requests.HTTPError)
        research_tools._ciks = {"NVO": cik}
        found = research_tools.latest_filings("NVO")
        check("foreign EDGAR lookup returns 6-K release, interim and latest 20-F",
              len(found) == 3 and all(f.url.endswith(suffix) for f, suffix in zip(
                  found, ("caq22026.htm", "caq22026.htm", "nn-2025xannualreport.htm"))),
              str(found))
    finally:
        research_tools.requests, research_tools._ciks = original


def test_private_and_primary_sources():
    import bot
    from types import SimpleNamespace as NS
    saved = config.ALLOWED_USER_IDS
    try:
        config.ALLOWED_USER_IDS = set()
        check("an empty allow-list serves no one",
              not bot._authorised(NS(effective_user=NS(id=1))))
        config.ALLOWED_USER_IDS = {1}
        check("an allowed user is served", bot._authorised(NS(effective_user=NS(id=1))))
    finally:
        config.ALLOWED_USER_IDS = saved
    check("a news site is never primary, whatever its host or path says",
          not research_tools.is_primary("https://news.yahoo.com/a")
          and not research_tools.is_primary("https://seekingalpha.com/investor-relations/a"))
    check("a company IR site and sec.gov stay primary",
          research_tools.is_primary("https://investor.apple.com/a")
          and research_tools.is_primary("https://www.sec.gov/a"))
    from types import SimpleNamespace as N
    d = N(revenue=100.0, prompt_block="", ttm_revenue=None, ttm_ebitda=None,
          ttm_net_income=None, shares_outstanding=1e6, market_cap=2e9, price=100.0,
          history=[("FY2023", 90, 0), ("2023", 120, 0), ("FY2024", 100, 0)])
    gates = valuation.data_gates(d)
    check("price x shares off the market cap is a data gate",
          any("market cap" in g for g in gates), str(gates))
    check("a restatement over 20% is a data gate",
          any("restatement" in g for g in gates), str(gates))
    # SPOT's four quarters to 2Q26: gains, interest and tax credits put net income over
    # operating EBITDA, and pre-tax income less tax is net income.
    spot = N(revenue=17186.0, prompt_block="", ttm_revenue=None, ttm_ebitda=2757.0,
             ttm_net_income=3339.0, ttm_operating_income=2653.0, ttm_pretax_income=3505.0,
             ttm_tax=166.0, shares_outstanding=None, market_cap=None, price=None, history=[])
    check("net income over EBITDA from gains and tax credits keeps the rating",
          not valuation.data_gates(spot), str(valuation.data_gates(spot)))
    check("the gap is explained in a note",
          "852mn from below operating income" in (market_data.net_income_gap(
              2757.0, 3339.0, 2653.0, 3505.0, 166.0) or ""))
    spot.ttm_pretax_income = 2500.0          # net income no longer pre-tax less tax
    check("a gap the statement does not reconcile still withholds the rating",
          any("not above trailing net income" in g for g in valuation.data_gates(spot)))
    spot.ttm_pretax_income, spot.ttm_operating_income = 3505.0, 3400.0
    check("a gap the items below operating income do not cover still withholds it",
          any("not above trailing net income" in g for g in valuation.data_gates(spot)))
    spot.ttm_operating_income = spot.ttm_pretax_income = spot.ttm_tax = None
    check("without the bridge figures the gap still withholds the rating",
          any("not above trailing net income" in g for g in valuation.data_gates(spot)))


def test_gates_and_reverse():
    """Hard data gates withhold the rating; the reverse DCF reproduces the price."""
    v = valuation.compute(synthetic_model(), synthetic_data())
    check("clean data passes every gate", not v.stops, str(v.stops))
    check("the implied margin revalues to the price",
          v.implied_margin is not None
          and abs(valuation._revalue(v, margin=v.implied_margin) - v.price) < 0.01)
    check("the implied WACC revalues to the price",
          v.implied_wacc is not None
          and abs(valuation._revalue(v, wacc=v.implied_wacc) - v.price) < 0.01)
    check("the implied terminal growth revalues to the price",
          v.implied_terminal is not None
          and abs(valuation._revalue(v, growth=v.implied_terminal) - v.price) < 0.01)
    check("conviction is computed", v.conviction in ("high", "medium", "low"))
    # Comfort Systems: one quarter's revenue read as the year.
    fix = synthetic_data(revenue=1830.0, ttm_revenue=11226.0,
                         history=[("FY2024", 7027.0, 900.0), ("FY2025", 1830.0, 300.0)])
    v = valuation.compute(synthetic_model(base_revenue=1830,
                                          revenue=[1830 * 1.1 ** k for k in range(1, 6)]), fix)
    check("a quarter read as the year fails the gates", len(v.stops) >= 2, str(v.stops))
    check("a failed gate publishes no rating", v.published_rating == "NO RATING"
          and valuation.placeholders(v)["intrinsic"] == "withheld"
          and v.conviction == "low")
    v = valuation.compute(synthetic_model(), synthetic_data(ttm_ebitda=100.0, ttm_net_income=150.0))
    check("EBITDA below net income fails a gate", any("EBITDA" in s for s in v.stops))


def test_charts():
    """Revenue history-to-forecast and the valuation range, drawn where the markers sit."""
    peers = [market_data.PeerMultiples(symbol=t, name=t, ev_ebitda=12.0, pe=20.0)
             for t in ("AAA", "BBB")]
    d = synthetic_data(ttm_ebitda=400.0, ttm_net_income=150.0, analyst_target=320.0,
                       analyst_count=12,
                       history=[("FY2024", 900.0, 180.0), ("FY2025", 950.0, None),
                                ("FY2026", 1000.0, 230.0)])
    v = valuation.compute(synthetic_model(downside={"revenue_change": -0.1}), d, peers)
    drawn = charts.build(v, d, 480)
    check("both charts drawn", set(drawn) == {"revenue", "valuation"}, str(drawn))
    texts = lambda drawing: [getattr(s, "text", "") for s in drawing.contents]
    rev = texts(drawn["revenue"])
    check("revenue chart: reported years then forecast, with the divider labelled",
          "FY2024" in rev and "FY2031" in rev and "Forecast" in rev and "Reported" in rev,
          str(rev))
    val = " ".join(texts(drawn["valuation"]))
    check("valuation chart: DCF, cross-check, downside, peers and Street against the price",
          all(x in val for x in ("DCF (base case)", "cross-check", "Downside case",
                                 "EV/EBITDA", "P/E", "Street average", "Current USD 250.00",
                                 f"{v.per_share:,.2f}")), val)
    check("ticks are round and share one format",
          charts._tick_fmt([0, 25, 50])(25) == "25" and charts._tick_fmt([0, 2.5, 5])(2.5)
          == "2.5")

    md = valuation.model_outputs_markdown(v)
    check("model outputs carry the chart markers",
          "<!-- chart:revenue -->" in md and "<!-- chart:valuation -->" in md)
    st = pdf_render._styles()
    flow = pdf_render.markdown_to_flowables(md, st, 480, drawn)
    check("the PDF places both drawings",
          sum(1 for f in flow if f is drawn["revenue"] or f is drawn["valuation"]) == 2)
    bare = pdf_render.markdown_to_flowables(md, st, 480, {})
    check("a marker with nothing to draw leaves nothing behind",
          not any("chart:" in getattr(f, "text", "") for f in bare))

    broken = valuation.compute(synthetic_model(), synthetic_data(history=[("FY2025", "bad", 1)]))
    check("a chart that cannot be drawn is skipped, not fatal",
          "revenue" not in charts.build(broken, synthetic_data(history=[("FY2025", "bad", 1)]),
                                        480))
    check("no valuation, no charts", charts.build(None, d, 480) == {})


async def test_house_wacc(data):
    """The code sets the discount rate from market data; the model cannot pick one."""
    near = lambda a, b, tol=1e-6: abs(a - b) <= tol
    # Equity 2,500 (250 x 10mn shares), debt 1,000: 28.6% debt at market value.
    d = synthetic_data(beta=0.644, beta_source="5-year monthly vs the S&P 500", risk_free=0.05,
                       debt=1000.0, market_cap=2.5e9)
    cost = valuation.cost_of_capital(d)
    beta = 0.67 * 0.644 + 0.33
    ke, kd = 0.05 + beta * 0.045, (0.05 + 0.015) * (1 - 0.21)
    w = 1000 / 3500
    check("house WACC: Blume beta, Treasury, 4.5% ERP, debt at market weight",
          near(cost.wacc, (1 - w) * ke + w * kd) and near(cost.beta, beta)
          and near(cost.debt_weight, w), f"{cost.wacc} {cost.describe()}")
    check("house WACC: the build-up names its sources",
          "10-year Treasury" in cost.describe() and "S&P 500" in cost.describe()
          and "0.64 raw" in cost.describe(), cost.describe())

    premium = valuation.cost_of_capital(d, {"wacc_premium": 0.01,
                                            "wacc_rationale": "single-program risk"})
    check("a premium is added and explained",
          near(premium.wacc, cost.wacc + 0.01) and "single-program risk" in premium.describe())
    big = valuation.cost_of_capital(d, {"wacc_premium": 0.05, "wacc_rationale": "x"})
    check("a premium over 2% is held to 2% and reported",
          near(big.wacc, cost.wacc + 0.02) and any("outside" in x for x in big.warnings))
    silent = valuation.cost_of_capital(d, {"wacc_premium": 0.01})
    check("a premium without a reason is reported",
          any("wacc_rationale" in x for x in silent.warnings), str(silent.warnings))

    blind = valuation.cost_of_capital(synthetic_data(debt=0.0))
    check("no beta or Treasury: market beta and the house risk-free, said so",
          near(blind.beta_raw, 1.0) and near(blind.risk_free, config.RISK_FREE_DEFAULT)
          and "unavailable" in blind.describe(), blind.describe())
    low = valuation.cost_of_capital(synthetic_data(beta=0.1, risk_free=0.01, debt=5000.0))
    check("a rate below the floor is held to 6% and says so",
          near(low.wacc, 0.06) and "held to 6%" in low.describe(), low.describe())

    model = synthetic_model(downside={"revenue_change": -0.1})
    v = valuation.compute(model, d, cost=cost)
    check("the house rate replaces the model's own", near(v.wacc, cost.wacc) and v.cost is cost)
    check("the downside keeps the house rate", v.downside_per_share is not None
          and near(valuation.compute({**model, "wacc": cost.wacc}, d).downside_per_share,
                   v.downside_per_share, 1e-6))
    check("the valuation table shows the build-up",
          f"| Beta | x | {cost.beta:.2f} |" in valuation.intrinsic_valuation_markdown(v))
    check("{{wacc_build}} is a placeholder", valuation.placeholders(v)["wacc_build"]
          == cost.describe())

    config.HOUSE_WACC = True
    try:
        own = json.loads(json.dumps(HEADER_OBJ))
        own["model"]["wacc"] = 0.13
        checked, _ = pitch_mod._finalise(own, BODY, data)
        house = valuation.cost_of_capital(data)
        check("with HOUSE_WACC the model's `wacc` is ignored",
              near(checked.valuation.wacc, house.wacc), f"{checked.valuation.wacc}")
        line = pitch_mod._discount_rate_line(data)
        check("the write is told the rate and not to set one",
              f"{house.wacc:.1%}" in line and "Do not set `wacc`" in line)
    finally:
        config.HOUSE_WACC = False
    check("without HOUSE_WACC the model is told to set it",
          "no discount rate is set" in pitch_mod._discount_rate_line(data))


def test_quote_retry_and_beta():
    """A refused quote summary is asked again; beta comes from price history."""
    months = pd.period_range("2021-09", periods=60, freq="M").to_timestamp()
    market = pd.Series([100 * 1.01 ** i * (1.03 if i % 3 else 0.98) for i in range(60)],
                       index=months)
    rets = market.pct_change().fillna(0)
    stock = 50 * (1 + 2 * rets).cumprod()          # twice the market's monthly moves

    answers = [{"trailingPegRatio": None}, {"trailingPegRatio": None},
               {"currentPrice": 70.0, "targetMeanPrice": 90.0}]

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def info(self):
            return answers.pop(0) if answers else {}

        @property
        def fast_info(self):
            return {"lastPrice": 5.18} if self.symbol == "^TNX" else {}

        def history(self, **kwargs):
            series = market if self.symbol == "^GSPC" else stock
            return pd.DataFrame({"Close": series})

    original = market_data.yf
    market_data.yf = NS(Ticker=FakeTicker)
    try:
        info = market_data._quote_summary("NEE")
        check("a refused quote summary is asked again until it answers",
              info.get("targetMeanPrice") == 90.0 and not answers)
        answers.extend([{}, {}, {}])
        check("three refusals give up with no price",
              "currentPrice" not in market_data._quote_summary("NEE") and not answers)
        beta = market_data.chart_beta("NEE")
        check("beta regressed from monthly prices", beta is not None and abs(beta - 2.0) < 1e-6,
              str(beta))
        check("the 10-year yield read as a fraction", abs(market_data.treasury_yield() - 0.0518)
              < 1e-9)
    finally:
        market_data.yf = original


async def test_market_data_refresh(data):
    """A summary refused at the start is asked for again after research, in place."""
    stale = synthetic_data(summary_refused=True)
    original = market_data.fetch
    try:
        market_data.fetch = lambda symbol: synthetic_data(analyst_target=300.0, beta=1.2)
        await pitch_mod._refresh_market_data(stale)
        check("a refreshed summary updates the data the bot holds",
              stale.analyst_target == 300.0 and stale.beta == 1.2 and not stale.summary_refused)
        still = synthetic_data(summary_refused=True)
        market_data.fetch = lambda symbol: synthetic_data(summary_refused=True, price=1.0)
        await pitch_mod._refresh_market_data(still)
        check("a second refusal leaves the data as it was", still.price == 250.0)
        # 2026-10-05: asked again while research runs; research ending early asks at once.
        market_data.fetch = lambda symbol: synthetic_data(analyst_target=310.0)
        done = asyncio.Event()
        task = asyncio.create_task(pitch_mod._fetch_after("TEST", 3600, done))
        await asyncio.sleep(0)
        started = time.monotonic()
        done.set()
        fresh = await task
        check("the refresh during research does not wait out its delay once research ends",
              fresh is not None and fresh.analyst_target == 310.0
              and time.monotonic() - started < 5)
    finally:
        market_data.fetch = original


async def test_edgar_in_research(data):
    """The EDGAR filings reach the write-up and the model is told not to fetch them."""
    filing = research_tools.Filing("latest 10-Q, filed 2026-07-30",
                                   "https://www.sec.gov/Archives/edgar/data/1/q.htm")
    original = research_tools.read_latest_filings
    research_tools.read_latest_filings = lambda symbol, log_to=None, with_tables=False: [
        (filing, "[latest 10-Q, filed 2026-07-30]\nBACKLOG IS 35.1 GW", "")]
    try:
        client = FakeClient([[delta(content="DONE"), final_chunk()],
                             [delta(content=HEADER + BODY), final_chunk()]])
        await pitch_mod.generate(data, client)
    finally:
        research_tools.read_latest_filings = original
    research = client.completions.calls[0]["messages"][1]["content"]
    write = client.completions.calls[1]["messages"][1]["content"]
    check("research is told which filings are already read",
          "Already read for you" in research and filing.url in research)
    check("the filing is in the write-up's notes as PRIMARY",
          f"[PRIMARY] fetch_page: {filing.url}" in write and "BACKLOG IS 35.1 GW" in write)
    check("the write-up is told the SEC filings override other notes",
          "override every other note" in write)
    calls = client.completions.calls
    check("research explores at TEMPERATURE, the write runs at WRITE_TEMPERATURE",
          calls[0]["temperature"] == config.TEMPERATURE
          and calls[1]["temperature"] == config.WRITE_TEMPERATURE
          and config.WRITE_TEMPERATURE < config.TEMPERATURE,
          f"{calls[0]['temperature']} {calls[1]['temperature']}")


class Patched:
    """Swap module attributes for a test and put them back after."""

    def __init__(self, *triples):
        self.triples = triples

    def __enter__(self):
        self.saved = [(mod, name, getattr(mod, name)) for mod, name, _ in self.triples]
        for mod, name, value in self.triples:
            setattr(mod, name, value)

    def __exit__(self, *exc):
        for mod, name, value in self.saved:
            setattr(mod, name, value)


def gather_patches(cik=1, filings=True, searched=None):
    """Every network call gather.py makes, answered with canned blocks."""
    filing = research_tools.Filing("latest 10-Q, filed 2026-07-30",
                                   "https://www.sec.gov/Archives/edgar/data/1/q.htm",
                                   periodic=True)

    def search(query, log_to=None):
        if searched is not None:
            searched.append(query)
        if log_to:
            log_to.searches += 1
        return (f"Results for '{query}':\n\n1. Social\n   https://www.reddit.com/r/x\n   s\n"
                f"2. Report\n   https://www.example.com/{len(query)}\n   MARKET SNIPPET")

    def fetch(url, log_to=None):
        if log_to:
            log_to.add(url, "page")
        return f"# Page\n{url}\n\nMARKET PAGE TEXT"

    return Patched(
        (research_tools, "cik_for", lambda symbol: cik),
        (research_tools, "read_latest_filings",
         lambda symbol, log_to=None, with_tables=False:
         [(filing, "[latest 10-Q, filed 2026-07-30]\nBACKLOG IS 35.1 GW " + "q" * 9000, "")]
         if filings else []),
        (gather, "submissions", lambda symbol: [{"form": "8-K", "date": date.today().isoformat(),
                                                 "items": "2.06,9.01",
                                                 "url": "https://www.sec.gov/8k.htm"}]),
        (gather, "ten_k_sections", lambda rows, log_to=None: [
            ("filing_section", "10-K, Item 1A Risk Factors", "RISK TEXT " + "r" * 5000)]),
        (gather, "insiders", lambda rows, log_to=None: "Form 4: CEO bought 1,000 shares"),
        (gather, "news", lambda name, symbol, log_to=None: [
            ("news_headlines", "Google News", "- 2026-09-01 Wire: HEADLINE " + "h" * 6000),
            ("fetch_page", "https://www.blog.com/a", "# Blog\nBLOG ARTICLE " + "b" * 6000)]),
        (research_tools, "web_search", search),
        (research_tools, "fetch_page", fetch),
        (config, "SEC_USER_AGENT", "test test@example.com"),
    )


async def test_gather(data):
    """RESEARCH_MODE=gather: notes built by code, filings first and under the cap, one
    gap-fill call that may fail without costing the pitch, and the loop for non-filers."""
    saved = config.RESEARCH_MODE, config.GATHER_GAP_SEARCHES, config.DIGEST_MAX_CHARS
    config.RESEARCH_MODE, config.GATHER_GAP_SEARCHES = "gather", 2
    try:
        searched: list[str] = []
        with gather_patches(searched=searched):
            gap = ('{"queries": ["music streaming market size 2026", '
                   '"Spotify market share 2026", "third"]}')
            client = FakeClient([[delta(content=gap), final_chunk()],
                                 [delta(content=HEADER + BODY), final_chunk()]])
            result = await pitch_mod.generate(data, client)
        calls = client.completions.calls
        asked = calls[0]["messages"][1]["content"]
        notes = calls[1]["messages"][1]["content"]
        order = [m.group(1) for m in re.finditer(r"^## \[(\w+)\]", notes, re.M)]
        check("gather: one gap-fill call without tools, then the write",
              len(calls) == 2 and "tools" not in calls[0], f"{len(calls)} calls")
        check("gather: the gap-fill call sees the contents list and the checklist, not the text",
              "filing_section: 10-K, Item 1A" in asked and "addressable market sizing" in asked
              and "RISK TEXT" not in asked)
        check("gather: at most GATHER_GAP_SEARCHES queries, run as asked",
              sorted(searched) == ["Spotify market share 2026", "music streaming market size 2026"],
              str(searched))
        check("gather: notes run PRIMARY, then SECONDARY, then SNIPPETS",
              order == sorted(order, key=pitch_mod.DIGEST_ORDER.get) and order[0] == "PRIMARY"
              and "SNIPPETS" in order, str(order))
        check("gather: filings, 10-K sections and the 8-K and Form 4 summary are PRIMARY",
              all(f"[PRIMARY] {k}" in notes for k in ("fetch_page: https://www.sec.gov",
                                                       "filing_section", "sec_events"))
              and "material impairment" in notes and "CEO bought" in notes)
        check("gather: the top non-excluded search result is read, the excluded one is not",
              "MARKET PAGE TEXT" in notes
              and "[SECONDARY] fetch_page: https://www.example.com/" in notes
              and "fetch_page: https://www.reddit.com" not in notes)
        check("gather: the path and the 8-K are recorded",
              result.replay["research_path"] == "gather"
              and any(s["url"] == "https://www.sec.gov/8k.htm" for s in result.sources),
              result.replay["research_path"])
        check("gather: research is timed like the loop",
              [s for s, _ in result.replay["timings"]][:1] == ["research"])

        config.DIGEST_MAX_CHARS = 20000
        with gather_patches():
            digest, _ = await gather.gather(
                FakeClient([[delta(content="{}"), final_chunk()]]), data, None,
                {"prompt_tokens": 0, "completion_tokens": 0})
        check("gather: notes stay under DIGEST_MAX_CHARS, the filing kept and snippets cut first",
              len(digest) <= 20000 + 20 and "BACKLOG IS 35.1 GW" in digest
              and "HEADLINE" not in digest, f"{len(digest)} chars")
        config.DIGEST_MAX_CHARS = saved[2]

        for label, reply in (("prose", "I would search for the market size."),
                             ("JSON without queries", '{"searches": 3}'),
                             ("a list of non-strings", "[1, 2, {}]")):
            searched.clear()
            with gather_patches(searched=searched):
                # Every research candidate gives the same bad reply, then the write.
                n = len(pitch_mod._research_candidates())
                client = FakeClient([[delta(content=reply), final_chunk()]] * n
                                    + [[delta(content=HEADER + BODY), final_chunk()]])
                result = await pitch_mod.generate(data, client)
            check(f"gather: a gap-fill reply of {label} means no searches, and the pitch is "
                  "written", searched == [] and result.valuation is not None
                  and "BACKLOG IS 35.1 GW" in client.completions.calls[-1]["messages"][1]["content"])
        check("gather: queries are parsed from fenced JSON, deduplicated and URLs dropped",
              gather.parse_queries('```json\n{"queries": ["a 2026", "a 2026", "https://x.com", '
                                   '"b"]}\n```', 6) == ["a 2026", "b"])

        class Failing:
            def __init__(self):
                self.chat = NS(completions=self)

            async def create(self, **kwargs):
                if kwargs["messages"][0]["content"].startswith("You plan web"):
                    raise RuntimeError("NIM is down")
                return FakeStream([delta(content=HEADER + BODY), final_chunk()])

        searched.clear()
        with gather_patches(searched=searched):
            result = await pitch_mod.generate(data, Failing())
        check("gather: a gap-fill call that fails is skipped and the pitch still written",
              searched == [] and result.valuation is not None
              and result.replay["research_path"] == "gather")

        for label, patch in (("does not file with the SEC", dict(cik=None)),
                             ("has no filings on EDGAR", dict(filings=False))):
            with gather_patches(**patch):
                client = FakeClient([[delta(content="DONE"), final_chunk()],
                                     [delta(content=HEADER + BODY), final_chunk()]])
                result = await pitch_mod.generate(data, client)
            calls = client.completions.calls
            check(f"gather: a company that {label} goes to the research loop",
                  len(calls) == 2 and "tools" in calls[0]
                  and result.replay["research_path"].startswith("loop ("),
                  result.replay["research_path"])
        config.SEC_USER_AGENT = ""
        check("gather: no SEC_USER_AGENT means the loop", gather.loop_reason("SPGI") is not None)

        rows = [{"form": "8-K", "date": date.today().isoformat(), "items": "4.01,9.01",
                 "url": "u1"},
                {"form": "8-K", "date": date.today().isoformat(), "items": "5.02", "url": "u2"},
                {"form": "8-K", "date": "2020-01-01", "items": "2.06", "url": "u3"}]
        flags = gather.red_8k(rows)
        check("gather: serious 8-K items in the window only",
              len(flags) == 1 and "auditor" in flags[0], str(flags))
        text = ("Item 1. Business ... 3\nItem 1A. Risk Factors ... 9\nItem 2. Properties 20\n"
                "Item 1. Business\nWe sell widgets in three segments.\n"
                "Item 1A. Risk Factors\nRegulation may hurt us.\n"
                "Item 1B. Unresolved Staff Comments\nNone.")
        check("gather: the 10-K section is the body, not the table of contents",
              "three segments" in gather.section(text, gather.BUSINESS_HEAD, gather.RISKS_HEAD)
              and gather.section(text, gather.RISKS_HEAD, gather.AFTER_RISKS).endswith("hurt us."))
        class Searcher:
            def text(self, query, max_results=5):
                return [{"href": "https://www.other.com/a"},
                        {"href": "https://www.reuters.com/markets/spgi-deal"}]

        gather._google_blocked_until[0] = 0.0
        with Patched((requests, "get", lambda *a, **k: NS(status_code=429, text="")),
                     (research_tools, "DDGS", Searcher), (config, "TAVILY_API_KEY", "")):
            first = gather._resolve({"link": "https://news.google.com/rss/articles/X",
                                     "title": "S&P Global buys Agusto",
                                     "site": "https://www.reuters.com"})
        check("gather: a 429 from Google stops decoding and the headline is found on its "
              "publisher's site", first == "https://www.reuters.com/markets/spgi-deal"
              and gather._google_blocked_until[0] > time.time(), str(first))
        gather._google_blocked_until[0] = 0.0
        check("gather: the news filter drops 13F notices and price-move write-ups",
              bool(gather.JUNK.search("Vanguard Group Inc. raises stake in S&P Global"))
              and bool(gather.JUNK.search("SPGI shares jumps 3.2% on Tuesday"))
              and not gather.JUNK.search("S&P Global to acquire Agusto"))
    finally:
        config.RESEARCH_MODE, config.GATHER_GAP_SEARCHES, config.DIGEST_MAX_CHARS = saved
        config.SEC_USER_AGENT = ""


async def test_review_when_flagged(data):
    """REVIEW_WHEN=flagged: the reviewer reads only a pitch that looks off."""
    import dataclasses
    saved = config.REVIEW_ENABLED, config.REVIEW_WHEN
    config.REVIEW_ENABLED, config.REVIEW_WHEN = True, "flagged"
    try:
        probe = await pitch_mod.generate(data, FakeClient(
            [[delta(content="DONE"), final_chunk()], [delta(content=HEADER + BODY), final_chunk()],
             [delta(content=verdict()), final_chunk()]]))
        value = probe.valuation.per_share
        near = dataclasses.replace(data, analyst_target=value * 1.1)
        client = FakeClient([[delta(content="DONE"), final_chunk()],
                             [delta(content=HEADER + BODY), final_chunk()]])
        result = await pitch_mod.generate(near, client)
        check("flagged review: a clean pitch near the Street is not reviewed",
              len(client.completions.calls) == 2 and result.review_status.startswith("skipped (")
              and "within 30%" in result.review_status
              and result.replay["review_status"] == result.review_status, result.review_status)
        far = dataclasses.replace(data, analyst_target=value * 2)
        client = FakeClient([[delta(content="DONE"), final_chunk()],
                             [delta(content=HEADER + BODY), final_chunk()],
                             [delta(content=verdict()), final_chunk()]])
        result = await pitch_mod.generate(far, client)
        check("flagged review: a DCF 50% under the Street is reviewed",
              len(client.completions.calls) == 3 and result.review_status == "passed",
              result.review_status)
        reason = pitch_mod._review_trigger(result, ["a problem"], near)
        check("flagged review: problems left by the checks trigger it",
              reason[0] and "problem" in reason[1], str(reason))
        result.valuation.peers = None
        bare = dataclasses.replace(data, analyst_target=None)
        check("flagged review: nothing to judge the DCF by triggers it",
              pitch_mod._review_trigger(result, [], bare)[0])
        config.REVIEW_WHEN = "always"
        check("REVIEW_WHEN=always reviews a clean pitch near the Street",
              pitch_mod._review_trigger(result, [], near)[0])
    finally:
        config.REVIEW_ENABLED, config.REVIEW_WHEN = saved


async def test_progress_checklist():
    """The Telegram progress message: stages tick off with their times, details stay
    inside the running stage, and the message is kept when the pitch ends."""
    import bot

    class Message:
        def __init__(self):
            self.texts = []

        async def edit_text(self, text, parse_mode=None):
            self.texts.append(text)

    msg = Message()
    status = bot.Status(msg, min_interval=0, tick=3600)
    status.symbol = "RTX"
    await status.set("data")
    await status.set("research")
    await status.set("searching", 4)
    await status.set("thinking")
    check("a search count is shown inside the research stage",
          "Research</b>" in msg.texts[-2] and "4 searches" in msg.texts[-2], msg.texts[-2])
    check("model reasoning does not move the checklist back to research",
          "model reasoning" in msg.texts[-1] and msg.texts[-1].count("Research") == 1)
    await status.set("planning")
    await status.set("streaming", 600)
    text = msg.texts[-1]
    check("finished stages are ticked, the running one has its detail, the rest are listed",
          "✅ Market data" in text and "✅ Research" in text
          and "⏳ <b>Building the valuation model</b>" in text and "100 words" in text
          and "▫️ Rendering the PDF" in text and "▫️ Revising" not in text, text)
    await status.set("retrying", 2)
    check("a provider retry is shown on the running stage",
          "model provider busy, retry 2" in msg.texts[-1])
    await status.finish(ok=False)
    check("a failed pitch marks the stage it stopped in and lists nothing after it",
          msg.texts[-1].startswith("❌ <b>RTX</b>")
          and "❌ <b>Building the valuation model</b>" in msg.texts[-1]
          and "▫️" not in msg.texts[-1], msg.texts[-1])
    await asyncio.sleep(0)
    check("the timer stops with the pitch", status._ticker.done())


def test_ppe_rollforward():
    """Capex-heavy companies get D&A from a PP&E roll-forward; asset-light ones keep theirs."""
    head = "| Line item | 2026-03-31 | 2025-03-31 | 2024-03-31 |\n| --- | --- | --- | --- |"
    unit = "(values in millions of reporting currency)"

    def block(capex):
        return "\n".join([
            f"### Income statement {unit}", "", head,
            "| Total Revenue | 1,000,000 | 990,000 | 940,000 |", "",
            f"### Balance sheet {unit}", "", head,
            "| Net PPE | 330,000 | 250,000 | 270,000 |", "",
            f"### Cash flow statement {unit}", "", head,
            f"| Capital Expenditure | -{capex:,} | -86,000 | -33,000 |",
            "| Depreciation | 50,000 | 40,000 | 38,000 |",
            "| Amortization Of Intangibles | 10,000 | 12,000 | 14,000 |", ""])

    revenue = [1_050_000 * 1.06 ** i for i in range(5)]
    capex = [0.06] * 5
    heavy = NS(prompt_block=block(127_000), reporting_currency="CNY")
    rolled = valuation.ppe_rollforward(heavy, "FY2026", revenue, capex)
    check("capex-heavy company gets a roll-forward", rolled is not None)
    da, note = rolled
    # Rate: 50,000/250,000 = 20% and 40,000/270,000 = 14.8%; mean 17.4% of opening PP&E.
    check("first-year depreciation from opening PP&E plus fading amortisation",
          abs(da[0] * revenue[0] - (0.174 * 330_000 + 10_000 * 0.8)) < 600, str(da[0] * revenue[0]))
    # A growing business ends with capex above D&A: the net investment funds the growth.
    check("final-year D&A ends below capex for a growing business",
          da[-1] < da[0] and da[-1] < capex[-1], str(da))
    check("note names the roll-forward", "PP&E roll-forward" in note and "17.4%" in note, note)
    light = NS(prompt_block=block(30_000), reporting_currency="CNY")
    check("asset-light company keeps the writer's D&A",
          valuation.ppe_rollforward(light, "FY2026", revenue, capex) is None)
    check("no roll-forward without PP&E",
          valuation.ppe_rollforward(NS(prompt_block=block(127_000).replace("Net PPE", "X"),
                                       reporting_currency="CNY"), "FY2026", revenue, capex) is None)


BANK_BLOCK = ("| Industry | Banks - Regional |\n\n"
              "### Income statement (values in millions)\n\n| Line item | 2025-12-31 | 2024-12-31 |\n"
              "|---|---|---|\n| Total Revenue | 210 | 200 |\n| Net Interest Income | 0 | 0 |\n"
              "| Net Income Common Stockholders | 150 | 140 |\n\n"
              "### Balance sheet (values in millions)\n\n| Line item | 2025-12-31 | 2024-12-31 |\n"
              "|---|---|---|\n| Total Assets | 12,000 | 11,500 |\n| Common Stock Equity | 1,000 | 960 |\n"
              "| Tangible Book Value | 800 | 760 |\n\n")


def bank_model(**overrides) -> dict:
    """Book of 1,000 growing 3% a year at a 15% ROE on opening book, all from fees at a 25%
    efficiency ratio, no tax, paying out 1 - g / ROE = 80%. With ke 10% and terminal ROE 15%
    the value is book x (ROE - g) / (ke - g) = 1,000 x 0.12 / 0.07 = 1,714.29."""
    books = [1000 * 1.03 ** t for t in range(5)]
    model = {"base_year": "FY2025", "years": ["FY2026", "FY2027", "FY2028", "FY2029", "FY2030"],
             "base_earning_assets": 8000,
             "bank": {"earning_assets": 8000, "nim": 0.0, "efficiency_ratio": 0.25,
                      "non_interest_income": [0.15 * b / 0.75 for b in books],
                      "loans": 0, "credit_cost": 0.0},
             "tax_rate": 0.0, "payout": 0.8, "cost_of_equity": 0.10,
             "capital": {"cet1_ratio": 0.12, "rwa": 8000, "requirement": 0.10, "source": "10-K"},
             "terminal": {"roe": 0.15, "growth": 0.03}}
    model.update(overrides)
    return model


def test_bank_excess_returns():
    import valuation_equity
    data = synthetic_data(prompt_block=BANK_BLOCK, cash=0.0, debt=0.0)
    v = valuation.compute(bank_model(), data)
    em = v.equity
    check("bank: valued on excess returns", v.family == "equity" and em.kind == "bank")
    check("bank: hand-computed value, book x (ROE - g) / (ke - g)",
          abs(em.value - 1000 * 0.12 / 0.07) < 1e-6, f"{em.value}")
    check("bank: per share from 10mn shares", abs(v.per_share - 171.428571) < 1e-4,
          f"{v.per_share}")
    check("bank: the dividend discount gives the same value", abs(em.ddm_value - em.value) < 1e-6)
    check("bank: ROE is computed from the drivers, 15% every year",
          all(abs(y.roe - 0.15) < 1e-9 for y in em.years))
    check("bank: book rolls forward at 3%", abs(em.years[-1].book_close - 1000 * 1.03 ** 5) < 1e-6)
    check("bank: price-implied ROE solves P/B = (ROE - g) / (ke - g)",
          abs(v.implied_margin - (0.03 + 2.5 * 0.07)) < 1e-9, f"{v.implied_margin}")
    check("bank: no CET1 warning when capital stays above the requirement",
          not any("CET1" in w for w in v.warnings), str(v.warnings))
    low = valuation.compute(bank_model(payout=1.4), data)
    check("bank: a payout that breaks the CET1 requirement is sent back",
          any("CET1 falls" in w for w in low.warnings), str(low.warnings))
    rich = valuation.compute(bank_model(terminal={"roe": 0.25, "growth": 0.03}), data)
    check("bank: a terminal ROE far above the forecast and ke needs an argument",
          any("terminal ROE" in w for w in rich.warnings), str(rich.warnings))
    try:
        valuation.compute(bank_model(terminal={"roe": 0.15, "growth": 0.095}), data)
        check("bank: growth within 1pp of ke is refused", False)
    except valuation.ModelInputError:
        check("bank: growth within 1pp of ke is refused", True)
    try:
        valuation.compute(synthetic_model(), data)
        check("bank: an FCFF model for a bank asks for the bank drivers", False)
    except valuation.ModelInputError as exc:
        check("bank: an FCFF model for a bank asks for the bank drivers", "model.bank" in str(exc))
    scen = valuation.compute(bank_model(
        bear={"terminal": {"roe": 0.11}, "rationale": "credit losses"},
        bull={"terminal": {"roe": 0.19}, "rationale": "margin"},
        scenario_probabilities={"bull": 0.25, "base": 0.5, "bear": 0.25}), data)
    check("bank: bear < base < bull, weighted value and rating",
          scen.downside_per_share < scen.per_share < scen.bull_per_share
          and scen.weighted_per_share is not None, str(scen.warnings))
    check("bank: conviction from the ke x terminal grid", scen.conviction is not None)
    md = valuation.model_outputs_markdown(scen)
    check("bank: tables show the earnings build, excess returns and implied P/B",
          all(s in md for s in ("Earnings build", "Excess return", "Implied price to book",
                                "CET1 ratio", "cost of equity by terminal ROE")), md[:300])
    values = valuation.placeholders(scen)
    check("bank: placeholders for ROE, P/B and the cost of equity",
          values.get("roe:FY2027") == "15.0%" and values.get("pb") == "2.50x"
          and values.get("cost_of_equity") == "10.0%", str(sorted(values)[:20]))
    insurer = valuation.compute(bank_model(
        bank=None, insurer={"net_premiums": 1000, "combined_ratio": 0.95,
                            "invested_assets": 3000, "investment_yield": 0.04},
        capital=None), synthetic_data(prompt_block=BANK_BLOCK.replace("Banks - Regional",
                                                                     "Insurance - Life")))
    first = insurer.equity.years[0]
    check("insurer: underwriting plus investment income",
          insurer.equity.kind == "insurer" and abs(first.pretax - (50 + 120)) < 1e-9,
          f"{first.pretax}")
    check("insurer: no CET1 demanded", not any("capital" in w for w in insurer.warnings),
          str(insurer.warnings))
    # ALL 2026-10-12: other segments' revenue typed as other income, value at 7.4x book.
    inflated = valuation.compute(bank_model(
        bank=None, insurer={"net_premiums": 1000, "combined_ratio": 0.95,
                            "invested_assets": 3000, "investment_yield": 0.04,
                            "other_income": 600},
        capital=None, terminal={"roe": 0.30, "growth": 0.03}),
        synthetic_data(prompt_block=BANK_BLOCK.replace("Banks - Regional", "Insurance - Life")))
    check("insurer: other income that is mostly revenue is sent back",
          any("not their revenue" in w for w in inflated.warnings), str(inflated.warnings))
    check("equity method: a value far from the price and the Street is sent back",
          any("from the current price" in w for w in inflated.warnings), str(inflated.warnings))


def test_utility_and_reit():
    # Utility: rate base 2,000 growing 3% a year, half equity, earning 15% on it with no
    # holding-company drag, so book (1,000 + equity added) grows 3% at a 15% ROE on average
    # equity. ke 10%, terminal ROE 15% and growth 3%: roughly 1,000 x 0.12 / 0.07 again,
    # not exactly, because income is earned on the average rate base.
    block = BANK_BLOCK.replace("Banks - Regional", "Utilities - Regulated Electric")
    data = synthetic_data(prompt_block=block, cash=0.0, debt=0.0)
    rb = [2000 * 1.03 ** t for t in range(1, 6)]
    model = {"base_year": "FY2025", "years": ["FY2026", "FY2027", "FY2028", "FY2029", "FY2030"],
             "base_rate_base": 2000,
             "utility": {"rate_base": rb, "equity_ratio": 0.5, "earned_roe": 0.15,
                         "allowed_roe": 0.15},
             "cost_of_equity": 0.10, "terminal": {"roe": 0.15, "growth": 0.03}}
    v = valuation.compute(model, data)
    em = v.equity
    check("utility: routed to the equity method as a utility", em.kind == "utility")
    check("utility: book grows with the equity share of the rate base",
          abs(em.years[-1].book_close - (1000 + 0.5 * (rb[-1] - 2000))) < 1e-6)
    check("utility: the dividend discount gives the same value", abs(em.ddm_value - em.value) < 1e-6)
    check("utility: close to book x (ROE - g) / (ke - g)",
          abs(em.value / (1000 * 0.12 / 0.07) - 1) < 0.03, f"{em.value}")
    heavy = valuation.compute({**model, "utility": {**model["utility"],
                                                    "rate_base": [2000 * 1.25 ** t for t in range(1, 6)],
                                                    "earned_roe": 0.09}}, data)
    check("utility: a capital plan earnings cannot fund shows new equity issued",
          any(y.distributions < 0 for y in heavy.equity.years)
          and any("new equity" in n for n in heavy.notes), str(heavy.notes))
    # Reported net income is 150 (BANK_BLOCK); a rate base earning 2,000 x 0.5 x 5% = 50 leaves
    # 100 unexplained, as NEE's model of 2026-10-12 left out its renewables business.
    partial = valuation.compute({**model, "utility": {**model["utility"], "earned_roe": 0.05,
                                                      "allowed_roe": 0.05}}, data)
    check("utility: a base year that does not explain reported earnings is sent back",
          any("unexplained" in w for w in partial.warnings), str(partial.warnings))
    over = valuation.compute({**model, "utility": {**model["utility"], "allowed_roe": 0.10}}, data)
    check("utility: earned above allowed needs an argument",
          any("allowed" in w for w in over.warnings), str(over.warnings))

    # REIT: NOI 100 growing 3%, no G&A, interest or capex, nothing invested: AFFO is all
    # distributable. Terminal: growth 3% funded by new investment at 15%, so 80% is paid out.
    block = BANK_BLOCK.replace("Banks - Regional", "REIT - Industrial")
    data = synthetic_data(prompt_block=block, cash=50.0, debt=550.0)
    noi = [100 * 1.03 ** t for t in range(5)]
    model = {"base_year": "FY2025", "years": ["FY2026", "FY2027", "FY2028", "FY2029", "FY2030"],
             "reit": {"noi": noi, "g_and_a": 0, "interest_expense": 0, "maintenance_capex": 0,
                      "net_investment": 0, "debt_funded": 0.5, "acquisition_cap_rate": 0.06,
                      "cap_rate": 0.05,
                      "cap_rate_source": "CBRE survey"},
             "cost_of_equity": 0.10, "terminal": {"investment_return": 0.15, "growth": 0.03}}
    v = valuation.compute(model, data)
    em = v.equity
    pv = sum(n / 1.1 ** (t + 1) for t, n in enumerate(noi))
    tv = noi[-1] * 1.03 * 0.8 / 0.07 / 1.1 ** 5
    check("reit: value is distributable AFFO discounted at the cost of equity",
          em.kind == "reit" and abs(em.value - (pv + tv)) < 1e-6, f"{em.value} vs {pv + tv}")
    check("reit: NAV is forward NOI at the cap rate less net debt",
          abs(em.nav - (100 / 0.05 - 500)) < 1e-6, f"{em.nav}")
    md = valuation.model_outputs_markdown(v)
    check("reit: tables show AFFO, P/AFFO, NAV and the implied cap rate",
          all(s in md for s in ("AFFO per share", "Implied P/AFFO", "NAV:", "Cap rate implied")),
          md[:400])
    values = valuation.placeholders(v)
    check("reit: placeholders for P/AFFO, NAV and the cap rate",
          {"p_affo", "nav_per_share", "cap_rate", "implied_cap_rate"} <= set(values))
    funded = valuation.compute({**model, "reit": {**model["reit"], "net_investment": 40}}, data)
    rate = config.RISK_FREE_DEFAULT + config.DEBT_SPREAD
    y0, y1 = funded.equity.years[0], funded.equity.years[1]
    check("reit: the equity-funded half of investment comes out of distributions",
          abs(y0.distributions - (noi[0] + 1.2 - 0.5 * 20 * rate - 20)) < 1e-9,
          f"{y0.distributions}")
    check("reit: new investment earns its cap rate and its debt pays interest",
          abs(y1.lines["acquired_noi"] - (40 * 0.06 + 20 * 0.06)) < 1e-9
          and abs(y1.lines["new_debt_interest"] - (20 + 10) * rate) < 1e-9)
    computed = valuation.compute({**model, "terminal": {"growth": 0.03}}, data)
    check("reit: the terminal return is the cap rate levered at the debt share",
          abs(computed.equity.terminal_roe - (0.06 - 0.5 * rate) / 0.5) < 1e-12,
          f"{computed.equity.terminal_roe}")
    check("reit: placeholders by their path in the model",
          "reit.noi:FY2026" in valuation.placeholders(funded))


def test_distress():
    base = valuation.compute(synthetic_model(), synthetic_data())
    # Failure: 30% chance the equity is worth nothing.
    failed = valuation.compute(synthetic_model(
        failure={"probability": 0.3, "equity_if_failed": 0, "source": "B- default table"}),
        synthetic_data())
    check("distress: value is survival-weighted",
          abs(failed.per_share - 0.7 * base.per_share) < 1e-6
          and abs(failed.going_concern_per_share - base.per_share) < 1e-6)
    check("distress: the what-ifs carry the same weighting",
          abs(valuation.value_at(failed, failed.wacc, growth=failed.growth) - failed.per_share)
          < 1e-6)
    # NOL: 10,000 of losses carried forward leaves every year untaxed.
    untaxed = valuation.compute(synthetic_model(nol=10000, nol_source="10-K tax note"),
                                synthetic_data())
    check("distress: losses carried forward shelter profit from tax",
          all(abs(r.nopat - r.ebit) < 1e-9 for r in untaxed.rows)
          and untaxed.per_share > base.per_share)
    # Sales to capital: revenue grows 100 in the first year at 2.0, so capex is D&A + 50.
    s2c = valuation.compute(synthetic_model(sales_to_capital=2.0, capex_pct_revenue=None),
                            synthetic_data())
    first = s2c.rows[0]
    check("distress: sales-to-capital sets reinvestment",
          abs(first.capex - (first.da + 50)) < 1e-9 and first.nwc_change == 0, f"{first}")
    # Burn: a -20% margin with no cash on hand must be funded with new shares.
    burning = valuation.compute(synthetic_model(ebit_margin=-0.2, nol=0),
                                synthetic_data(cash=0.0, debt=0.0))
    check("distress: a cash shortfall is raised as new shares",
          burning.inputs["raised"] > 0 and burning.inputs["new_shares"] > 0
          and any("new shares" in n for n in burning.notes), str(burning.notes))
    block = ("### Income statement (values in millions)\n\n| Line item | 2025-12-31 |\n"
             "|---|---|\n| Total Revenue | 1,000 |\n| Operating Income | -50 |\n\n")
    flagged = valuation.compute(synthetic_model(), synthetic_data(prompt_block=block))
    check("distress: a loss-maker without failure inputs is asked for them",
          any("model.failure" in w for w in flagged.warnings)
          and any("model.nol" in w for w in flagged.warnings), str(flagged.warnings))


def test_cycle():
    history = [("2025-12-31", 1000e6, 165e6), ("2024-12-31", 1000e6, 200e6),
               ("2023-12-31", 1000e6, 190e6), ("2022-12-31", 1000e6, 150e6),
               ("2021-12-31", 1000e6, 120e6), ("2020-12-31", 1000e6, 80e6)]
    block = ("| Industry | Farm & Heavy Construction Machinery |\n\n"
             + market_data.cycle_table(history))
    margins = market_data.cycle_margins(block)
    check("cycle: SEC margin history read back from the block",
          [m for _, m in margins] == [0.165, 0.2, 0.19, 0.15, 0.12, 0.08], str(margins))
    data = synthetic_data(prompt_block=block)
    mid = valuation.compute(synthetic_model(ebit_margin=0.15), data)
    check("cycle: the base year's place in the cycle is shown",
          mid.cycle and abs(mid.cycle["mean"] - 0.1508333) < 1e-6
          and "**Cycle:**" in valuation.checks_notes_markdown(mid)
          and not any("cycle" in w for w in mid.warnings), str(mid.warnings))
    peak = valuation.compute(synthetic_model(ebit_margin=0.22), data)
    check("cycle: a terminal margin above the cycle's best is sent back",
          any("above the best" in w for w in peak.warnings), str(peak.warnings))
    argued = valuation.compute(synthetic_model(
        ebit_margin=0.22, cycle_rationale="the services mix has doubled since 2020, lifting "
        "trough margins by four points"), data)
    check("cycle: an argued structural change is a note, not a rewrite",
          not any("above the best" in w for w in argued.warnings)
          and any("above the best" in n for n in argued.notes))
    top = market_data.cycle_table([("2025-12-31", 1000e6, 200e6)] + history[1:])
    topped = valuation.compute(synthetic_model(ebit_margin=0.15), synthetic_data(
        prompt_block="| Industry | Farm & Heavy Construction Machinery |\n\n" + top))
    check("cycle: a base year at the top must declare its cycle position",
          any("cycle_position" in w for w in topped.warnings), str(topped.warnings))
    oil = valuation.compute(synthetic_model(ebit_margin=0.15), synthetic_data(
        prompt_block=block.replace("Farm & Heavy Construction Machinery", "Oil & Gas E&P")))
    check("cycle: a commodity producer is asked for its price deck",
          any("price_deck" in w for w in oil.warnings), str(oil.warnings))
    plain = valuation.compute(synthetic_model(), synthetic_data(
        prompt_block=block.replace("Farm & Heavy Construction Machinery", "Software - Application")))
    check("cycle: six years of history alone do not make a software company cyclical",
          plain.cycle is None)


def test_nvda_2026_10_12():
    """What NVDA's pitch of 2026-10-12 shipped with: EBITDA carrying stake gains, a price
    driver read as units, and a bold lead printing n.a."""
    cols = pd.to_datetime(["2026-07-31", "2026-04-30"])
    income = pd.DataFrame({cols[0]: [96221.0, 63734.0, 1127.0, 72861.0],
                           cols[1]: [81615.0, 53536.0, 997.0, 71002.0]},
                          index=["Total Revenue", "Operating Income", "Reconciled Depreciation",
                                 "EBITDA"])
    built = market_data.operating_ebitda(income)
    check("EBITDA is operating income plus D&A, without gains below operating income",
          list(built.loc["EBITDA"]) == [64861.0, 54533.0]
          and list(income.loc["EBITDA"]) == [72861.0, 71002.0], str(list(built.loc["EBITDA"])))
    bank = income.drop(index="Operating Income")
    check("a statement without operating income keeps Yahoo's EBITDA",
          market_data.operating_ebitda(bank) is bank)

    model = header_obj(1000)["model"]
    model["drivers"]["Ad ASP ($)"] = {"unit": "$", "base": 350, "segment": "Ad-Supported",
                                      "values": [380, 400, 420, 440, 460]}
    v = valuation.compute(model, synthetic_data(revenue=1000.0))
    build = valuation.volume_build_table(v)
    check("a price driver is not divided into sales per unit",
          "Ad-Supported sales per unit" not in build and "Premium sales per unit" in build
          and "Ad-Supported sales (projected directly)" in build, build)

    fx = facts.Facts({}, "", "")
    text = ("**Margins.** Gross margin peaks early. **Operating margin falls as R&D scales "
            "({{assume:opex_rnd}}).** FY2027 operating margin {{ebit_margin:FY2027}}; then "
            "it fades. The signpost is OpEx.")
    out, found = facts.strip_misused(text, fx, assumptions={})
    check("a bold lead citing a missing assumption goes with its sentence",
          "opex_rnd" not in out and "**Margins.**" in out and "The signpost is OpEx." in out
          and found, out)

    # From the review of the 2026-10-12 replays.
    worked = "FY2025 earning assets were ~$4.0T (derived from $95.4bn NII / 2.39% NIM);"
    check("a capital T is trillions in a worked figure",
          provenance._arithmetic(worked) == []
          and provenance._arithmetic(worked.replace("4.0T", "5.0T")), worked)
    check("a lowercase t is not trillions",
          [f.text for f in provenance.figures("a 13t stage and US$1.2T of assets")]
          == ["US$1.2T"])
    orphan = ("Signpost: BIS rule updates; China revenue line in quarterly reports "
              "({{segment.Compute & Networking:FY2028}}). [#D1]\nI project revenue "
              "({{revenue:FY2030}}). The base case is {{base_intrinsic}} ({{base_upside}}). "
              "Borrowing would be EPS-dilutive ({{buyback_eps_change}}).")
    kept = consistency.drop_orphan_placeholders(orphan)
    check("a bracketed placeholder its sentence does not name is dropped",
          "quarterly reports. [#D1]" in kept and "({{revenue:FY2030}})" in kept
          and "({{base_upside}})" in kept and "({{buyback_eps_change}})" in kept, kept)
    leads = ("- **Prices are rising and subscribers are staying:** (a) Data Center grew.\n\n"
             "- **Prices are rising and subscribers are staying:** (a) Racks ship.\n\n"
             "**Signpost to watch:** one.\n\n**Signpost to watch:** two.")
    out = consistency.drop_repeated_leads(leads)
    check("a long run-in lead on several paragraphs is dropped, a short one kept",
          "subscribers are staying" not in out and "- (a) Racks ship." in out
          and out.count("**Signpost to watch:**") == 2, out)
    pattern = provenance._name_pattern("Productivity and Business Processes")
    check("a segment name matches with & for and",
          bool(pattern.search("**Productivity & Business Processes (PBP):** base $140bn")))


def test_stakes_and_parts():
    base = valuation.compute(synthetic_model(), synthetic_data())
    held = valuation.compute(synthetic_model(), synthetic_data(equity_investments=300.0))
    check("stakes: book equity investments are added to equity, 300mn = 30 a share",
          abs(held.per_share - base.per_share - 30.0) < 1e-6)
    quote = market_data.PeerMultiples("ANT", "ANT", None, None, price=20.0, currency="USD",
                                      stake=True)
    stake = {"name": "Ant Group", "ticker": "ANT", "shares": 10, "carrying_value": 100,
             "source": "20-F note 12"}
    listed = valuation.compute(synthetic_model(listed_stakes=[stake]),
                               synthetic_data(equity_investments=300.0), [quote])
    check("stakes: a listed stake at market replaces its carrying value (300 - 100 + 200)",
          abs(listed.investments - 400.0) < 1e-6 and listed.peers is None,
          f"{listed.investments}")
    scen = valuation.compute(synthetic_model(listed_stakes=[stake], bear={"ebit_margin": 0.1},
                                             bull={"ebit_margin": 0.3}),
                             synthetic_data(equity_investments=300.0), [quote])
    plain = valuation.compute(synthetic_model(bear={"ebit_margin": 0.1}, bull={"ebit_margin": 0.3}),
                              synthetic_data(equity_investments=300.0))
    check("stakes: the bear case carries the stake too",
          abs(scen.downside_per_share - plain.downside_per_share - 10.0) < 1e-6)
    unpriced = valuation.compute(synthetic_model(listed_stakes=[stake]), synthetic_data())
    check("stakes: an unpriced stake is flagged, not guessed",
          any("could not be priced" in w for w in unpriced.warnings))
    parts = {"A": {"metric": "EBITDA", "amount": 100, "year": "FY2027", "multiple": 10,
                   "source": "peers"},
             "B": {"metric": "EBITDA", "amount": 110, "year": "FY2028", "multiple": 10,
                   "source": "peers"}}
    sotp = valuation.compute(synthetic_model(parts=parts,
                                             corporate_costs={"amount": 20, "multiple": 10}),
                             synthetic_data())
    ev = 1000 + 1100 / 1.1 - 200
    check("parts: segments at their multiples, later years discounted, less corporate costs",
          abs(sotp.parts["ev"] - ev) < 1e-6
          and abs(sotp.parts["per_share"] - (ev + 500 - 200) * 1e6 / 1e7) < 1e-6,
          f"{sotp.parts}")
    check("parts: shown beside the DCF", "Sum of the Parts" in valuation.model_outputs_markdown(sotp))
    conglomerate = valuation.compute(synthetic_model(), synthetic_data(
        prompt_block="| Industry | Conglomerates |\n"))
    check("parts: a conglomerate is asked for its parts",
          any("model.parts" in w for w in conglomerate.warnings))


def test_leverage_path():
    cost = valuation.CostOfCapital(wacc=0.09, risk_free=0.04, risk_free_source="test",
                                   beta_raw=1.0, beta=1.0, beta_source="test", erp=0.05,
                                   cost_of_equity=0.09, cost_of_debt=0.04, debt_weight=0.0)
    flat = valuation.compute(synthetic_model(), synthetic_data(), cost=cost)
    same = valuation.compute(synthetic_model(debt_weight=0.0, debt_weight_source="10-K"),
                             synthetic_data(), cost=cost)
    check("leverage: an unchanged structure discounts as before",
          abs(flat.per_share - same.per_share) < 1e-9)
    levered = valuation.compute(synthetic_model(debt_weight=[0.1, 0.2, 0.3, 0.4, 0.4],
                                                debt_weight_source="recap plan"),
                                synthetic_data(), cost=cost)
    path = levered.inputs["wacc_path"]
    # Relevered at 40%: beta 1 x (1 + 0.79 x 0.4/0.6) = 1.5267; ke 11.63%; WACC 8.58%.
    check("leverage: beta relevered and WACC rebuilt each year",
          abs(path[-1] - (0.6 * (0.04 + 1.526667 * 0.05) + 0.4 * 0.04)) < 1e-5
          and path[0] > path[-1] and abs(levered.wacc - path[-1]) < 1e-12, str(path))
    check("leverage: the what-ifs keep the path",
          abs(valuation.value_at(levered, levered.wacc, growth=levered.growth)
              - levered.per_share) < 1e-6)


def test_valuation_route():
    import valuation_route

    def block(industry, assets=None, revenue=None):
        out = f"| Industry | {industry} |\n\n"
        if revenue is not None:
            out += ("### Income statement (values in millions)\n\n| Line item | 2025-12-31 |\n"
                    f"|---|---|\n| Total Revenue | {revenue:,.0f} |\n\n")
        if assets is not None:
            out += ("### Balance sheet (values in millions)\n\n| Line item | 2025-12-31 |\n"
                    f"|---|---|\n| Total Assets | {assets:,.0f} |\n\n")
        return out

    def method(text):
        return valuation_route.route(synthetic_data(prompt_block=text)).method

    check("route: a regional bank gets the equity method", method(block("Banks - Regional")) == "equity")
    check("route: a life insurer gets the equity method", method(block("Insurance - Life")) == "equity")
    check("route: an insurance broker gets the DCF", method(block("Insurance Brokers")) == "fcff")
    check("route: a REIT gets the equity method", method(block("REIT - Retail")) == "equity")
    check("route: a regulated utility gets the equity method",
          method(block("Utilities - Regulated Electric")) == "equity")
    check("route: an independent power producer keeps the DCF",
          method(block("Utilities - Independent Power Producers")) == "fcff")
    check("route: a software company gets the DCF",
          method(block("Software - Application")) == "fcff")
    check("route: a lender in Credit Services gets the equity method (Capital One, ~12x)",
          method(block("Credit Services", assets=480_000, revenue=40_000)) == "equity")
    check("route: a card network in Credit Services gets the DCF (Visa, ~2.6x)",
          method(block("Credit Services", assets=94_000, revenue=36_000)) == "fcff")
    check("route: a bank in Capital Markets gets the equity method (Goldman, ~30x)",
          method(block("Capital Markets", assets=1_600_000, revenue=53_000)) == "equity")
    check("route: Capital Markets with no balance sheet gets the equity method",
          method(block("Capital Markets")) == "equity")


def test_noted_citations():
    """BABA's outline of 2026-10-12: a real block cited with a note after it, or under
    another tier tag, is the block; a source not in the notes is still refused."""
    digest = ("## [PRIMARY] fetch_page: https://www.sec.gov/a/baba-20260331.htm\n\ntext\n\n"
              "## [SNIPPETS] web_search: Alibaba Cloud market share China Canalys IDC AWS\n\nx\n")
    check("a citation with a note after it matches its block", outline_mod.source_matches(
        "## [PRIMARY] fetch_page: https://www.sec.gov/a/baba-20260331.htm (CEO letter: +40%)",
        digest))
    check("a citation under another tier tag matches its block", outline_mod.source_matches(
        "## [SECONDARY] web_search: Alibaba Cloud market share China Canalys IDC (36% share)",
        digest))
    check("a source not in the notes is still refused", not outline_mod.source_matches(
        "## [SECONDARY] fetch_page: https://example.com/nothing (a note)", digest))


def test_health_endpoint():
    """Render's port scan and the keep-awake cron both depend on this answering."""
    client = TestClient(health.app)
    check("health answers GET", client.get("/health").json() == {"status": "ok"})
    check("health answers HEAD", client.head("/health").status_code == 200)

    # The real server has to come up on a background thread, because the main
    # thread belongs to run_polling.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    health.serve_in_background(port, host="127.0.0.1")
    served = False
    for _ in range(50):
        try:
            served = requests.get(f"http://127.0.0.1:{port}/health", timeout=1).ok
            break
        except requests.ConnectionError:
            time.sleep(0.1)
    check("health served from a background thread", served)


def test_live_search():
    log = research_tools.SourceLog()
    out = research_tools.web_search("Spotify Q2 2026 premium subscribers", log)
    check("live search returns results", "http" in out and len(out) > 200, f"{len(out)} chars")
    check("live search logs sources", len(log.sources) > 0, f"{len(log.sources)} sources")

    if log.sources:
        page = research_tools.fetch_page(log.sources[0]["url"], log)
        check("live fetch returns text", len(page) > 200, f"{len(page)} chars")


async def main() -> int:
    # A card quoted in a failure carries emoji the Windows console cannot print.
    sys.stdout.reconfigure(errors="replace")
    # The test PDF and the deliberate parse-failure dumps go to a scratch folder, not out/.
    with tempfile.TemporaryDirectory() as scratch:
        config.OUT_DIR = Path(scratch)
        return await run_checks()


async def run_checks() -> int:
    # Scripted research ends after a search or two; the floors get their own tests.
    config.MIN_SEARCHES = 0
    # The scripted fakes stand in for NIM and Gemini; a real OpenRouter key in .env must
    # not send a test review to the live service.
    config.OPENROUTER_API_KEY = ""
    if config.REVIEW_MODEL.startswith(("openrouter/", "claude-")):
        config.REVIEW_MODEL = "openai/gpt-oss-20b"
        config.REVIEW_TIMEOUT_S = 300.0
    config.REVIEW_FALLBACK_MODELS = [m for m in config.REVIEW_FALLBACK_MODELS
                                     if not m.startswith("openrouter/")]
    config.MIN_PRIMARY_FETCHES = 0
    # Scripted clients answer a fixed number of calls; the reviewer and the two-pass
    # write get their own tests.
    # No test reaches EDGAR; the EDGAR tests declare an agent themselves.
    # Scripted fixtures carry their own `wacc`; the house rate gets its own test.
    config.HOUSE_WACC = False
    config.QUOTE_RETRY_DELAYS_S = [0.0, 0.0]
    config.SEC_USER_AGENT = ""
    config.RESEARCH_MODE = "model"
    config.STRUCTURED_FACTS = False
    config.REVIEW_ENABLED = False
    config.TWO_PASS_WRITE = False
    # The synthetic models replace D&A with equal capex and grow forever: the reinvestment
    # rule is tested on its own in test_returns_and_leverage.
    config.REINVESTMENT_CHECK = False
    # The hand-checked DCF figures below are on a plain perpetuity; the glide gets its own
    # test.
    config.GLIDE_YEARS = 0
    # Keep every call on the fake client: a Gemini key in .env would otherwise send
    # research to Google's real endpoint. The Gemini paths get their own tests.
    config.GEMINI_API_KEY = ""
    # The scripted fakes are OpenAI-compatible: keep the writer on a NIM model, whatever
    # .env says. The Claude Code route gets its own test.
    config.MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
    config.NVIDIA_API_KEY = config.NVIDIA_API_KEY or "test-key"
    config.RESEARCH_MODEL = config.MODEL

    spot_replays = [p for p in replay.replay_files() if p.name.startswith("SPOT_")]
    if spot_replays:
        data = replay.load(spot_replays[-1])["data"]
        data.summary_refused = False
        if not data.analyst_target:
            data.analyst_target = round(data.price * 1.12, 2)
        check("saved SPOT market-data fixture loaded", data.price and data.price > 0,
              f"{data.symbol} @ {data.price}")
    else:
        data = synthetic_data(symbol="SPOT", name="Spotify Technology S.A.",
                              exchange="NYSE", currency="USD", reporting_currency="EUR",
                              fx=1.1, price=600.0, market_cap=100000.0,
                              shares_outstanding=170.0, revenue=17000.0,
                              cash_debt_known=True)
        check("synthetic SPOT market-data fixture loaded", data.price and data.price > 0)
    check("reporting currency and FX are valid",
          bool(data.reporting_currency) and data.fx > 0,
          f"{data.reporting_currency}->{data.currency} {data.fx}")
    check("bridge inputs pulled", data.cash_debt_known and data.revenue and data.revenue > 0)
    check("bridge inputs shown to the model",
          "Valuation inputs used by the model" in data.prompt_block)
    set_fixtures(data)

    test_valuation_math()
    test_glide_and_implied_growth()
    test_returns_and_leverage()
    test_shipped_2026_10_07()
    test_shipped_2026_10_11()
    test_spot_model_and_tables()
    test_consistency()
    test_placeholders_and_tables()
    test_share_count_and_cover(data)
    test_downside_drivers_and_typed_beside(data)
    test_beta_floor_and_finance_arm()
    test_shipped_pitch_errors(data)
    test_terminal_and_downside(data)
    test_shipped_2026_09_25(data)
    test_shipped_2026_09_26()
    test_provenance(data)
    test_saved_pitches()
    test_growth_rates_and_repeats()
    test_filing_tables()
    test_sec_xbrl()
    test_facts_mode(data)
    test_figure_sheet_and_outline(data)
    test_v1_prompt_repairs()
    test_review_evidence_and_history(data)
    test_model_rules()
    test_claim_types()
    test_revenue_build(data)
    test_conglomerate(data)
    test_sensitivity_and_peers()
    await test_write_checks(data)
    await test_two_pass_write(data)
    await test_outline_step(data)

    result = await test_tool_loop(data)
    await test_request_shape(data)
    await test_budget_exhaustion(data)
    await test_research_floor(data)
    await test_primary_floor(data)
    await test_tool_timeout(data)
    test_source_quality()
    await test_review(data)
    await test_parsing_resilience(data)
    await test_transient_retry(data)
    await test_write_fallback(data)
    await test_research_on_gemini(data)
    await test_peer_lookup_in_pitch(data)
    await test_free_tier_resilience(data)
    test_tool_error_handling()
    test_rendering(result, data)
    test_street_view(result, data)
    test_symbol_resolution()
    test_blocked_quote_summary()
    test_peer_dkk_currency_conversion()
    test_tavily_search()
    test_sec_fetch()
    test_edgar_filings()
    test_foreign_latest_filings_include_annual_and_6k()
    await test_edgar_in_research(data)
    await test_gather(data)
    await test_review_when_flagged(data)
    test_gates_and_reverse()
    test_private_and_primary_sources()
    test_charts()
    await test_house_wacc(data)
    test_quote_retry_and_beta()
    await test_market_data_refresh(data)
    test_ppe_rollforward()
    test_noted_citations()
    test_valuation_route()
    test_bank_excess_returns()
    test_utility_and_reit()
    test_distress()
    test_cycle()
    test_stakes_and_parts()
    test_nvda_2026_10_12()
    test_leverage_path()
    test_health_endpoint()
    await test_progress_checklist()
    await test_claude_code_route(data)

    if "--live-search" in sys.argv:
        print("Running live search checks…")
        test_live_search()

    for line in PASSES:
        print(f"  PASS  {line}")
    for line in FAILS:
        print(f"  FAIL  {line}")
    print(f"\n{len(PASSES)} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
