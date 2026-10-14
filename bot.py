"""Telegram bot: message it a ticker, get a full investment pitch back as a PDF."""

from __future__ import annotations

import asyncio
import html
import logging
import time

import openai
from telegram import InputMediaDocument, InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import NetworkError
from telegram.constants import ChatAction, ChatType
from telegram.ext import (
    AIORateLimiter, Application, CommandHandler, ContextTypes, MessageHandler, filters,
)

import config
import health
import history
import market_data
import pdf_render
import pitch as pitch_mod
import replay
import tg_format

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
# ddgs's HTTP client logs every backend it tries, including Google's routine 429 for a
# datacenter IP before it falls back. A search that really fails is logged by
# research_tools.web_search.
logging.getLogger("primp").setLevel(logging.WARNING)
log = logging.getLogger("stockbot")

WELCOME = (
    "<b>Stock Thesis Bot</b>\n\n"
    "Send me a ticker and I will research the company and write a full investment pitch: "
    "thesis pillars, competitive position, forecast build, a DCF with the model table, and a "
    "buy/hold/sell call.\n\n"
    "<b>Try</b>\n"
    "<code>SPOT</code>   <code>$NVDA</code>   <code>Palantir</code>\n\n"
    "It takes roughly 10-15 minutes per name, longer when NVIDIA's free tier is busy — I search "
    "the web and build the model before writing. You get a summary card here and the full "
    "thesis as a PDF.\n\n"
    "<i>Not investment advice. Verify every figure against primary filings.</i>"
)

# The stages of a pitch, in the order they run, as the progress checklist names them.
STAGES = {
    "data": "Market data",
    "research": "Research",
    "facts": "Reconciling SEC filings",
    "planning": "Building the valuation model",
    "writing": "Writing the thesis",
    "reviewing": "Independent review",
    "revising": "Revising",
    "pdf": "Rendering the PDF",
}
# The progress events pitch.py sends that start a stage. Any other event (streaming,
# thinking, retrying) is a detail of the stage already running.
STAGE_OF = {
    "resolving": "data", "data": "data", "research": "research",
    "searching": "research", "reconciling financial data": "facts", "planning": "planning",
    "writing": "writing", "revising": "revising", "reviewing": "reviewing", "pdf": "pdf",
}


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


