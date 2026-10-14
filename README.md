# Stock Thesis Bot

I vibe-coded a Telegram bot using Claude Opus 5.5, in conjunction with inspiration from many other repos on a similar topic, to build a full buy-side investment pitch.

## How it works

```
ticker  ->  yfinance     reported financials, price, cash, debt, FX   (authoritative)
        ->  SEC/XBRL      period-reconciled fact database + conflicts (code)
        ->  NVIDIA NIM   web research + checked claim map             (evidence)
        ->  NVIDIA NIM   thesis + DCF *assumptions*                   (judgement)
        ->  valuation    DCF, bridge, per-share value, rating         (arithmetic, in code)
        ->  consistency  prose checked against the computed model     (re-ask twice)
        ->  reviewer     a second model challenges the argument       (revise once)
        ->  reportlab    research-note PDF                            (delivery)
```

The split matters: code reconciles reported figures by source and period, and computes the
valuation; the model supplies judgement and the argument. A checked claim map comes before
the full V1 prose, which retains the existing style and report depth.

**Valuation coverage:** Enterprise DCF is for non-financial companies. Banks, insurers
and mortgage-finance companies need an equity-based sector model because deposits, financial
liabilities and receivables are operating items. The bot still researches and delivers a
business and investment brief for those companies, with DCF valuation and rating clearly
marked unavailable until a sector model exists.

**The model never does the maths.** 

**Two cross-checks sit beside the DCF**, both computed in code. A sensitivity grid shows the
per-share value across WACC and terminal growth (or exit multiple), since with a five-year
forecast most of the value sits in the terminal assumption. A peer check values the company at
the median EV/EBITDA and P/E of the three to five peers the model names, with each peer's
multiples computed from its chart-data market cap and reported statements rather than Yahoo's
quote summary, which Render cannot reach. Peers never set the rating; a DCF more than 50% from
the peer value is passed to the reviewer to question. EBITDA is not like-for-like everywhere:
Yahoo's EBITDA for Netflix adds back content amortisation, so its EV/EBITDA reads low, and
P/E is the better comparison there.

**Then the prose is checked.** `consistency.py` reads the finished text and reports any stated
intrinsic price, upside, rating or forecast-year revenue, EBIT or free cash flow that the
computed model contradicts. 

**Then a reviewer challenges it.** A second model (`REVIEW_MODEL`, from a different family than
the writer) reads the pitch as a PM would: unsupported assumptions, figures from weak sources, an
unexplained gap to the sell-side consensus, a straw-man counter-case, stale facts. High-severity
issues go back to the writer for one paragraph repair, which is kept only if it still passes the
code checks. Unresolved high-severity issues mark the report **DRAFT** and appear as
**Reviewer notes** at the end of the PDF. A reviewer that times out or answers badly is skipped,
never waited on.

Research is done through **client-side tools**, not a hosted search product. 

| File | Role |
| --- | --- |
| `bot.py` | Telegram handlers, progress status, orchestration |
| `market_data.py` | yfinance pull, symbol resolution, valuation bridge inputs, the MARKET DATA prompt block |
| `pitch.py` | The NIM calls: research loop, write with re-ask, review and revision, response parsing |
| `valuation.py` | The DCF from the model's inputs, the rating, the generated tables and placeholders |
| `consistency.py` | Checks the prose against the computed model |
| `provenance.py` | Sends back any figure in the prose that no source accounts for |
| `replay.py` | Saves each pitch's inputs and raw writes; replays the checks on them |
| `research_tools.py` | `web_search` and `fetch_page`, source filtering and primary-source detection |
| `pdf_render.py` | Markdown subset to a formatted PDF |
| `tg_format.py` | Summary card and message splitting |
| `health.py` | `/health` endpoint for running as a Render web service |
| `prompts/analyst_system.md` | Analyst persona, section skeleton, `model` inputs, placeholders |
| `prompts/reviewer_system.md` | The reviewing PM: what to challenge, severity, verdict format |
| `prompts/exemplar.md` | The style exemplar (replace with your own pitch) |
| `test_offline.py` | Checks the pipeline without spending an API call |

