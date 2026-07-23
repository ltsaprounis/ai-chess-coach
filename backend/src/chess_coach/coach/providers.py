"""LLM providers behind the CoachProvider seam (docs/06-coach.md)."""

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
from pydantic import BaseModel

from chess_coach.coach.prompt import SYSTEM_PROMPT, format_eval
from chess_coach.domain import EvalLine, LlmConfig

logger = logging.getLogger(__name__)

# The engine seam: FEN in, MultiPV lines out. The API layer injects this,
# wrapping the engine pool — components never import each other.
PositionAnalystFn = Callable[[str], Awaitable[list[EvalLine]]]

# Bounds the agentic explain() loop: enough turns for a couple of follow-up
# engine calls plus the final write-up, without letting it run away.
_EXPLAIN_MAX_TURNS = 8

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
    async def complete(self, prompt: str) -> str: ...
    def explain(
        self, prompt: str, analyst: PositionAnalystFn
    ) -> AsyncGenerator[ExplainEvent]: ...


class ClaudeAgentSdkProvider:
    """One-shot completion through the local Claude Code login.

    No API key anywhere: authentication and billing ride the user's
    Claude subscription. Requires the `claude` CLI to be installed
    and logged in on this machine.
    """

    def __init__(self, model: str, system_prompt: str | None = None) -> None:
        self._model = model
        self._system_prompt = system_prompt

    async def complete(self, prompt: str) -> str:
        options = ClaudeAgentOptions(
            model=self._model,
            max_turns=1,
            system_prompt=self._system_prompt,
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
    """Factory over the seam; only claude-agent-sdk ships today.

    `api_key` is reserved for the anthropic / azure-foundry providers.
    """
    if cfg.provider == "claude-agent-sdk":
        return ClaudeAgentSdkProvider(model=cfg.model, system_prompt=SYSTEM_PROMPT)
    raise CoachProviderError(
        f"llm provider {cfg.provider!r} is not implemented yet — "
        "set llm.provider to 'claude-agent-sdk'"
    )