class Status:
    """A progress checklist in one message, edited in place: finished stages with how
    long they took, the running one with a live timer and its detail (searches, words
    written, provider retries), and the stages still to come.

    Stage changes post immediately. Detail updates within a stage are throttled, and a
    background tick keeps the timer moving while a model sits in a queue. The message
    stays in the chat when the pitch ends, as a record of where the time went.
    """

    def __init__(self, message, min_interval: float = 4.0, tick: float = 10.0):
        self._message = message
        self._min_interval = min_interval
        self._tick = tick
        self._last_edit = 0.0
        self._last_text = ""
        self.symbol = ""
        self._started = time.monotonic()
        self._done: list[tuple[str, float, str]] = []    # (stage, seconds, detail)
        self._stage = ""
        self._stage_started = self._started
        self._detail = ""
        self._chars = 0
        self._lock = asyncio.Lock()
        self._ticker: asyncio.Task | None = None

    def _upcoming(self) -> list[str]:
        """The main stages not reached yet; skipped ones are dropped at the end."""
        seen = {s for s, _, _ in self._done} | {self._stage}
        skip = {"revising"}
        if not config.STRUCTURED_FACTS:
            skip.add("facts")
        if not config.TWO_PASS_WRITE:
            skip.add("planning")
        if not config.REVIEW_ENABLED:
            skip.add("reviewing")
        order = list(STAGES)
        at = order.index(self._stage) if self._stage in order else -1
        return [s for s in order[at + 1:] if s not in seen and s not in skip]

    def render(self, final: str = "") -> str:
        now = time.monotonic()
        name = f"<b>{html.escape(self.symbol)}</b> · " if self.symbol else ""
        head = {"ok": "✅", "failed": "❌"}.get(final, "⏳")
        lines = [f"{head} {name}{_duration(now - self._started)}"]
        for stage, seconds, detail in self._done:
            lines.append(f"✅ {STAGES[stage]} · {_duration(seconds)}"
                         + (f" — {html.escape(detail)}" if detail else ""))
        if self._stage:
            mark = "❌" if final == "failed" else ("✅" if final else "⏳")
            lines.append(f"{mark} <b>{STAGES[self._stage]}</b> · "
                         f"{_duration(now - self._stage_started)}"
                         + (f" — {html.escape(self._detail)}" if self._detail else ""))
        if not final:
            lines += [f"▫️ {STAGES[s]}" for s in self._upcoming()]
        return "\n".join(lines)

    async def set(self, phase: str, value: int = 0) -> None:
        stage = STAGE_OF.get(phase)
        now = time.monotonic()
        if stage and stage != self._stage:
            if self._stage:
                self._done.append((self._stage, now - self._stage_started, self._detail))
            self._stage, self._stage_started, self._detail, self._chars = stage, now, "", 0
            if stage == "revising" and value:
                self._detail = f"{value} reviewer issue{'s' if value != 1 else ''}"
            await self._edit(force=True)
            return
        if phase == "streaming":
            self._chars += value
            self._detail = f"{max(1, self._chars // 6):,} words"
        elif phase == "searching":
            self._detail = f"{value} search{'es' if value != 1 else ''}"
        elif phase == "thinking" and stage is None:
            self._detail = "model reasoning"
        elif phase == "retrying":
            self._detail = f"model provider busy, retry {value}"
        elif stage is None:     # an event this checklist does not know: show it as is
            self._detail = phase
        await self._edit()

    async def _edit(self, force: bool = False, final: str = "") -> None:
        if self._ticker is None and not final:
            self._ticker = asyncio.create_task(self._tick_loop())
        async with self._lock:
            now = time.monotonic()
            if not force and now - self._last_edit < self._min_interval:
                return
            text = self.render(final)
            if text == self._last_text:
                return
            self._last_text, self._last_edit = text, now
            try:
                await self._message.edit_text(text, parse_mode="HTML")
            except Exception:  # deleted, unchanged or rate limited: progress is cosmetic
                pass

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick)
            await self._edit()

    def stop(self) -> None:
        """Stop the timer, however the pitch ended."""
        if self._ticker:
            self._ticker.cancel()

    async def finish(self, ok: bool = False) -> None:
        self.stop()
        await self._edit(force=True, final="ok" if ok else "failed")


def _authorised(update: Update) -> bool:
    if not config.ALLOWED_USER_IDS:
        return False   # private by default: an empty allow-list serves no one
    user = update.effective_user
    return bool(user and user.id in config.ALLOWED_USER_IDS)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await update.message.reply_text("This bot is private.")
        return
    await update.message.reply_html(WELCOME)


async def handle_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await update.message.reply_text("This bot is private.")
        return

    query = (update.message.text or "").strip()
    if not query or query.startswith("/"):
        return
    if len(query) > 64:
        await update.message.reply_text("That does not look like a ticker. Try `SPOT` or `NVDA`.")
        return

    user_id = update.effective_user.id
    running: set[int] = context.application.bot_data.setdefault("running", set())
    if user_id in running:
        await update.message.reply_text(
            "I am still working on your last request. One at a time, please."
        )
        return
    running.add(user_id)

    status_msg = await update.message.reply_text(f"⏳ {query}: resolving the ticker…")
    status = Status(status_msg)
    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        await status.set("data")
        try:
            data = await asyncio.to_thread(market_data.fetch, query)
        except market_data.TickerNotFound as exc:
            await status.finish(ok=False)
            await update.message.reply_text(f"❌ {exc}")
            return

        log.info("Generating pitch for %s (user %s)", data.symbol, user_id)
        status.symbol = data.symbol
        await status.set("research")

        client: openai.AsyncOpenAI = context.application.bot_data["llm"]
        runs, pinned = await _load_history(context.bot, update.effective_chat.id)
        result = await pitch_mod.generate(data, client, on_progress=status.set)
        result.history = history.earlier(runs, data.symbol)

        await status.set("pdf")
        pdf_path = await asyncio.to_thread(pdf_render.build, result, data)

        await status.finish(ok=True)
        await update.message.reply_html(tg_format.summary_card(result, data))
        # Files go without captions, at the user's request; the disclaimer is in the PDF.
        with pdf_path.open("rb") as fh:
            await update.message.reply_document(document=fh, filename=pdf_path.name)
        # Telegram now holds the document. Keeping a copy would grow the disk of a
        # host nobody is watching; a failed upload leaves the file for inspection.
        pdf_path.unlink(missing_ok=True)
        await _send_assumptions_editor(update, result)
        await _send_replay(update, result)
        await _save_history(context.bot, update.effective_chat.id, runs, pinned, result, data)

        log.info(
            "Done %s — %s prompt tokens, %s completion tokens, %d sources",
            data.symbol, result.usage.get("prompt_tokens"),
            result.usage.get("completion_tokens"), len(result.sources),
        )

    except pitch_mod.PitchError as exc:
        log.error("Pitch failed for %s: %s", query, exc)
        await status.finish(ok=False)
        await update.message.reply_text(f"❌ {exc}")
    except Exception:
        log.exception("Unhandled failure for query %r", query)
        await status.finish(ok=False)
        await update.message.reply_text(
            "❌ Something broke while building that pitch. Try again in a moment."
        )
    finally:
        status.stop()
        running.discard(user_id)