## Revalue with your own assumptions

Each saved replay can produce an editable JSON assumptions file. This is a local,
deterministic calculation: it does not call the research or writing models, and it keeps the
saved run's price, shares, cash, debt, FX and peer data fixed while you change the forecast.

In a private Telegram chat, the bot also sends an **Edit valuation assumptions** button after
the PDF. It opens a mobile-friendly editor. Submit your changes to calculate a new valuation
comparison in the chat; the PDF already sent remains unchanged. WACC is shown as a percentage
(for example, `8.47`) and converted back to a decimal for the model. The link is a short-lived,
unguessable bearer link tied to the Telegram account that requested the pitch, so it also works
in Telegram clients that do not expose Web App `initData`. Keep it private. On Render, the
service's HTTPS URL is detected automatically. On another host, set `WEB_APP_BASE_URL` to the
public HTTPS origin before starting the bot.

From the `stock thesis v1` directory:

```powershell
python replay.py assumptions "..\path\to\AAPL_20261007T152910.json.gz"
```

The template is written to `replays/assumptions/AAPL_assumptions.json`. Edit that file, then
compare the new value with the original:

```powershell
python replay.py revalue "..\path\to\AAPL_20261007T152910.json.gz" replays/assumptions/AAPL_assumptions.json
```

The file contains the selected draft's full `model`. You can adjust yearly segment revenue,
EBIT margins, tax, D&A, capex, working capital, terminal growth or exit multiple, scenario
inputs and probabilities, and WACC. Revenue is in reporting-currency millions; rates are
fractions (`0.10` means 10%). The generated template pins the effective WACC into the editable
model, so your chosen rate is honored instead of being replaced by the house rate. Only the
`model` object is used as assumptions; edits to market data are ignored. The output shows the
original and custom values, ratings, forecast CAGR, and any valuation warnings.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS/Linux

