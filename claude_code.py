"""Run Claude through Claude Code itself, via the Claude Agent SDK.

The SDK starts the Claude Code program bundled with it, signed in with
CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) or a local `claude` login, so the
calls count against a Claude subscription rather than an API key. Claude Code's own
tools are switched off: research gets the bot's web_search and fetch_page as in-process
tools, and every other call gets none.

Claude Code runs its own tool loop, so research is one session here rather than one
request per round as on the OpenAI-compatible providers. A model name starting with
"claude-" routes here (pitch._one_stream and pitch._research).
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from typing import Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLINotFoundError,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolAnnotations,
    create_sdk_mcp_server,
    tool,
)

import config

log = logging.getLogger(__name__)

SERVER = "research"

# The SDK gives Claude Code this long (ms) to answer its start-up handshake, 60 s by
# default. On Render's shared CPU a GS run on 2026-10-12 took longer, and the launch
# failed as "Control request timeout: initialize".
os.environ.setdefault("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "240000")


class ClaudeCodeError(ClaudeSDKError):
    """Claude Code ended a call with an error: overload, a usage limit, a bad request."""


class ClaudeCodeAuthError(ClaudeCodeError):
    """Not signed in, or the token was refused. Retrying will not help."""


# Errors that will repeat on every retry, and errors worth moving on from.
FATAL_ERRORS = (CLINotFoundError, ClaudeCodeAuthError)
TRANSIENT_ERRORS = (ClaudeSDKError,)


def is_claude(model: str) -> bool:
    return model.startswith("claude-")


def _split(messages: list[dict]) -> tuple[str, str]:
    """OpenAI-style messages as (system prompt, prompt). Claude Code takes one user
    prompt per turn, so a conversation with earlier turns (a revision after a plan) is
    written out as a transcript ending in the latest request."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    if len(turns) == 1:
        return system, turns[0]["content"] or ""
    parts = ["The conversation so far, oldest first. Reply to the last user message."]
    for m in turns:
        label = "Your earlier reply" if m["role"] == "assistant" else "User"
        parts.append(f"<{label}>\n{m.get('content') or ''}\n</{label}>")
    return system, "\n\n".join(parts)


@contextlib.contextmanager
def _prompt_file(system: str):
    """The system prompt as a file. Passed inline it goes on the command line, and the
    write's instructions run past Windows's 32,767-character limit: the launch then fails
    as "Claude Code not found"."""
    fd, path = tempfile.mkstemp(prefix="stockbot-system-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(system)
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def _options(system_path: str, effort: str, **extra) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=extra.pop("model"),
        system_prompt={"type": "file", "path": system_path},
        effort=effort,
        tools=[],                    # none of Claude Code's own tools
        setting_sources=[],          # no ~/.claude settings, CLAUDE.md or memory
        permission_mode="dontAsk",   # anything not in allowed_tools is refused
        cwd=tempfile.gettempdir(),
        include_partial_messages=True,
        # Thinking shares the output budget, and the plan's JSON alone ran past 16,000.
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max(config.WRITE_MAX_TOKENS, 64000))},
        stderr=lambda line: log.info("claude: %s", line.rstrip()),
        **extra,
    )