async def _send_replay(update: Update, result) -> None:
    """The pitch's inputs and raw writes, for the replays/ folder. Render's disk is wiped
    on every deploy, so the chat is the only place a replay file survives."""
    if not config.SEND_REPLAY or not result.replay:
        return
    try:
        path = await asyncio.to_thread(replay.save, result.replay, config.OUT_DIR)
        with path.open("rb") as fh:
            await update.message.reply_document(
                document=fh, filename=path.name, disable_notification=True)
        path.unlink(missing_ok=True)
    except Exception:
        log.exception("could not send the replay file")


async def _send_assumptions_editor(update: Update, result) -> None:
    """Offer an authenticated Telegram Web App for this pitch's saved model inputs."""
    if not config.WEB_APP_BASE_URL or not result.replay:
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    path = None
    try:
        path = await asyncio.to_thread(replay.save, result.replay, config.OUT_DIR)
        token = await asyncio.to_thread(
            health.register_assumptions, path, update.effective_user.id)
        url = f"{config.WEB_APP_BASE_URL}/assumptions/{token}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
            "Edit valuation assumptions", web_app=WebAppInfo(url=url))]])
        await update.message.reply_text(
            "You can also edit the forecast inputs on your phone and calculate a new valuation. "
            "The PDF already sent is unchanged.", reply_markup=keyboard)
    except Exception:
        log.exception("could not create the assumptions editor")
    finally:
        if path:
            path.unlink(missing_ok=True)


async def _load_history(bot, chat_id: int):
    """Every earlier run: the repo's seed, this machine's file, and the history document
    pinned in this chat, which is the copy that survives a Render deploy. Never raises."""
    runs, pinned = history.load_local(), None
    if not config.HISTORY_PIN:
        return runs, None
    try:
        chat = await bot.get_chat(chat_id)
        message = chat.pinned_message
        if (message and message.document and message.document.file_name == history.FILENAME
                and message.from_user and message.from_user.id == bot.id):
            raw = await (await bot.get_file(message.document.file_id)).download_as_bytearray()
            runs, pinned = history.merge(runs, history.parse(bytes(raw))), message
    except Exception:
        log.exception("could not read the pinned run history")
    return runs, pinned


async def _save_history(bot, chat_id: int, runs: list, pinned, result, data) -> None:
    """Add this run, and replace the pinned history document (or pin a first one)."""
    run = history.from_pitch(result, data)
    if run is None:
        return
    runs = history.merge(runs, [run])
    history.save_local(runs)
    if not config.HISTORY_PIN:
        return
    try:
        if pinned is not None:
            await bot.edit_message_media(
                chat_id=chat_id, message_id=pinned.message_id,
                media=InputMediaDocument(history.dump(runs), filename=history.FILENAME))
        else:
            sent = await bot.send_document(chat_id, document=history.dump(runs),
                                           filename=history.FILENAME,
                                           disable_notification=True)
            await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
    except Exception:
        log.exception("could not save the run history to the chat")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Telegram error", exc_info=context.error)