cp .env.example .env
```

Fill in `.env`:

- `TELEGRAM_BOT_TOKEN` — message [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
- `NVIDIA_API_KEY` — from [build.nvidia.com](https://build.nvidia.com). Sign in, pick any model,
  click **Get API Key**. The key starts with `nvapi-` and works across the whole catalog.
- `ALLOWED_USER_IDS` — get your ID from [@userinfobot](https://t.me/userinfobot).
- `TAVILY_API_KEY` — optional at home, needed on a server (see Deploying). Free at
  [tavily.com](https://tavily.com): 1,000 searches a month, no card.
- `SEC_USER_AGENT` — strongly recommended; lets the bot read SEC filings, including the latest
  earnings release and 10-Q/10-K it fetches for every US-listed company. EDGAR blocks unidentified
  clients, so give an app name and a contact email: `StockThesisBot you@example.com`. A 10-Q or
  10-K is read from its MD&A section.

Then:

```bash
.venv/Scripts/python.exe bot.py
```

Message the bot `SPOT`, `$NVDA`, or `Palantir`.

## Verifying without spending credit

```bash
.venv/Scripts/python.exe test_offline.py               # no model calls; writes nothing to out/
.venv/Scripts/python.exe test_offline.py --live-search # also runs a real web search
```

Fetches live market data, then drives the generation path against a mocked NIM client to check
the tool-calling loop (including split tool-argument reassembly), the research-budget backstop
and minimum-search floor,
JSON header recovery when a reasoning model drops the code fence or leaks `<think>` tags, tool
error handling, PDF rendering, and Telegram formatting. It also checks the DCF against a
hand-computed case, replays the NVDA pitch's contradictions through the consistency checker,
and drives the re-ask, primary-source floor and reviewer paths against scripted responses.

## Replaying real pitches

Every pitch the bot writes comes with a replay file (`NEE_20260927T0912.json.gz`) sent after
the PDF: the market data, the research notes the writer read, the peers, and every raw write
attempt. Save them to a `replays/` folder (none ship with this repo). Before shipping a change to the checks, run
it against every pitch the bot has written:

```bash
.venv/Scripts/python.exe replay.py check            # every file in replays/
.venv/Scripts/python.exe replay.py run NEE          # a full pitch here, saved to replays/
```

`check` reruns the code checks of today's code on each saved attempt and lists what they
find; it makes no model calls. `SEND_REPLAY=0` stops the bot sending the files.

## Deploying

The bot is a long-lived polling process with 10–15 minute jobs, so it wants an always-on host, not
a serverless function. Serverless platforms cap invocation duration below the length of a pitch
(Vercel's Hobby plan is a hard 300s), and switching to webhooks would mean handling Telegram's
re-delivery of any update you don't answer in time.

It runs as a free **Render Web Service** (Python runtime) that auto-deploys from `main`.

**Create the service.** New → Web Service, connect the repo, build command
`pip install -r requirements.txt`, start command `python bot.py`. Put the `.env` values under
Environment — `TELEGRAM_BOT_TOKEN`, `NVIDIA_API_KEY`, `ALLOWED_USER_IDS`, `TAVILY_API_KEY`,
`SEC_USER_AGENT`, `TZ`,
plus any knobs from [Choosing a model](#choosing-a-model). Every push to `main` redeploys, which
kills a pitch in progress.

**Keep it awake.** A free web service must listen on `$PORT` and is spun down after 15 minutes
without inbound HTTP — Telegram polling is outbound, so it never counts. When `PORT` is set,
`bot.py` serves `GET`/`HEAD /health` on it from a background thread. Set the service's Health
Check Path to `/health` and point an external cron (cron-job.org, UptimeRobot) at
`https://<service>.onrender.com/health` every 10 minutes. Locally `PORT` is unset and no port is
opened. Startup takes about a minute on the free CPU, so "No open ports detected" right after a
deploy is harmless. Free instances can still restart at any time; a paid Background Worker needs
neither the port nor the pinger.

**One bot per token.** Telegram allows a single poller per bot token. Running `bot.py` locally
while Render is live gives `telegram.error.Conflict: terminated by other getUpdates request` —
suspend the Render service first. One or two Conflict lines during a deploy are normal, while the
old and new instances overlap.

**What Render's network loses.** Free data sources treat Render's shared egress IPs harshly:

- Yahoo Finance refuses the quote-summary call, and sometimes search. `market_data` falls back to
  chart data — price, market cap, shares, currency, exchange, company name — and reports the
  missing ratios, margins and analyst targets to the model as data gaps. From a home connection
  everything comes through.
- Every keyless search engine was blocked within hours of the first deploy. Set `TAVILY_API_KEY`:
  `web_search` then searches through Tavily (basic depth, 1 credit a search) and falls back to the
  keyless engines only when Tavily fails, e.g. once the month's credits are gone. The startup line
  reports `search=tavily`.

A failed search returns a message to the model rather than raising, so a pitch is still written,
just from thinner research. Check each deploy's first pitch for `research done: N searches` with a
non-zero N.

If the bot goes quiet, read the service logs first. `preflight` raises `SystemExit` when the
configured model has been retired, which shows up as a crash loop — loud on purpose, since it
needs a human to pick a new model.

**Elsewhere.** `Dockerfile` and `docker-compose.yml` run the same bot on any VM with
`docker compose up -d` and a `.env` alongside; `restart: unless-stopped` brings it back after a
reboot, and no inbound ports are needed.
## Choosing a model

**Do not trust the NIM docs pages or `/v1/models` for this.** Both list models that are retired
(410 Gone) or that 404 on invoke. The only proof a model works is calling it. These were probed
directly against a live key on 2026-09-06:

| Model | Result |
| --- | --- |
| `nvidia/nemotron-3-ultra-550b-a55b` | **Default.** Calls tools, ~10s first response |
| `openai/gpt-oss-20b` | Calls tools, ~4s. Small — use as a fast fallback |
| `nvidia/nemotron-3.5-lightning-30b-a3b` | Calls tools, ~4s. Small |
| `nvidia/nemotron-3-super-120b-a12b` | **Avoid.** Ignores tools and answers from memory |
| `moonshotai/kimi-k3`, `minimaxai/minimax-m3`, `deepseek-ai/deepseek-v4-*` | Timed out at 150s |
| `deepseek-ai/deepseek-v4.1-flash` | **Reviewer default.** Valid JSON verdict in 13s (2026-09-23) |
| `moonshotai/kimi-k2.6`, `meta/llama-3.3-70b-instruct` | 404 / 410 |

**The model must support tool calling**, and must actually use them. `nemotron-3-super-120b-a12b`
is the cautionary case: it accepts the tool schemas, ignores them, and answers from memory. The
pitch looks fine and is quietly wrong about anything after its training cutoff. `bot.py` runs a
live preflight call at startup that catches retired and unavailable models, but it cannot detect
a model that merely declines to use tools — that one is on you to check.

Model availability changes without warning. Re-probe with:

```bash
.venv/Scripts/python.exe -c "import openai,config; print(*sorted(m.id for m in openai.OpenAI(api_key=config.NVIDIA_API_KEY,base_url=config.NIM_BASE_URL).models.list().data),sep='\n')"
```