async def _read_reply(client: ClaudeSDKClient, on_progress, usage: dict) -> str:
    """Drain one reply. Returns its final text; raises ClaudeCodeError on a failed run."""
    last_text: list[str] = []
    async for msg in client.receive_response():
        if isinstance(msg, StreamEvent):
            delta = msg.event.get("delta") or {}
            if on_progress and delta.get("type") == "text_delta":
                await on_progress("streaming", len(delta.get("text", "")))
            elif on_progress and delta.get("type") == "thinking_delta":
                await on_progress("thinking", 0)
        elif isinstance(msg, AssistantMessage):
            if msg.error in ("authentication_failed", "billing_error"):
                raise ClaudeCodeAuthError(f"Claude Code: {msg.error}")
            texts = [b.text for b in msg.content if isinstance(b, TextBlock)]
            if texts:
                last_text = texts
            if on_progress and any(isinstance(b, ThinkingBlock) for b in msg.content):
                await on_progress("thinking", 0)
        elif isinstance(msg, ResultMessage):
            u = msg.usage or {}
            usage["prompt_tokens"] += (u.get("input_tokens", 0)
                                       + u.get("cache_read_input_tokens", 0)
                                       + u.get("cache_creation_input_tokens", 0))
            usage["completion_tokens"] += u.get("output_tokens", 0)
            if msg.stop_reason == "max_tokens":
                usage["finish_reason"] = "length"
            elif msg.stop_reason:
                usage["finish_reason"] = "stop"
            if msg.is_error and msg.subtype != "error_max_turns":
                detail = "; ".join(msg.errors or []) or msg.result or msg.subtype
                if msg.api_error_status in (401, 403):
                    raise ClaudeCodeAuthError(f"Claude Code refused the login: {detail}")
                raise ClaudeCodeError(f"Claude Code failed ({msg.subtype}, "
                                      f"HTTP {msg.api_error_status}): {detail}")
            if msg.subtype == "error_max_turns":
                log.info("Claude Code research reached its turn limit")
            return msg.result if msg.result is not None else "".join(last_text)
    raise ClaudeCodeError("Claude Code ended without a result")


async def complete(model: str, messages: list[dict], effort: str, on_progress,
                   usage: dict) -> str:
    """One answer with no tools: the write, the plan, a revision, the gap-fill call."""
    system, prompt = _split(messages)
    with _prompt_file(system) as path:
        options = _options(path, effort, model=model, max_turns=1)
        async with _client(options) as client:
            await client.query(prompt)
            return await _read_reply(client, on_progress, usage)


@contextlib.asynccontextmanager
async def _client(options: ClaudeAgentOptions):
    """A connected client. A start-up handshake that times out comes from the SDK as a
    bare Exception; it is raised as ClaudeCodeError so callers fall back to the next
    model instead of losing the pitch."""
    client = ClaudeSDKClient(options=options)
    try:
        await client.connect()
    except Exception as exc:
        if isinstance(exc, ClaudeSDKError) or "Control request timeout" not in str(exc):
            raise
        with contextlib.suppress(Exception):
            await client.disconnect()
        raise ClaudeCodeError(f"Claude Code did not start: {exc}") from exc
    try:
        yield client
    finally:
        await client.disconnect()


ToolRunner = Callable[[str, dict], Awaitable[str]]


def _mcp_tools(tool_schemas: list[dict], run_tool: ToolRunner) -> list:
    """The bot's OpenAI-style tool schemas as in-process MCP tools that call run_tool."""
    out = []
    for spec in tool_schemas:
        fn = spec["function"]

        def bind(name: str):
            async def handler(args: dict) -> dict:
                return {"content": [{"type": "text", "text": await run_tool(name, args)}]}
            return handler

        # A fetched filing runs to tens of thousands of characters; keep it inline
        # rather than letting Claude Code park it in a file the model cannot open.
        out.append(tool(fn["name"], fn["description"], fn["parameters"],
                        annotations=ToolAnnotations(readOnlyHint=True,
                                                    maxResultSizeChars=500_000))(
            bind(fn["name"])))
    return out


async def research(model: str, messages: list[dict], tool_schemas: list[dict],
                   run_tool: ToolRunner, follow_up: Callable[[str], str | None],
                   effort: str, max_turns: int, on_progress, usage: dict) -> None:
    """A research session: Claude calls run_tool until it replies. follow_up(reply)
    returns a message that sends it back for more (a nudge), or None to stop."""
    system, prompt = _split(messages)
    names = [spec["function"]["name"] for spec in tool_schemas]
    with _prompt_file(system) as path:
        options = _options(
            path, effort, model=model, max_turns=max_turns,
            mcp_servers={SERVER: create_sdk_mcp_server(
                SERVER, tools=_mcp_tools(tool_schemas, run_tool))},
            allowed_tools=[f"mcp__{SERVER}__{n}" for n in names],
        )
        async with _client(options) as client:
            while prompt:
                await client.query(prompt)
                reply = await _read_reply(client, on_progress, usage)
                prompt = follow_up(reply)