# Verified against the live NIM catalog on 2026-09-06: these answer and call tools.
KNOWN_GOOD_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b",
    "openai/gpt-oss-20b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
]


def preflight(client: openai.OpenAI) -> None:
    """Prove the configured model actually answers before the bot accepts work.

    Catalog membership is not enough: NVIDIA retires models on a rolling basis
    (410 Gone) and several models that appear in /v1/models still 404 on invoke.
    Only a real call settles it. A network failure here is not fatal. Claude models
    run through Claude Code, which reports a bad login on the first call instead.
    """
    if config.MODEL.startswith("claude-"):
        return
    try:
        client.chat.completions.create(
            model=config.MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        return
    except (openai.NotFoundError, openai.APIStatusError) as exc:
        status = getattr(exc, "status_code", "")
        if status not in (404, 410):
            log.warning("Could not verify the model (HTTP %s): %s", status, exc)
            return
        reason = "has been retired" if status == 410 else "is not available to your account"
    except openai.APIConnectionError as exc:
        log.warning("Could not reach NIM to verify the model: %s", exc)
        return

    raise SystemExit(
        f"MODEL '{config.MODEL}' {reason} (HTTP {status}).\n"
        "Set MODEL in .env to one of these, verified working:\n"
        + "\n".join(f"  {m}" for m in KNOWN_GOOD_MODELS)
        + "\n\nFull catalog: https://build.nvidia.com"
    )


def build_app() -> Application:
    """Construct a fresh Application. Called again after a network drop."""
    # PTB defaults to 5s, which is tight for a first connection and too short for
    # uploading a PDF of a few hundred KB.
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(60)
        .get_updates_read_timeout(40)
        .build()
    )

    # No SDK-level retries: pitch._stream_turn retries every error the SDK would, and
    # also moves an overloaded turn to the fallback model. Stacked, the two retried a
    # single busy request up to 16 times, each after minutes in NIM's queue.
    app.bot_data["llm"] = openai.AsyncOpenAI(
        api_key=config.NVIDIA_API_KEY,
        base_url=config.NIM_BASE_URL,
        timeout=config.REQUEST_TIMEOUT_S,
        max_retries=0,
    )

    async def close_llm(application: Application) -> None:
        """Close the HTTP pool on shutdown; otherwise Python 3.14 prints a teardown
        traceback from httpcore's async generator as the interpreter exits."""
        await application.bot_data["llm"].close()
        await pitch_mod.close_clients()

    app.post_shutdown = close_llm

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ticker))
    app.add_error_handler(on_error)

    return app


def main() -> None:
    missing = config.missing_required()
    if missing:
        raise SystemExit(
            "Missing required settings: " + ", ".join(missing)
            + "\nCopy .env.example to .env and fill them in."
        )

    # Before preflight: a slow NIM can hold preflight for ~90s of retries, and Render
    # is scanning for the port the whole time.
    if config.HEALTH_PORT:
        health.serve_in_background(config.HEALTH_PORT)
        log.info("Health endpoint listening on :%d/health", config.HEALTH_PORT)

    preflight(openai.OpenAI(
        api_key=config.NVIDIA_API_KEY, base_url=config.NIM_BASE_URL, timeout=30
    ))

    log.info("Bot online — model=%s, research=%s, %d tool rounds, search=%s, allowlist=%s",
             config.MODEL, config.RESEARCH_MODEL, config.MAX_TOOL_ROUNDS,
             "tavily" if config.TAVILY_API_KEY else "keyless engines",
             config.ALLOWED_USER_IDS or "NO ONE (set ALLOWED_USER_IDS)")

    # A laptop that sleeps, or a dropped Wi-Fi link, kills polling with a DNS or
    # connect error. That should be a pause, not the end of the bot. The old
    # Application has already shut down and cannot be reused, so build a fresh one.
    delay = 5
    while True:
        try:
            build_app().run_polling(allowed_updates=Update.ALL_TYPES)
            return                                    # clean shutdown (Ctrl+C)
        except KeyboardInterrupt:
            return
        except NetworkError as exc:
            log.warning("Lost the Telegram connection (%s) — reconnecting in %ds",
                        exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 300)


if __name__ == "__main__":
    main()
