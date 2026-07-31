"""LLM providers behind the CoachProvider seam (docs/06-coach.md)."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, assert_never, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    SdkMcpTool,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)
from copilot import CopilotClient, CopilotSession, SystemMessageConfig, ToolSet
from copilot.session_events import (
    AssistantMessageData,
    SessionErrorData,
    SessionEvent,
    SessionIdleData,
)
from copilot.tools import Tool, ToolInvocation, ToolResult
from pydantic import BaseModel

from chess_coach.coach.prompt import (
    CHAT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    format_eval,
    render_chat_prompt,
)
from chess_coach.domain import (
    ChatMessage,
    EvalLine,
    GameDetail,
    GameSummary,
    LlmConfig,
    OpeningStats,
    Result,
    TimeClass,
)

logger = logging.getLogger(__name__)

# The engine seam: FEN in, MultiPV lines out. The API layer injects this,
# wrapping the engine pool — components never import each other.
PositionAnalystFn = Callable[[str], Awaitable[list[EvalLine]]]

# Bounds the agentic explain() loop: enough turns for a couple of follow-up
# engine calls plus the final write-up, without letting it run away. The two
# providers enforce this differently: ClaudeAgentSdkProvider passes it as
# max_turns, an SDK-enforced hard stop on the whole run. The Copilot SDK has
# no such option, so CopilotSdkProvider counts engine-tool calls itself and
# gives one grace round past the budget (a nudge to wrap up) before cutting
# the run off outright — see CopilotSdkProvider.explain.
_EXPLAIN_MAX_TURNS = 8

# Same budget, for the agentic complete() loop when an analyst is supplied
# (docs/archive/fixes-2026-07/04-report-engine-tool.md): a report run gets a couple
# of engine calls to verify concrete lines before asserting them, plus the
# final write-up. A separate constant from _EXPLAIN_MAX_TURNS because the
# two flows are tuned independently even though they share a value today.
# Same per-provider enforcement split — see ClaudeAgentSdkProvider.complete
# and CopilotSdkProvider.complete.
_REPORT_MAX_TURNS = 8

# Bounds each chat message's agentic loop (docs/06-coach.md, "Chat" --
# "Budgets"): one message is a user pressing send, so it gets the same
# turn allowance a report or explain run does. Enforced the same
# per-provider way -- ClaudeAgentSdkProvider's max_turns vs
# CopilotSdkProvider's counted _ToolCallBudget -- except the budget is
# shared across all four chat tools in one message, not just the engine
# tool, since it stands in for the SDK's own per-run max_turns.
_CHAT_MAX_TURNS = 8

# The in-process MCP server exposing the engine seam as a tool. The model
# calls it as f"mcp__{_MCP_SERVER_NAME}__{_ANALYZE_TOOL_NAME}".
_MCP_SERVER_NAME = "engine"
_ANALYZE_TOOL_NAME = "analyze_position"
_ALLOWED_ANALYZE_TOOL = f"mcp__{_MCP_SERVER_NAME}__{_ANALYZE_TOOL_NAME}"
_ANALYZE_TOOL_DESCRIPTION = (
    "Run a Stockfish multi-PV analysis of a chess position (given as FEN) "
    "and return the top candidate lines. Use this for follow-ups, such as "
    "analyzing the position after the played move to find the opponent's "
    "best reply."
)

# The chat toolkit's other three tools (docs/06-coach.md, "Chat" --
# "Tools"): read-only lookups over the thread's player, pre-scoped by the
# API layer's ChatToolkit implementation -- the model passes filters,
# never a username.
_FIND_GAMES_TOOL_NAME = "find_games"
_GET_GAME_TOOL_NAME = "get_game"
_GET_OPENING_STATS_TOOL_NAME = "get_opening_stats"

_FIND_GAMES_TOOL_DESCRIPTION = (
    "Search the student's own stored games by opponent, opening, result, "
    "time class, or date range (unix epoch seconds). Returns compact rows "
    "-- date, color, opponent with ratings, result, time class, opening, "
    "and the game id -- for at most `limit` games (default 10, most "
    "recent first). Call get_game with a returned id for full detail."
)
_GET_GAME_TOOL_DESCRIPTION = (
    "Look up one of the student's games by id (as returned by find_games) "
    "and return its identity plus a compact move sheet: every move in "
    "SAN, with judgment and eval in pawns shown at the moves that matter."
)
_GET_OPENING_STATS_TOOL_DESCRIPTION = (
    "Return the student's repertoire: one row per opening per color, "
    "each with the student's own move order, the full line as played, "
    "whether the name is the opponent's choice, the record, and the "
    "average loss in pawns per move for the opening phase and for the "
    "whole game."
)

_RESULT_VALUES = frozenset({"win", "loss", "draw"})
_TIME_CLASS_VALUES = frozenset({"bullet", "blitz", "rapid", "daily"})

_FIND_GAMES_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "opponent": {
            "type": "string",
            "description": "Filter by opponent username (exact match).",
        },
        "opening": {
            "type": "string",
            "description": "Filter by opening name (substring match).",
        },
        "result": {
            "type": "string",
            "enum": sorted(_RESULT_VALUES),
            "description": "Filter by the student's result.",
        },
        "time_class": {
            "type": "string",
            "enum": sorted(_TIME_CLASS_VALUES),
            "description": "Filter by time control class.",
        },
        "since": {
            "type": "integer",
            "description": "Only games ending at or after this unix epoch second.",
        },
        "until": {
            "type": "integer",
            "description": "Only games ending before this epoch second (exclusive).",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum rows to return (default 10).",
        },
    },
    "required": [],
}
_GET_GAME_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "game_id": {
            "type": "string",
            "description": "The game id, as returned by find_games.",
        }
    },
    "required": ["game_id"],
}
_GET_OPENING_STATS_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}

# What a chat tool call returns once _CHAT_MAX_TURNS's grace round (and
# every runaway call after it) is spent, in place of doing the real work --
# mirrors _ENGINE_BUDGET_EXHAUSTED below but phrased for any of chat's four
# tools, not just the engine.
_CHAT_BUDGET_EXHAUSTED = (
    "Tool-call budget for this message is exhausted — finish your answer "
    "with what you have gathered so far."
)


class ExplainEvent(BaseModel):
    """One streamed increment of a move explanation."""

    type: Literal["text", "tool"]
    text: str  # text chunk | tool-call summary


class ChatEvent(BaseModel):
    """One streamed increment of a chat turn (docs/06-coach.md, "Chat")."""

    type: Literal["text", "tool", "done"]
    text: str = ""  # text chunk | tool-call summary | full reply
    provider_state: str | None = None  # done events only


class ChatToolkit(Protocol):
    """The chat tool seam -- PositionAnalystFn generalized. Implemented by
    the API layer over storage and the engine pool (components never
    import each other), pre-scoped to the thread's player: the model
    passes filters, never a username. All read-only; there is no raw SQL
    tool.
    """

    analyst: PositionAnalystFn | None  # None = engine pool down

    async def find_games(
        self,
        *,
        opponent: str | None = None,
        opening: str | None = None,
        result: Result | None = None,
        time_class: TimeClass | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int = 10,
    ) -> list[GameSummary]: ...
    async def get_game(self, game_id: str) -> GameDetail | None: ...
    async def opening_stats(self) -> list[OpeningStats]: ...


class CoachProviderError(Exception):
    """The provider could not produce advice."""


class CoachProvider(Protocol):
    async def complete(
        self, prompt: str, analyst: PositionAnalystFn | None = None
    ) -> str: ...
    def explain(
        self, prompt: str, analyst: PositionAnalystFn
    ) -> AsyncGenerator[ExplainEvent]: ...
    def chat(
        self,
        *,
        system_context: str,
        history: list[ChatMessage],
        message: str,
        toolkit: ChatToolkit,
        provider_state: str | None = None,
    ) -> AsyncGenerator[ChatEvent]: ...


class ClaudeAgentSdkProvider:
    """Coach completions through the local Claude Code login.

    No API key anywhere: authentication and billing ride the user's
    Claude subscription. Requires the `claude` CLI to be installed
    and logged in on this machine.
    """

    def __init__(self, model: str, system_prompt: str | None = None) -> None:
        self._model = model
        self._system_prompt = system_prompt

    async def complete(
        self, prompt: str, analyst: PositionAnalystFn | None = None
    ) -> str:
        if analyst is None:
            # Same built-in-tool lockdown as every other provider path:
            # a coaching completion must never reach Claude Code's file
            # or shell tools, and with max_turns=1 a stray tool call
            # would burn the only turn and come back empty anyway.
            options = ClaudeAgentOptions(
                model=self._model,
                max_turns=1,
                system_prompt=self._system_prompt,
                tools=[],
                allowed_tools=[],
            )
        else:
            # Same MCP-server mechanics as explain() below, under the
            # report turn budget rather than the explain one — the model
            # can verify a concrete line before asserting it in the brief.
            server = create_sdk_mcp_server(
                name=_MCP_SERVER_NAME, tools=[_build_analyze_tool(analyst)]
            )
            options = ClaudeAgentOptions(
                model=self._model,
                system_prompt=self._system_prompt,
                max_turns=_REPORT_MAX_TURNS,
                mcp_servers={_MCP_SERVER_NAME: server},
                tools=[],  # no built-in Claude Code tools — only the engine tool
                allowed_tools=[_ALLOWED_ANALYZE_TOOL],
            )
        chunks: list[str] = []
        fallback: str | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
                elif isinstance(message, ResultMessage):
                    fallback = message.result
                    if message.is_error:
                        # The actionable detail (e.g. "Not logged in")
                        # arrives in `result`, not `subtype`.
                        detail = message.result or message.subtype
                        raise CoachProviderError(
                            f"claude-agent-sdk run failed: {detail}"
                        )
                    if message.total_cost_usd is not None:
                        logger.info(
                            "coach completion: %.4f USD, %d turn(s)",
                            message.total_cost_usd,
                            message.num_turns,
                        )
        except CoachProviderError:
            raise
        except Exception as exc:  # CLI missing, process death, transport
            raise CoachProviderError(
                f"claude-agent-sdk failed: {exc} — is the `claude` CLI "
                "installed and logged in?"
            ) from exc

        text = "".join(chunks).strip() or (fallback or "").strip()
        if not text:
            raise CoachProviderError("claude-agent-sdk returned no text")
        return text

    async def explain(
        self, prompt: str, analyst: PositionAnalystFn
    ) -> AsyncGenerator[ExplainEvent]:
        server = create_sdk_mcp_server(
            name=_MCP_SERVER_NAME, tools=[_build_analyze_tool(analyst)]
        )
        options = ClaudeAgentOptions(
            model=self._model,
            system_prompt=self._system_prompt,
            max_turns=_EXPLAIN_MAX_TURNS,
            mcp_servers={_MCP_SERVER_NAME: server},
            tools=[],  # no built-in Claude Code tools — only the engine tool
            allowed_tools=[_ALLOWED_ANALYZE_TOOL],
        )

        produced_text = False
        fallback: str | None = None
        try:
            # aclosing: an early close of *this* generator (client gone)
            # must also end the SDK query turn now, not at GC time. The
            # SDK types query() as AsyncIterator, but it is an async
            # generator — the cast recovers the aclose() aclosing needs.
            stream = cast(
                "AsyncGenerator[Message]", query(prompt=prompt, options=options)
            )
            async with aclosing(stream) as messages:
                async for message in messages:
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                if block.text:
                                    produced_text = True
                                    yield ExplainEvent(type="text", text=block.text)
                            elif isinstance(block, ToolUseBlock):
                                yield ExplainEvent(
                                    type="tool", text=_tool_summary(block)
                                )
                    elif isinstance(message, ResultMessage):
                        fallback = message.result
                        if message.is_error:
                            detail = message.result or message.subtype
                            raise CoachProviderError(
                                f"claude-agent-sdk run failed: {detail}"
                            )
                        if message.total_cost_usd is not None:
                            logger.info(
                                "coach explain: %.4f USD, %d turn(s)",
                                message.total_cost_usd,
                                message.num_turns,
                            )
        except CoachProviderError:
            raise
        except Exception as exc:  # CLI missing, process death, transport
            raise CoachProviderError(
                f"claude-agent-sdk failed: {exc} — is the `claude` CLI "
                "installed and logged in?"
            ) from exc

        if not produced_text:
            fallback_text = (fallback or "").strip()
            if not fallback_text:
                raise CoachProviderError("claude-agent-sdk returned no text")
            yield ExplainEvent(type="text", text=fallback_text)

    async def chat(
        self,
        *,
        system_context: str,
        history: list[ChatMessage],
        message: str,
        toolkit: ChatToolkit,
        provider_state: str | None = None,
    ) -> AsyncGenerator[ChatEvent]:
        tools = _build_chat_tools(toolkit)
        allowed_tools = _chat_allowed_tools(toolkit)
        # The scope seed is re-supplied as part of the system prompt on
        # every call, resumed or not (docs/06-coach.md, "Providers": "system
        # prompt ... re-supplied on every call") -- only the per-call
        # `prompt` argument differs between a resume (just the new message)
        # and a fresh/replay run (the full transcript).
        system_prompt = f"{CHAT_SYSTEM_PROMPT}\n\n{system_context}"

        def build_options(resume: str | None) -> ClaudeAgentOptions:
            server = create_sdk_mcp_server(name=_MCP_SERVER_NAME, tools=tools)
            return ClaudeAgentOptions(
                model=self._model,
                system_prompt=system_prompt,
                max_turns=_CHAT_MAX_TURNS,
                mcp_servers={_MCP_SERVER_NAME: server},
                tools=[],  # no built-in Claude Code tools — only chat's own
                allowed_tools=allowed_tools,
                resume=resume,
            )

        chunks: list[str] = []
        session_id: str | None = None
        fallback: str | None = None
        produced_any = False

        async def run_once(
            prompt: str, resume: str | None
        ) -> AsyncGenerator[ChatEvent]:
            nonlocal session_id, fallback, produced_any
            stream = cast(
                "AsyncGenerator[Message]",
                query(prompt=prompt, options=build_options(resume)),
            )
            async with aclosing(stream) as messages:
                async for msg in messages:
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                if block.text:
                                    produced_any = True
                                    chunks.append(block.text)
                                    yield ChatEvent(type="text", text=block.text)
                            elif isinstance(block, ToolUseBlock):
                                produced_any = True
                                yield ChatEvent(
                                    type="tool", text=_chat_tool_summary(block)
                                )
                    elif isinstance(msg, ResultMessage):
                        session_id = msg.session_id
                        fallback = msg.result
                        if msg.is_error:
                            detail = msg.result or msg.subtype
                            raise CoachProviderError(
                                f"claude-agent-sdk run failed: {detail}"
                            )
                        if msg.total_cost_usd is not None:
                            logger.info(
                                "coach chat: %.4f USD, %d turn(s)",
                                msg.total_cost_usd,
                                msg.num_turns,
                            )

        async def run_wrapped(
            prompt: str, resume: str | None
        ) -> AsyncGenerator[ChatEvent]:
            try:
                async for event in run_once(prompt, resume):
                    yield event
            except CoachProviderError:
                raise
            except Exception as exc:  # CLI missing, process death, transport
                raise CoachProviderError(
                    f"claude-agent-sdk failed: {exc} — is the `claude` CLI "
                    "installed and logged in?"
                ) from exc

        replay_prompt = render_chat_prompt(history, message)
        try:
            if provider_state:
                async for event in run_wrapped(message, provider_state):
                    yield event
            else:
                async for event in run_wrapped(replay_prompt, None):
                    yield event
        except CoachProviderError:
            # Any resume failure falls back silently to a full replay
            # (docs/06-coach.md, "Providers") -- but only when nothing has
            # reached the caller yet. Once an event has streamed, the user
            # has already seen part of the resumed turn, so a silent
            # restart from scratch would duplicate or contradict it; that
            # is a real failure, not a quiet resume miss, and it surfaces
            # normally.
            if provider_state and not produced_any:
                chunks.clear()
                session_id = None
                fallback = None
                async for event in run_wrapped(replay_prompt, None):
                    yield event
            else:
                raise

        text = "".join(chunks).strip() or (fallback or "").strip()
        if not text:
            raise CoachProviderError("claude-agent-sdk returned no text")
        yield ChatEvent(type="done", text=text, provider_state=session_id)


# JSON schema for the low-level copilot.tools.Tool — the Copilot SDK has no
# equivalent of the Agent SDK's `{"fen": str}` shorthand.
_ANALYZE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fen": {"type": "string", "description": "FEN of the position to analyze."}
    },
    "required": ["fen"],
}

# A Copilot session that neither errors nor goes idle (a wedged CLI
# runtime) would otherwise hang its request forever — the SDK offers no
# deadline of its own. Generous: a report run with engine tool calls
# takes minutes, and explain's drain resets the clock on every event.
_SESSION_STALL_TIMEOUT = 600.0

# What analyze_position returns instead of calling the engine again once the
# budget is spent — both for the one-time grace round and for every runaway
# call after it, steering the model to wrap up rather than looping. Shared by
# explain() and complete() (with an analyst) — both enforce the same
# self-imposed-budget pattern, just against different turn budgets.
_ENGINE_BUDGET_EXHAUSTED = (
    "Engine analysis budget for this run is exhausted — finish your answer "
    "with the analysis already gathered."
)


class _ToolCallBudget:
    """Counts tool calls against a budget, with one grace round before the
    hard cutoff.

    The Copilot SDK has no built-in turn limit (unlike
    ClaudeAgentSdkProvider's `max_turns`), so CopilotSdkProvider counts
    calls itself and enforces the identical shape everywhere it needs a
    budget: complete()'s and explain()'s single engine tool, and chat()'s
    four tools sharing one budget. Every call within `max_calls` is "ok";
    the call at `max_calls + 1` is "grace" (one nudge to wrap up); every
    call after that is "cutoff" (the run must end).
    """

    def __init__(self, max_calls: int) -> None:
        self._max_calls = max_calls
        self._calls = 0

    def record_call(self) -> Literal["ok", "grace", "cutoff"]:
        self._calls += 1
        if self._calls <= self._max_calls:
            return "ok"
        if self._calls == self._max_calls + 1:
            return "grace"
        return "cutoff"

    @property
    def is_cut_off(self) -> bool:
        """True once `record_call` has returned "cutoff" at least once.

        chat()'s runaway teardown is not instantaneous: the session may
        still emit an assistant message between the cutoff tool result and
        the generator actually breaking out of its drain loop (a real
        network round trip, or -- in tests -- a fake session replaying its
        whole script synchronously). Content arriving after cutoff was
        never streamed to the caller as its own event, so it must not
        silently leak into the final concatenated `done` text either.
        """
        return self._calls > self._max_calls + 1


class CopilotSdkProvider:
    """Coach completions through the local GitHub Copilot CLI login.

    No API key anywhere: authentication and billing ride the user's
    Copilot seat. Requires the Copilot CLI runtime to be installed
    (`python -m copilot download-runtime`) and logged in
    (`copilot login`).
    """

    def __init__(self, model: str, system_prompt: str | None = None) -> None:
        self._model = model
        self._system_prompt = system_prompt

    async def complete(
        self, prompt: str, analyst: PositionAnalystFn | None = None
    ) -> str:
        chunks: list[str] = []
        error: CoachProviderError | None = None
        idle = asyncio.Event()

        def handle_event(event: SessionEvent) -> None:
            nonlocal error
            match event.data:
                case AssistantMessageData() as data:
                    if data.content:
                        chunks.append(data.content)
                case SessionErrorData() as data:
                    error = CoachProviderError(
                        f"github-copilot-sdk run failed: {data.message}"
                    )
                    idle.set()
                case SessionIdleData():
                    idle.set()
                case _:  # every other session-event type is irrelevant here
                    pass

        # A one-shot coaching completion (no analyst) needs no tools at all —
        # same reasoning as explain()'s built-in-tool lockdown. With an
        # analyst, register the same analyze_position custom tool explain()
        # does, under the report turn budget.
        tools: list[Tool] | None = None
        available_tools = ToolSet()
        if analyst is not None:
            engine_analyst = analyst  # narrowed: not None from here on
            budget = _ToolCallBudget(_REPORT_MAX_TURNS)

            async def handle_analyze(invocation: ToolInvocation) -> ToolResult:
                args = cast("dict[str, Any]", invocation.arguments or {})
                fen = str(args.get("fen", ""))
                status = budget.record_call()
                if status == "grace":
                    # One grace round: nudge the model to wrap up instead of
                    # cutting it off the instant it goes over budget.
                    return ToolResult(
                        text_result_for_llm=_ENGINE_BUDGET_EXHAUSTED,
                        result_type="success",
                    )
                if status == "cutoff":
                    # The grace round is spent and the model called again
                    # anyway — a runaway. Unlike explain()'s queue-based
                    # drain sentinel, complete() collects straight into
                    # `chunks`, so the cutoff is: set the idle event so the
                    # `await idle.wait()` below returns and the `async with`
                    # blocks disconnect the session instead of letting the
                    # run loop forever. Text already collected stands.
                    idle.set()
                    return ToolResult(
                        text_result_for_llm=_ENGINE_BUDGET_EXHAUSTED,
                        result_type="success",
                    )
                lines = await engine_analyst(fen)
                return ToolResult(
                    text_result_for_llm=_render_lines(lines), result_type="success"
                )

            tools = [
                Tool(
                    name=_ANALYZE_TOOL_NAME,
                    description=_ANALYZE_TOOL_DESCRIPTION,
                    parameters=_ANALYZE_TOOL_SCHEMA,
                    handler=handle_analyze,
                    # No permission prompt for our own tool — it is the only
                    # tool available_tools admits below, so nothing else can
                    # run.
                    skip_permission=True,
                )
            ]
            available_tools = ToolSet().add_custom(_ANALYZE_TOOL_NAME)

        try:
            async with CopilotClient() as client:
                session = await client.create_session(
                    model=self._model,
                    system_message=_system_message(self._system_prompt),
                    tools=tools,
                    available_tools=available_tools,
                )
                async with session:
                    unsubscribe = session.on(handle_event)
                    try:
                        await session.send(prompt)
                        async with asyncio.timeout(_SESSION_STALL_TIMEOUT):
                            await idle.wait()
                    finally:
                        unsubscribe()
        except CoachProviderError:
            raise
        except TimeoutError as exc:
            raise CoachProviderError(
                "github-copilot-sdk session stalled — no completion or "
                f"error within {int(_SESSION_STALL_TIMEOUT)}s"
            ) from exc
        except Exception as exc:  # runtime missing, process death, transport
            raise CoachProviderError(
                f"github-copilot-sdk failed: {exc} — is the Copilot CLI "
                "runtime installed and logged in? (python -m copilot "
                "download-runtime, then copilot login via the CLI)"
            ) from exc

        if error is not None:
            raise error

        text = "".join(chunks).strip()
        if not text:
            raise CoachProviderError("github-copilot-sdk returned no text")
        return text

    async def explain(
        self, prompt: str, analyst: PositionAnalystFn
    ) -> AsyncGenerator[ExplainEvent]:
        # Bridges the SDK's callback-based session.on() into the pull-based
        # generator the CoachProvider protocol requires. None is the
        # sentinel for "session went idle, stop draining"; an Exception
        # value is a session.error event to raise once dequeued, so it
        # surfaces at the right point in the yield sequence.
        queue: asyncio.Queue[ExplainEvent | Exception | None] = asyncio.Queue()
        produced_text = False
        budget = _ToolCallBudget(_EXPLAIN_MAX_TURNS)

        def handle_event(event: SessionEvent) -> None:
            nonlocal produced_text
            match event.data:
                case AssistantMessageData() as data:
                    if data.content:
                        produced_text = True
                        queue.put_nowait(ExplainEvent(type="text", text=data.content))
                case SessionErrorData() as data:
                    queue.put_nowait(
                        CoachProviderError(
                            f"github-copilot-sdk run failed: {data.message}"
                        )
                    )
                    queue.put_nowait(None)
                case SessionIdleData():
                    queue.put_nowait(None)
                case _:  # every other session-event type is irrelevant here
                    pass

        async def handle_analyze(invocation: ToolInvocation) -> ToolResult:
            # ToolInvocation.arguments is typed Any by the SDK (it's decoded
            # straight off the wire); our schema pins it to {"fen": string}.
            args = cast("dict[str, Any]", invocation.arguments or {})
            fen = str(args.get("fen", ""))
            status = budget.record_call()
            if status == "grace":
                # One grace round: nudge the model to wrap up instead of
                # cutting it off the instant it goes over budget.
                return ToolResult(
                    text_result_for_llm=_ENGINE_BUDGET_EXHAUSTED, result_type="success"
                )
            if status == "cutoff":
                # The model used its grace round and called again anyway —
                # a runaway. Enqueue the drain sentinel so explain()'s while
                # loop ends the generator; the `async with` teardown then
                # disconnects the session and cancels the run instead of
                # letting it loop forever. Text already yielded stands.
                queue.put_nowait(None)
                return ToolResult(
                    text_result_for_llm=_ENGINE_BUDGET_EXHAUSTED, result_type="success"
                )
            queue.put_nowait(ExplainEvent(type="tool", text=_analyze_summary(fen)))
            lines = await analyst(fen)
            return ToolResult(
                text_result_for_llm=_render_lines(lines), result_type="success"
            )

        analyze_tool = Tool(
            name=_ANALYZE_TOOL_NAME,
            description=_ANALYZE_TOOL_DESCRIPTION,
            parameters=_ANALYZE_TOOL_SCHEMA,
            handler=handle_analyze,
            # No permission prompt for our own tool — it is the only tool
            # available_tools admits below, so nothing else can run.
            skip_permission=True,
        )

        try:
            async with CopilotClient() as client:
                session = await client.create_session(
                    model=self._model,
                    system_message=_system_message(self._system_prompt),
                    tools=[analyze_tool],
                    # Built-in Copilot tools (shell, file edits, web) must
                    # not run: restrict the catalog to just our engine tool
                    # rather than relying on permission prompts to block them.
                    available_tools=ToolSet().add_custom(_ANALYZE_TOOL_NAME),
                )
                async with session:
                    unsubscribe = session.on(handle_event)
                    try:
                        await session.send(prompt)
                        while True:
                            # Per-event stall clock: a healthy session
                            # keeps events coming; only silence times
                            # out. The yield sits outside the timeout so
                            # consumer time never counts against it.
                            async with asyncio.timeout(_SESSION_STALL_TIMEOUT):
                                item = await queue.get()
                            if item is None:
                                break
                            if isinstance(item, Exception):
                                raise item
                            yield item
                    finally:
                        unsubscribe()
        except CoachProviderError:
            raise
        except TimeoutError as exc:
            raise CoachProviderError(
                "github-copilot-sdk session stalled — no event within "
                f"{int(_SESSION_STALL_TIMEOUT)}s"
            ) from exc
        except Exception as exc:  # runtime missing, process death, transport
            raise CoachProviderError(
                f"github-copilot-sdk failed: {exc} — is the Copilot CLI "
                "runtime installed and logged in? (python -m copilot "
                "download-runtime, then copilot login via the CLI)"
            ) from exc

        if not produced_text:
            raise CoachProviderError("github-copilot-sdk returned no text")

    async def chat(
        self,
        *,
        system_context: str,
        history: list[ChatMessage],
        message: str,
        toolkit: ChatToolkit,
        provider_state: str | None = None,
    ) -> AsyncGenerator[ChatEvent]:
        system_message = _system_message(f"{CHAT_SYSTEM_PROMPT}\n\n{system_context}")
        queue: asyncio.Queue[ChatEvent | Exception | None] = asyncio.Queue()
        budget = _ToolCallBudget(_CHAT_MAX_TURNS)
        chunks: list[str] = []

        def handle_event(event: SessionEvent) -> None:
            match event.data:
                case AssistantMessageData() as data:
                    # Once a tool call has tripped the cutoff, the run is
                    # being torn down -- any further content was never
                    # streamed to the caller as its own event, so it must
                    # not silently extend the final `done` text either
                    # (_ToolCallBudget.is_cut_off's docstring).
                    if data.content and not budget.is_cut_off:
                        chunks.append(data.content)
                        queue.put_nowait(ChatEvent(type="text", text=data.content))
                case SessionErrorData() as data:
                    queue.put_nowait(
                        CoachProviderError(
                            f"github-copilot-sdk run failed: {data.message}"
                        )
                    )
                    queue.put_nowait(None)
                case SessionIdleData():
                    queue.put_nowait(None)
                case _:  # every other session-event type is irrelevant here
                    pass

        tools, available_tools = _build_copilot_chat_tools(toolkit, queue, budget)
        session_id: str | None = None

        try:
            async with CopilotClient() as client:
                session: CopilotSession | None = None
                if provider_state:
                    # Resume when the runtime still holds this session,
                    # sending only the new message. Any failure here --
                    # unknown session, runtime restart -- falls back
                    # silently to a fresh session that replays the whole
                    # stored transcript (docs/06-coach.md, "Providers"):
                    # unlike ClaudeAgentSdkProvider's query(), this call
                    # fails or succeeds before anything is ever sent to the
                    # model, so there is no partial output to protect.
                    try:
                        session = await client.resume_session(
                            provider_state,
                            model=self._model,
                            system_message=system_message,
                            tools=tools,
                            available_tools=available_tools,
                        )
                    except Exception:
                        session = None
                prompt = message
                if session is None:
                    session = await client.create_session(
                        model=self._model,
                        system_message=system_message,
                        tools=tools,
                        available_tools=available_tools,
                    )
                    prompt = render_chat_prompt(history, message)
                session_id = session.session_id
                async with session:
                    unsubscribe = session.on(handle_event)
                    try:
                        await session.send(prompt)
                        while True:
                            # Per-event stall clock: a healthy session
                            # keeps events coming; only silence times
                            # out. The yield sits outside the timeout so
                            # consumer time never counts against it.
                            async with asyncio.timeout(_SESSION_STALL_TIMEOUT):
                                item = await queue.get()
                            if item is None:
                                break
                            if isinstance(item, Exception):
                                raise item
                            yield item
                    finally:
                        unsubscribe()
        except CoachProviderError:
            raise
        except TimeoutError as exc:
            raise CoachProviderError(
                "github-copilot-sdk session stalled — no event within "
                f"{int(_SESSION_STALL_TIMEOUT)}s"
            ) from exc
        except Exception as exc:  # runtime missing, process death, transport
            raise CoachProviderError(
                f"github-copilot-sdk failed: {exc} — is the Copilot CLI "
                "runtime installed and logged in? (python -m copilot "
                "download-runtime, then copilot login via the CLI)"
            ) from exc

        text = "".join(chunks).strip()
        if not text:
            raise CoachProviderError("github-copilot-sdk returned no text")
        # A budget cutoff tears the session down mid-thought: content
        # arriving after the cutoff lives in the warm session but was
        # excluded from `text` (handle_event's is_cut_off guard), so the
        # session no longer matches what gets persisted. Handing its id
        # back would make the next turn resume a conversation that
        # half-remembers text the student never saw — the exact
        # divergence the stored-transcript-is-truth rule exists to
        # prevent, so the cutoff path forces a replay instead.
        yield ChatEvent(
            type="done",
            text=text,
            provider_state=None if budget.is_cut_off else session_id,
        )


def _system_message(content: str | None) -> SystemMessageConfig | None:
    if content is None:
        return None
    # "replace" fully swaps in the coach persona, same as
    # ClaudeAgentSdkProvider's system_prompt — note its docstring warns this
    # also removes the CLI's own guardrails, which is why explain() locks
    # the tool catalog down independently via available_tools rather than
    # relying on any tool-calling guidance the replaced prompt might have
    # carried. If replace ever proves to break the tool flow in practice,
    # "append" is the documented fallback.
    return {"mode": "replace", "content": content}


def _analyze_summary(fen: str) -> str:
    if fen:
        return f"engine: analyzing {fen}"
    return "engine: analyzing position"


def _build_analyze_tool(analyst: PositionAnalystFn) -> SdkMcpTool[Any]:
    @tool(_ANALYZE_TOOL_NAME, _ANALYZE_TOOL_DESCRIPTION, {"fen": str})
    async def analyze_position(args: dict[str, Any]) -> dict[str, Any]:
        fen = str(args["fen"])
        lines = await analyst(fen)
        return {"content": [{"type": "text", "text": _render_lines(lines)}]}

    return analyze_position


def _tool_summary(block: ToolUseBlock) -> str:
    fen = block.input.get("fen")
    if isinstance(fen, str) and fen:
        return f"engine: analyzing {fen}"
    return "engine: analyzing position"


def _render_lines(lines: list[EvalLine]) -> str:
    if not lines:
        return "The engine returned no candidate lines for this position."
    rows = [
        f"{line.multipv}. depth {line.depth}, "
        f"{format_eval(line.eval_cp, line.eval_mate)}: {' '.join(line.pv_san)}"
        for line in lines
    ]
    return "\n".join(rows)


# --- chat tools (docs/06-coach.md, "Chat" -- "Tools") ----------------------
#
# One tool per ChatToolkit capability, registered by each provider through
# its own SDK mechanics (the in-process MCP server on Claude, custom Tools
# on Copilot) exactly as analyze_position already is. `_call_find_games`,
# `_call_get_game`, `_call_opening_stats` do the actual toolkit call plus
# result rendering, shared by both providers' tool handlers so the prompt
# style (docs/06-coach.md: pawns, never centipawns) stays owned in one
# place.


def _format_date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d")


def _pawns(value: float | None) -> str:
    """A centipawn aggregate as bare pawns, the tool-result twin of
    `prompt.py::_pawns_or_na`. The caller names the unit beside it: a
    tool result lands mid-conversation with no header to define
    anything, which is why none of them says "ACPL" (docs/06-coach.md,
    "Units").
    """
    return f"{value / 100:.2f}" if value is not None else "n/a"


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _opt_result(value: Any) -> Result | None:
    return cast("Result", value) if value in _RESULT_VALUES else None


def _opt_time_class(value: Any) -> TimeClass | None:
    return cast("TimeClass", value) if value in _TIME_CLASS_VALUES else None


async def _call_find_games(toolkit: ChatToolkit, args: dict[str, Any]) -> str:
    limit = args.get("limit")
    games = await toolkit.find_games(
        opponent=_opt_str(args.get("opponent")),
        opening=_opt_str(args.get("opening")),
        result=_opt_result(args.get("result")),
        time_class=_opt_time_class(args.get("time_class")),
        since=_opt_int(args.get("since")),
        until=_opt_int(args.get("until")),
        limit=int(limit) if isinstance(limit, (int, float)) else 10,
    )
    return _render_game_summaries(games)


async def _call_get_game(toolkit: ChatToolkit, args: dict[str, Any]) -> str:
    game_id = str(args.get("game_id", ""))
    detail = await toolkit.get_game(game_id)
    return _render_game_detail(detail, game_id)


async def _call_opening_stats(toolkit: ChatToolkit) -> str:
    rows = await toolkit.opening_stats()
    return _render_opening_stats(rows)


def _render_game_summaries(games: list[GameSummary]) -> str:
    if not games:
        return "No games matched."
    rows: list[str] = []
    for g in games:
        date = _format_date(g.end_time)
        opening = f", {g.opening.name}" if g.opening else ""
        rows.append(
            f"- {date}, {g.color} vs {g.opponent} "
            f"({g.player_rating} vs {g.opponent_rating}), {g.result}, "
            f"{g.time_class}{opening} -- id `{g.id}`"
        )
    return "\n".join(rows)


def _render_game_detail(detail: GameDetail | None, game_id: str) -> str:
    if detail is None:
        return f"No game found for id `{game_id}`."
    date = _format_date(detail.end_time)
    opening = f", {detail.opening.name}" if detail.opening else ""
    header = (
        f"{detail.username} played {detail.color} vs {detail.opponent} on "
        f"{date} ({detail.time_class}), result: {detail.result}{opening}. "
        f"id `{detail.id}`."
    )
    return f"{header}\n{_render_move_sheet(detail)}"


def _numbered_san(san_moves: list[str]) -> list[str]:
    tokens: list[str] = []
    for ply, san in enumerate(san_moves, start=1):
        move_number = (ply + 1) // 2
        prefix = f"{move_number}." if ply % 2 == 1 else f"{move_number}..."
        tokens.append(f"{prefix}{san}")
    return tokens


def _render_move_sheet(detail: GameDetail) -> str:
    if detail.analysis is None:
        return "Moves (unanalyzed): " + " ".join(_numbered_san(detail.san_moves))
    evals_by_ply = {e.ply: e for e in detail.analysis.evals}
    tokens: list[str] = []
    for ply, san in enumerate(detail.san_moves, start=1):
        move_number = (ply + 1) // 2
        prefix = f"{move_number}." if ply % 2 == 1 else f"{move_number}..."
        token = f"{prefix}{san}"
        move_eval = evals_by_ply.get(ply)
        if move_eval is not None and move_eval.judgment != "best":
            token += (
                f" ({move_eval.judgment}, "
                f"{format_eval(move_eval.eval_cp, move_eval.eval_mate)})"
            )
        tokens.append(token)
    return "Moves (evals in pawns; only inaccuracies and worse annotated): " + " ".join(
        tokens
    )


def _render_opening_stats(rows: list[OpeningStats]) -> str:
    if not rows:
        return "No repertoire data available."
    lines: list[str] = []
    for r in rows:
        role = "faced" if r.faced else "chosen"
        score = (r.wins + r.draws / 2) / r.games * 100 if r.games else 0.0
        lines.append(
            f"- {r.color}, {r.name} ({r.eco}, {role}): {r.system} "
            f"[{r.first_moves}], {r.games}g, {score:.0f}%, "
            f"opening avg loss {_pawns(r.opening_acpl)}, "
            f"game avg loss {_pawns(r.avg_cp_loss)} (pawns per move)"
        )
    return "\n".join(lines)


def _chat_tool_names(toolkit: ChatToolkit) -> list[str]:
    names: list[str] = []
    if toolkit.analyst is not None:
        names.append(_ANALYZE_TOOL_NAME)
    names += [_FIND_GAMES_TOOL_NAME, _GET_GAME_TOOL_NAME, _GET_OPENING_STATS_TOOL_NAME]
    return names


def _chat_allowed_tools(toolkit: ChatToolkit) -> list[str]:
    return [f"mcp__{_MCP_SERVER_NAME}__{name}" for name in _chat_tool_names(toolkit)]


def _chat_tool_summary(block: ToolUseBlock) -> str:
    name = block.name.rsplit("__", 1)[-1]  # mcp__engine__find_games -> find_games
    if name == _ANALYZE_TOOL_NAME:
        return _analyze_summary(str(block.input.get("fen", "")))
    if name == _FIND_GAMES_TOOL_NAME:
        return "looking up games"
    if name == _GET_GAME_TOOL_NAME:
        game_id = block.input.get("game_id")
        return f"looking up game {game_id}" if game_id else "looking up a game"
    if name == _GET_OPENING_STATS_TOOL_NAME:
        return "looking up the repertoire"
    return f"calling {name}"


def _build_find_games_tool(toolkit: ChatToolkit) -> SdkMcpTool[Any]:
    @tool(_FIND_GAMES_TOOL_NAME, _FIND_GAMES_TOOL_DESCRIPTION, _FIND_GAMES_TOOL_SCHEMA)
    async def find_games(args: dict[str, Any]) -> dict[str, Any]:
        text = await _call_find_games(toolkit, args)
        return {"content": [{"type": "text", "text": text}]}

    return find_games


def _build_get_game_tool(toolkit: ChatToolkit) -> SdkMcpTool[Any]:
    @tool(_GET_GAME_TOOL_NAME, _GET_GAME_TOOL_DESCRIPTION, _GET_GAME_TOOL_SCHEMA)
    async def get_game(args: dict[str, Any]) -> dict[str, Any]:
        text = await _call_get_game(toolkit, args)
        return {"content": [{"type": "text", "text": text}]}

    return get_game


def _build_opening_stats_tool(toolkit: ChatToolkit) -> SdkMcpTool[Any]:
    @tool(
        _GET_OPENING_STATS_TOOL_NAME,
        _GET_OPENING_STATS_TOOL_DESCRIPTION,
        _GET_OPENING_STATS_TOOL_SCHEMA,
    )
    async def get_opening_stats(args: dict[str, Any]) -> dict[str, Any]:
        text = await _call_opening_stats(toolkit)
        return {"content": [{"type": "text", "text": text}]}

    return get_opening_stats


def _build_chat_tools(toolkit: ChatToolkit) -> list[SdkMcpTool[Any]]:
    tools: list[SdkMcpTool[Any]] = []
    if toolkit.analyst is not None:
        tools.append(_build_analyze_tool(toolkit.analyst))
    tools.append(_build_find_games_tool(toolkit))
    tools.append(_build_get_game_tool(toolkit))
    tools.append(_build_opening_stats_tool(toolkit))
    return tools


async def _guarded_chat_tool_call(
    queue: asyncio.Queue[ChatEvent | Exception | None],
    budget: _ToolCallBudget,
    progress_text: str,
    call: Callable[[], Awaitable[str]],
) -> ToolResult:
    """Shared per-call plumbing for CopilotSdkProvider.chat's four tools:
    budget check, then either the wrap-up steer or a progress event
    followed by the real call. Generalizes explain()'s single-tool budget
    handling (_ToolCallBudget) across chat's four tools sharing one
    budget.
    """
    status = budget.record_call()
    if status == "grace":
        return ToolResult(
            text_result_for_llm=_CHAT_BUDGET_EXHAUSTED, result_type="success"
        )
    if status == "cutoff":
        queue.put_nowait(None)
        return ToolResult(
            text_result_for_llm=_CHAT_BUDGET_EXHAUSTED, result_type="success"
        )
    queue.put_nowait(ChatEvent(type="tool", text=progress_text))
    result_text = await call()
    return ToolResult(text_result_for_llm=result_text, result_type="success")


def _build_copilot_chat_tools(
    toolkit: ChatToolkit,
    queue: asyncio.Queue[ChatEvent | Exception | None],
    budget: _ToolCallBudget,
) -> tuple[list[Tool], ToolSet]:
    tools: list[Tool] = []
    names: list[str] = []

    if toolkit.analyst is not None:
        analyst = toolkit.analyst  # narrowed: not None from here on

        async def handle_analyze(invocation: ToolInvocation) -> ToolResult:
            args = cast("dict[str, Any]", invocation.arguments or {})
            fen = str(args.get("fen", ""))

            async def do_call() -> str:
                lines = await analyst(fen)
                return _render_lines(lines)

            return await _guarded_chat_tool_call(
                queue, budget, _analyze_summary(fen), do_call
            )

        tools.append(
            Tool(
                name=_ANALYZE_TOOL_NAME,
                description=_ANALYZE_TOOL_DESCRIPTION,
                parameters=_ANALYZE_TOOL_SCHEMA,
                handler=handle_analyze,
                skip_permission=True,
            )
        )
        names.append(_ANALYZE_TOOL_NAME)

    async def handle_find_games(invocation: ToolInvocation) -> ToolResult:
        args = cast("dict[str, Any]", invocation.arguments or {})
        return await _guarded_chat_tool_call(
            queue, budget, "looking up games", lambda: _call_find_games(toolkit, args)
        )

    tools.append(
        Tool(
            name=_FIND_GAMES_TOOL_NAME,
            description=_FIND_GAMES_TOOL_DESCRIPTION,
            parameters=_FIND_GAMES_TOOL_SCHEMA,
            handler=handle_find_games,
            skip_permission=True,
        )
    )
    names.append(_FIND_GAMES_TOOL_NAME)

    async def handle_get_game(invocation: ToolInvocation) -> ToolResult:
        args = cast("dict[str, Any]", invocation.arguments or {})
        game_id = str(args.get("game_id", ""))
        return await _guarded_chat_tool_call(
            queue,
            budget,
            f"looking up game {game_id}" if game_id else "looking up a game",
            lambda: _call_get_game(toolkit, args),
        )

    tools.append(
        Tool(
            name=_GET_GAME_TOOL_NAME,
            description=_GET_GAME_TOOL_DESCRIPTION,
            parameters=_GET_GAME_TOOL_SCHEMA,
            handler=handle_get_game,
            skip_permission=True,
        )
    )
    names.append(_GET_GAME_TOOL_NAME)

    async def handle_opening_stats(invocation: ToolInvocation) -> ToolResult:
        return await _guarded_chat_tool_call(
            queue,
            budget,
            "looking up the repertoire",
            lambda: _call_opening_stats(toolkit),
        )

    tools.append(
        Tool(
            name=_GET_OPENING_STATS_TOOL_NAME,
            description=_GET_OPENING_STATS_TOOL_DESCRIPTION,
            parameters=_GET_OPENING_STATS_TOOL_SCHEMA,
            handler=handle_opening_stats,
            skip_permission=True,
        )
    )
    names.append(_GET_OPENING_STATS_TOOL_NAME)

    available_tools = ToolSet()
    for name in names:
        available_tools.add_custom(name)
    return tools, available_tools


def create_provider(cfg: LlmConfig, api_key: str | None = None) -> CoachProvider:
    """Factory over the seam; every `LlmProvider` value has a class here.

    `assert_never` keeps that true: adding a value to the domain
    Literal without a branch here fails pyright. `api_key` is unused
    today — reserved for the planned API-backed providers.
    """
    if cfg.provider == "claude-agent-sdk":
        return ClaudeAgentSdkProvider(model=cfg.model, system_prompt=SYSTEM_PROMPT)
    if cfg.provider == "github-copilot":
        return CopilotSdkProvider(model=cfg.model, system_prompt=SYSTEM_PROMPT)
    assert_never(cfg.provider)