| Variable | Default | Effect |
| --- | --- | --- |
| `MODEL` | `nvidia/nemotron-3-ultra-550b-a55b` | Must support tool calling |
| `FALLBACK_MODEL` | `openai/gpt-oss-20b` | Takes over research when `MODEL` is overloaded, and is the write's last resort. Empty disables |
| `GEMINI_API_KEY` | empty | Google AI Studio key. Enables the Gemini writing fallbacks; without it they are skipped |
| `WRITE_FALLBACK_MODELS` | `gemini-3.8-flash,gemini-2.5-flash` | Who writes, in order, when `MODEL` keeps failing. Gemini runs on Google's endpoint, so an NVIDIA outage does not take the backup down too |
| `RESEARCH_MODEL` | `gemini-2.5-flash` with a Gemini key, else `MODEL` | Drives research (choosing searches and pages). On Gemini a research phase took 1.1 min on 2026-09-23 against 8-25 min queueing on NIM. The write stays on `MODEL` |
| `RESEARCH_MODE` | `gather` | Reads SEC filings and news in parallel, then makes one gap-fill call. Non-SEC filers fall back to the model loop; set `loop` to compare |
| `PLAN_REASK_WARNINGS` | `0` | Re-asks the planning step for softer valuation warnings; out-of-range or held inputs are always sent back for a fix, up to twice |
| `AUTO_REPAIR_NO_REASK` | `1` | Skips a draft rewrite when code already removes or flags sentence-level problems; bound model-input warnings are still re-asked in planning |
| `PATCH_REPAIRS` | `1` | Enables one keyed paragraph repair for localized draft or reviewer issues, then reruns the checks on the merged pitch; `PATCH_RETRIES` remains a fallback environment name |
| `PEER_TIMEOUT_S` | `45` | Longest the peer-multiple lookup may take before the pitch goes without a peer check |
| `RESEARCH_LAST_RESORT_S` | `120` | Time limit for the last research model in line (research otherwise moves on after `RESEARCH_FIRST_BYTE_S`) |
| `TOOL_TIMEOUT_S` | `45` | Longest a single search or page fetch may take before research moves on without it |
| `WRITE_PRIMARY_RETRIES` | `2` | Retries of `MODEL` on a write before the fallbacks. `FALLBACK_MODEL` is the last resort |
| `RESEARCH_FIRST_BYTE_S` | `60` | How long a research turn on `MODEL` may queue before moving to `FALLBACK_MODEL` |
| `REVIEW_MODEL` | `openai/gpt-oss-20b` | First reviewer; it was the only one that returned a usable verdict in the RTX run. Set `REVIEW_FALLBACK_MODELS` to change the fallback order |
| `REVIEW_FIRST_BYTE_S` | `60` | How long each reviewer before the last may wait for its first streamed response before moving to the next reviewer |
| `NIM_BASE_URL` | `https://integrate.api.nvidia.com/v1` | Point at a self-hosted NIM container to run locally |
| `TEMPERATURE` | `0.4` | Research. Lower is more literal, higher more speculative |
| `WRITE_TEMPERATURE` | `0.2` | The write and the review, kept low so repeat runs on the same facts agree |
| `MAX_TOKENS` | `16000` | Raise if pitches get truncated |
| `MAX_TOOL_ROUNDS` | `16` | Research budget. On exhaustion the tools are withdrawn and the model is told to write |
| `MIN_SEARCHES` | `10` | A model that stops with fewer successful searches is sent back (at most twice) to fill the gaps. 0 disables. Keep it well under `MAX_TOOL_ROUNDS` |
| `MIN_PRIMARY_FETCHES` | `3` | Filings, earnings releases or IR pages research must read before it may stop; the ones read from EDGAR count. Shares the two nudges with `MIN_SEARCHES`. 0 disables |
| `HOUSE_WACC` | `1` | The code computes the discount rate: 10-year Treasury + Blume-adjusted beta (5-year monthly vs the S&P 500, from price history) x `EQUITY_RISK_PREMIUM`, debt at risk-free + `DEBT_SPREAD` after `WACC_TAX_RATE`, weighted at market value, held to 6-14%. The model may add up to 2pp with a named reason. `0` lets the model set it |
| `EQUITY_RISK_PREMIUM` | `0.045` | Equity risk premium in the house WACC |
| `DEBT_SPREAD` | `0.015` | Pre-tax cost of debt over the Treasury yield |
| `WACC_TAX_RATE` | `0.21` | Tax rate for the debt shield in the house WACC |
| `RISK_FREE_DEFAULT` | `0.043` | Used only when the 10-year Treasury quote is unavailable |
| `QUOTE_RETRY_DELAYS_S` | `3,10` | Pauses between retries when Yahoo refuses the quote summary; it is also asked once more after research |
| `TWO_PASS_WRITE` | `1` | The model sends its `model` inputs and checked argument map first, the code values them, and the prose is written with the computed numbers and counterarguments in hand. `0` writes in one pass |
| `STRUCTURED_FACTS` | `1` | Adds V2's period-aware SEC/XBRL fact database and source-conflict reconciliation to V1's research notes. If it fails, the original V1 research continues |
| `REVIEW_ENABLED` | `1` | Run the reviewer after the code checks pass. `0` turns it off |
| `REVIEW_TIMEOUT_S` | `300` | A review not finished in this long moves to the next reviewer, then is skipped |
| `REVIEW_FALLBACK_MODELS` | `deepseek-ai/deepseek-v4.1-flash,gemini-3.8-flash,gemini-2.5-flash` | Reviewers tried when `REVIEW_MODEL` fails (the Gemini ones need `GEMINI_API_KEY`) |
| `REVIEW_LAST_RESORT_MODEL` | `openai/gpt-oss-20b` | Reviewer on NIM tried after all others, so a spent Gemini quota does not skip the review. Also tried when a reviewer answers without a usable verdict. Empty turns it off |
| `REVIEW_DIGEST_CHARS` | `30000` | How much of the research notes the reviewer sees |

Because NIM is OpenAI-compatible, pointing `NIM_BASE_URL` at a self-hosted NIM container (or any
OpenAI-compatible endpoint) works without code changes.

## Notes and limits

- **Gemini's free tier allows 20 requests a day per model.** 
- **The numbers are model estimates.** 
