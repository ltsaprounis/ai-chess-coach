"""LLM providers behind the CoachProvider seam (docs/06-coach.md)."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from typing import Any, Literal, Protocol, cast

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
from copilot import CopilotClient, SystemMessageConfig, ToolSet
from copilot.session_events import (
    AssistantMessageData,
    SessionErrorData,
    SessionEvent,
    SessionIdleData,
)
from copilot.tools import Tool, ToolInvocation, ToolResult
from pydantic import BaseModel

from chess_coach.coach.prompt import SYSTEM_PROMPT, format_eval
from chess_coach.domain import EvalLine, LlmConfig

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
# (docs/fixes-2026-07/04-report-engine-tool.md): a report run gets a couple
# of engine calls to verify concrete lines before asserting them, plus the
# final write-up. A separate constant from _EXPLAIN_MAX_TURNS because the
# two flows are tuned independently even though they share a value today.
# Same per-provider enforcement split — see ClaudeAgentSdkProvider.complete
# and CopilotSdkProvider.complete.
_REPORT_MAX_TURNS = 8

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


class ExplainEvent(BaseModel):
    """One streamed increment of a move explanation."""

    type: Literal["text", "tool"]
    text: str  # text chunk | tool-call summary


class CoachProviderError(Exception):
    """The provider could not produce advice."""


class CoachProvider(Protocol):
    async def complete(
        self, prompt: str, analyst: PositionAnalystFn | None = None
    ) -> str: ...
    def explain(
        self, prompt: str, analyst: PositionAnalystFn
    ) -> AsyncGenerator[ExplainEvent]: ...


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
            options = ClaudeAgentOptions(
                model=self._model,
                max_turns=1,
                system_prompt=self._system_prompt,
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


# JSON schema for the low-level copilot.tools.Tool — the Copilot SDK has no
# equivalent of the Agent SDK's `{"fen": str}` shorthand.
_ANALYZE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fen": {"type": "string", "description": "FEN of the position to analyze."}
    },
    "required": ["fen"],
}

# What analyze_position returns instead of calling the engine again once the
# budget is spent — both for the one-time grace round and for every runaway
# call after it, steering the model to wrap up rather than looping. Shared by
# explain() and complete() (with an analyst) — both enforce the same
# self-imposed-budget pattern, just against different turn budgets.
_ENGINE_BUDGET_EXHAUSTED = (
    "Engine analysis budget for this run is exhausted — finish your answer "
    "with the analysis already gathered."
)


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
            tool_calls = 0

            async def handle_analyze(invocation: ToolInvocation) -> ToolResult:
                nonlocal tool_calls
                tool_calls += 1
                args = cast("dict[str, Any]", invocation.arguments or {})
                fen = str(args.get("fen", ""))
                if tool_calls == _REPORT_MAX_TURNS + 1:
                    # One grace round: nudge the model to wrap up instead of
                    # cutting it off the instant it goes over budget.
                    return ToolResult(
                        text_result_for_llm=_ENGINE_BUDGET_EXHAUSTED,
                        result_type="success",
                    )
                if tool_calls > _REPORT_MAX_TURNS + 1:
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
                        await idle.wait()
                    finally:
                        unsubscribe()
        except CoachProviderError:
            raise
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
        tool_calls = 0

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
            nonlocal tool_calls
            tool_calls += 1
            # ToolInvocation.arguments is typed Any by the SDK (it's decoded
            # straight off the wire); our schema pins it to {"fen": string}.
            args = cast("dict[str, Any]", invocation.arguments or {})
            fen = str(args.get("fen", ""))
            if tool_calls == _EXPLAIN_MAX_TURNS + 1:
                # One grace round: nudge the model to wrap up instead of
                # cutting it off the instant it goes over budget.
                return ToolResult(
                    text_result_for_llm=_ENGINE_BUDGET_EXHAUSTED, result_type="success"
                )
            if tool_calls > _EXPLAIN_MAX_TURNS + 1:
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
                        while (item := await queue.get()) is not None:
                            if isinstance(item, Exception):
                                raise item
                            yield item
                    finally:
                        unsubscribe()
        except CoachProviderError:
            raise
        except Exception as exc:  # runtime missing, process death, transport
            raise CoachProviderError(
                f"github-copilot-sdk failed: {exc} — is the Copilot CLI "
                "runtime installed and logged in? (python -m copilot "
                "download-runtime, then copilot login via the CLI)"
            ) from exc

        if not produced_text:
            raise CoachProviderError("github-copilot-sdk returned no text")


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


def create_provider(cfg: LlmConfig, api_key: str | None = None) -> CoachProvider:
    """Factory over the seam; claude-agent-sdk and github-copilot ship today.

    `api_key` is reserved for the anthropic / azure-foundry providers.
    """
    if cfg.provider == "claude-agent-sdk":
        return ClaudeAgentSdkProvider(model=cfg.model, system_prompt=SYSTEM_PROMPT)
    if cfg.provider == "github-copilot":
        return CopilotSdkProvider(model=cfg.model, system_prompt=SYSTEM_PROMPT)
    raise CoachProviderError(
        f"llm provider {cfg.provider!r} is not implemented yet — "
        "set llm.provider to 'claude-agent-sdk' or 'github-copilot'"
    )
