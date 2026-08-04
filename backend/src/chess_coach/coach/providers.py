"""LLM providers behind the CoachProvider seam (docs/06-coach.md)."""

import asyncio
import contextlib
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

from chess_coach.coach.comparisons import build_comparisons
from chess_coach.coach.context import build_move_context
from chess_coach.coach.prompt import (
    CHAT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    format_cp_loss,
    format_eval,
    render_chat_prompt,
)
from chess_coach.domain import (
    ChatMessage,
    Comparison,
    ComparisonGroup,
    ComparisonInput,
    EvalLine,
    GameDetail,
    GameSearchPage,
    LlmConfig,
    MoveEval,
    OpeningStats,
    Record,
    Result,
    ScanEventSpec,
    ScanMatch,
    ScanOutcome,
    ScanSpec,
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

# The chat toolkit's other tools (docs/06-coach.md, "Chat" --
# "Tools"): read-only lookups over the thread's player, pre-scoped by the
# API layer's ChatToolkit implementation -- the model passes filters,
# never a username.
_FIND_GAMES_TOOL_NAME = "find_games"
_GET_GAME_TOOL_NAME = "get_game"
_GET_OPENING_STATS_TOOL_NAME = "get_opening_stats"
_SCAN_GAMES_TOOL_NAME = "scan_games"
_COMPARE_TOOL_NAME = "compare_groups"

_FIND_GAMES_TOOL_DESCRIPTION = (
    "Search the student's own stored games by opponent, opening, result, "
    "time class, or date range (unix epoch seconds) -- this filters game "
    "METADATA, the row a game is stored under; it never looks at what "
    "happened on the board (use scan_games for that). Returns the total "
    "match count, then compact rows -- date, color, opponent with "
    "ratings, result, time class, opening, an unanalyzed marker, and the "
    "game id -- for `limit` games (default 10) starting at `offset`, "
    "most recent first; page past the total with `offset`. Call get_game "
    "with a returned id for full detail."
)
_GET_GAME_TOOL_DESCRIPTION = (
    "Look up one of the student's games by id (as returned by find_games "
    "or scan_games) and return its identity plus a compact move sheet: "
    "every move in SAN, with judgment and eval in pawns shown at the "
    "moves that matter. Pass `ply` to also get a position block for that "
    "one move -- FEN before and after, the played move, judgment, loss, "
    "the engine's best move, and the eval either side -- which is what "
    "hands analyze_position a position to check."
)
_GET_OPENING_STATS_TOOL_DESCRIPTION = (
    "Return the student's repertoire: one row per opening per color, "
    "each with the student's own move order, the full line as played, "
    "whether the name is the opponent's choice, the record, and the "
    "average loss in pawns per move for the opening phase and for the "
    "whole game."
)
_SCAN_GAMES_TOOL_DESCRIPTION = (
    "Search the student's games by what happened on the board, not by "
    "row metadata: find_games filters metadata (opponent, result, date); "
    "scan_games replays every matching game's moves and looks for one to "
    "three named events, in order, within the same game -- a single "
    'event is the common case, more express a chain like "castled, '
    'then sacrificed" (optionally within a ply window of the previous '
    'step\'s match). Events: "sacrifice" (a real, SEE-gated piece '
    'offer -- see `piece`/`sound_only`), "eval_swing" (a big '
    "player-POV eval change across one ply -- see "
    '`min_swing_pawns`/`direction`), "comeback" (won after standing '
    '3+ pawns worse at some point), "delivered_mate" (won by '
    'checkmate), "castled" (see `side`). Use it for questions no '
    'metadata filter can answer, e.g. "games where I sacrificed my '
    'queen" or "games I came back from losing badly."\n'
    "Every result opens with its own denominators: how many games were "
    "scanned out of how many eligible, how many of those had no stored "
    "analysis (soundness on those is simply unverified, never assumed "
    "either way), and whether the scan was truncated. Report those "
    "numbers as given -- never estimate coverage yourself.\n"
    'Matches are EXAMPLES to read, never a tendency: "2 of your last 10 '
    'games show a sound sacrifice" says nothing about how often that '
    "happens across the archive. compare_groups is the only tool that "
    "establishes a tendency; scan dimensions must never be treated as one.\n"
    "An offer made while a forced mate was already available is never a "
    "sacrifice -- the detail states whether it forced the mate home or was "
    "a slip the advantage absorbed.\n"
    "A sacrifice hit's ply anchors at the move after which the piece "
    "first sits capturable, not necessarily the move that offered it: "
    "when that move answered a check, or the piece fell only through a "
    "forcing sequence, an earlier move made the actual offer. Read the "
    "preceding moves before saying which move sacrificed."
)

_COMPARE_TOOL_DESCRIPTION = (
    "Compare one group of the student's games against the rest, and get "
    "back a verdict on whether the difference is real. This is the ONLY "
    "way to establish that a difference is a tendency -- a percentage "
    "you work out yourself from find_games or get_opening_stats has not "
    "been checked and must not be reported as one.\n"
    "Name the group by properties fixed before the game was played: "
    "color, opening, time class, date range. There is deliberately no "
    "result filter -- selecting games by how they ended and then "
    "measuring how they ended proves nothing.\n"
    "The other side is computed for you by subtracting the group from "
    "`within` (default: every game in scope), so you cannot accidentally "
    "compare a group against a set that contains it. Use `within` to "
    "pick the baseline that makes the comparison mean something -- an "
    "opening the student plays as Black belongs against their other "
    "Black games, not against every game they have played.\n"
    "Each call joins a family judged together, so asking more questions "
    "makes every answer harder to earn."
)

_RESULT_VALUES = frozenset({"win", "loss", "draw"})
_TIME_CLASS_VALUES = frozenset({"bullet", "blitz", "rapid", "daily"})
_SCAN_PIECE_VALUES = ("queen", "rook", "minor")
_SCAN_SIDE_VALUES = ("short", "long", "any")
_SCAN_DIRECTION_VALUES = ("gained", "lost")
# Every `ScanEventName` value now has a detector (coach/scan.py's
# `_EVENT_DETECTORS`), so the schema exposes the domain's full set.
_SCAN_EVENT_VALUES = (
    "sacrifice",
    "eval_swing",
    "comeback",
    "delivered_mate",
    "castled",
)

# The metadata filters find_games and scan_games share verbatim
# (docs/06-coach.md, "Chat": "Metadata filters mean exactly what
# find_games' do"), so the two schemas cannot drift apart.
_GAME_FILTER_PROPERTIES: dict[str, Any] = {
    "opponent": {
        "type": "string",
        "description": "Filter by opponent username (substring match).",
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
    "min_rating": {
        "type": "integer",
        "description": (
            "Only games where the student's own rating at game time was at least this."
        ),
    },
    "max_rating": {
        "type": "integer",
        "description": (
            "Only games where the student's own rating at game time was at most this."
        ),
    },
}

_FIND_GAMES_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_GAME_FILTER_PROPERTIES,
        "limit": {
            "type": "integer",
            "description": "Maximum rows to return (default 10).",
        },
        "offset": {
            "type": "integer",
            "description": (
                "Skip this many matches, newest first (default 0) -- page "
                "further under the result's own total."
            ),
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
        },
        "ply": {
            "type": "integer",
            "description": (
                "Optional 1-based ply (as in find_games/scan_games results) "
                "to inspect: appends the position before and after that "
                "move -- FEN, the played move, judgment, loss, the engine's "
                "best move, and the eval either side."
            ),
        },
    },
    "required": ["game_id"],
}
_GET_OPENING_STATS_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}

_SCAN_EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "event": {
            "type": "string",
            "enum": list(_SCAN_EVENT_VALUES),
            "description": "The move-content event to match.",
        },
        "piece": {
            "type": "string",
            "enum": list(_SCAN_PIECE_VALUES),
            "description": (
                "sacrifice: the tier of piece given up, resolved from what "
                'actually stood en prise. "rook" matches rook or queen; '
                '"minor" matches minor or better -- a pure pawn offer '
                'never matches any tier. Default "minor".'
            ),
        },
        "sound_only": {
            "type": "boolean",
            "description": (
                "sacrifice: drop matches where the player's eval after the "
                "move is negative, on games that have analysis. On "
                "unanalyzed games soundness is unknown, so the match is "
                "kept either way. Default false."
            ),
        },
        "min_swing_pawns": {
            "type": "number",
            "minimum": 1.0,
            "description": (
                "eval_swing: the minimum player-POV stored-eval change, in "
                "pawns, across one ply (mate folds to a large score). "
                "Requires analysis -- unanalyzed games never match this "
                "event. Default 3.0."
            ),
        },
        "direction": {
            "type": "string",
            "enum": list(_SCAN_DIRECTION_VALUES),
            "description": (
                'eval_swing: "gained" for a swing toward the player, "lost" '
                'for one against them. Default "gained".'
            ),
        },
        "side": {
            "type": "string",
            "enum": list(_SCAN_SIDE_VALUES),
            "description": (
                'castled: which side to match -- "short" (kingside), '
                '"long" (queenside), or "any". Default "any".'
            ),
        },
        "within_plies": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Steps after the first only: the maximum ply gap allowed "
                "to the previous step's match. Omit for no limit."
            ),
        },
    },
    "required": ["event"],
}
_SCAN_GAMES_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "match": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": _SCAN_EVENT_SCHEMA,
            "description": (
                "An ordered sequence of one to three event conditions "
                'within the same game -- a single event (e.g. [{"event": '
                '"sacrifice", "piece": "queen"}]) is the common case.'
            ),
        },
        **_GAME_FILTER_PROPERTIES,
        "limit": {
            "type": "integer",
            "description": "Maximum matches to return (default 10).",
        },
    },
    "required": ["match"],
}

_COMPARISON_GROUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "color": {
            "type": "string",
            "enum": ["white", "black"],
            "description": "Only games the student had this color in.",
        },
        "opening": {
            "type": "string",
            "description": "Opening name substring, e.g. 'Pirc'.",
        },
        "time_class": {
            "type": "string",
            "enum": ["bullet", "blitz", "rapid", "daily"],
            "description": "Only games at this time control.",
        },
        "since": {
            "type": "integer",
            "description": "Only games ending at or after this epoch second.",
        },
        "until": {
            "type": "integer",
            "description": "Only games ending before this epoch second.",
        },
    },
    "required": [],
}
_COMPARE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "group": {
            **_COMPARISON_GROUP_SCHEMA,
            "description": "The group of games to test.",
        },
        "within": {
            **_COMPARISON_GROUP_SCHEMA,
            "description": (
                "The baseline to read it against; the group is subtracted "
                "from it. Omit for every game in scope."
            ),
        },
    },
    "required": ["group"],
}

# What a chat tool call returns once _CHAT_MAX_TURNS's grace round (and
# every runaway call after it) is spent, in place of doing the real work --
# mirrors _ENGINE_BUDGET_EXHAUSTED below but phrased for any of chat's
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
        min_rating: int | None = None,
        max_rating: int | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> GameSearchPage: ...
    async def get_game(self, game_id: str) -> GameDetail | None: ...
    async def opening_stats(self) -> list[OpeningStats]: ...

    # The event scan (docs/06-coach.md, "Chat" -- "Tools"): coach owns the
    # event detectors and rendering; the API implementation owns the
    # candidate fetch, the denominators, and the wall-time budget. Metadata
    # filters mean exactly what find_games' do; the rating pair filters on
    # the student's own rating at game time.
    async def scan_games(
        self,
        spec: ScanSpec,
        *,
        opponent: str | None = None,
        opening: str | None = None,
        result: Result | None = None,
        time_class: TimeClass | None = None,
        since: int | None = None,
        until: int | None = None,
        min_rating: int | None = None,
        max_rating: int | None = None,
        limit: int = 10,
    ) -> ScanOutcome: ...

    # The comparison guard's data half (docs/06-coach.md, "Reading a
    # comparison"). Returns the group's record and the rest of `within`,
    # computed by subtraction -- the caller never supplies the other
    # side, so a run cannot compare a group against a set containing it.
    # Records and not scores: the coach needs W/D/L to get both the mean
    # and its variance.
    prior_comparisons: list[Comparison]

    async def compare_games(
        self,
        group: ComparisonGroup,
        within: ComparisonGroup | None = None,
    ) -> tuple[Record, Record]: ...


class CoachProviderError(Exception):
    """The provider could not produce advice."""


class CoachProvider(Protocol):
    async def complete(
        self,
        prompt: str,
        analyst: PositionAnalystFn | None = None,
        *,
        toolkit: ChatToolkit | None = None,
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
        self,
        prompt: str,
        analyst: PositionAnalystFn | None = None,
        *,
        toolkit: ChatToolkit | None = None,
    ) -> str:
        if toolkit is not None:
            # The agentic profile run (docs/06-coach.md, "Narrative"):
            # the same in-process MCP mechanics chat uses, with the full
            # read-only toolkit, so the narrative can read the
            # repertoire and pull games rather than paraphrasing the
            # aggregates it was handed. `toolkit` subsumes `analyst` --
            # it carries one of its own.
            options = ClaudeAgentOptions(
                model=self._model,
                system_prompt=self._system_prompt,
                max_turns=_REPORT_MAX_TURNS,
                mcp_servers={
                    _MCP_SERVER_NAME: create_sdk_mcp_server(
                        name=_MCP_SERVER_NAME, tools=_build_chat_tools(toolkit)
                    )
                },
                tools=[],  # no built-in Claude Code tools
                allowed_tools=_chat_allowed_tools(toolkit),
            )
        elif analyst is None:
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
                        elif isinstance(block, ToolUseBlock):
                            # Text written before a tool call is the model
                            # narrating its plan ("I'll verify the turning
                            # points first"), not the finished piece
                            # (docs/06-coach.md, "Providers"). Keeping it
                            # concatenated a preamble onto the front of
                            # every agentic brief.
                            chunks.clear()
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
                                # As in complete(): narration before a tool
                                # call is not the reply. It has already been
                                # streamed as its own text event, so the
                                # student still watches the coach work --
                                # only the `done` text the API persists and
                                # replays drops it.
                                chunks.clear()
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
    tools sharing one budget. Every call within `max_calls` is "ok";
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
        self,
        prompt: str,
        analyst: PositionAnalystFn | None = None,
        *,
        toolkit: ChatToolkit | None = None,
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
        drainer: asyncio.Task[None] | None = None
        if toolkit is not None:
            # The agentic profile run, with chat's full read-only
            # toolkit (docs/06-coach.md, "Narrative"). Chat's tools
            # report progress onto a queue a streaming consumer drains;
            # complete() has no stream, so the queue is drained here and
            # its one meaningful item -- the cutoff sentinel -- is
            # translated into the idle event this method already waits
            # on, which is how its own runaway path ends the run.
            queue: asyncio.Queue[ChatEvent | Exception | None] = asyncio.Queue()
            tools, available_tools = _build_copilot_chat_tools(
                toolkit, queue, _ToolCallBudget(_REPORT_MAX_TURNS), chunks.clear
            )

            async def drain() -> None:
                while True:
                    item = await queue.get()
                    if item is None:
                        idle.set()
                        return

            drainer = asyncio.create_task(drain())
        elif analyst is not None:
            engine_analyst = analyst  # narrowed: not None from here on
            budget = _ToolCallBudget(_REPORT_MAX_TURNS)

            async def handle_analyze(invocation: ToolInvocation) -> ToolResult:
                args = cast("dict[str, Any]", invocation.arguments or {})
                fen = str(args.get("fen", ""))
                status = budget.record_call()
                if status != "cutoff":
                    # Narration before a tool call is not the brief, exactly
                    # as in ClaudeAgentSdkProvider.complete(). Cutoff is
                    # excluded because it tears the run down: no further
                    # text is coming, so clearing there could only destroy
                    # the last thing left to return.
                    chunks.clear()
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
        finally:
            # `finally`, not the success path: every `except` above
            # re-raises, so cancelling after them left a task suspended
            # on `queue.get()` forever on each failed agentic run
            # (GUIDELINES.md, "no fire-and-forget tasks"). Awaiting the
            # cancellation is what makes it tracked rather than merely
            # signalled.
            if drainer is not None:
                drainer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drainer

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

        tools, available_tools = _build_copilot_chat_tools(
            toolkit, queue, budget, chunks.clear
        )
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
    offset = args.get("offset")
    page = await toolkit.find_games(
        opponent=_opt_str(args.get("opponent")),
        opening=_opt_str(args.get("opening")),
        result=_opt_result(args.get("result")),
        time_class=_opt_time_class(args.get("time_class")),
        since=_opt_int(args.get("since")),
        until=_opt_int(args.get("until")),
        min_rating=_opt_int(args.get("min_rating")),
        max_rating=_opt_int(args.get("max_rating")),
        limit=int(limit) if isinstance(limit, (int, float)) else 10,
        offset=int(offset) if isinstance(offset, (int, float)) else 0,
    )
    return _render_game_summaries(page)


async def _call_get_game(toolkit: ChatToolkit, args: dict[str, Any]) -> str:
    game_id = str(args.get("game_id", ""))
    detail = await toolkit.get_game(game_id)
    return _render_game_detail(detail, game_id, _opt_int(args.get("ply")))


async def _call_opening_stats(toolkit: ChatToolkit) -> str:
    rows = await toolkit.opening_stats()
    return _render_opening_stats(rows)


def _move_prefix(ply: int) -> str:
    move_number = (ply + 1) // 2
    return f"{move_number}." if ply % 2 == 1 else f"{move_number}..."


def _render_game_summaries(page: GameSearchPage) -> str:
    if not page.games:
        if page.total > 0:
            # An offset past the end must not read as "no games": the
            # model was just told the total, and losing it here is the
            # dishonesty the header exists to prevent.
            return f"Matched {page.total} games; nothing at offset {page.offset}."
        return "No games matched."
    first = page.offset + 1
    last = page.offset + len(page.games)
    header = f"Matched {page.total} games; showing {first}-{last}, newest first."
    rows: list[str] = []
    for g in page.games:
        date = _format_date(g.end_time)
        opening = f", {g.opening.name}" if g.opening else ""
        unanalyzed = "" if g.analyzed else ", unanalyzed"
        rows.append(
            f"- {date}, {g.color} vs {g.opponent} "
            f"({g.player_rating} vs {g.opponent_rating}), {g.result}, "
            f"{g.time_class}{opening}{unanalyzed} -- id `{g.id}`"
        )
    return "\n".join([header, *rows])


def _render_game_detail(
    detail: GameDetail | None, game_id: str, ply: int | None = None
) -> str:
    if detail is None:
        return f"No game found for id `{game_id}`."
    date = _format_date(detail.end_time)
    opening = f", {detail.opening.name}" if detail.opening else ""
    header = (
        f"{detail.username} played {detail.color} vs {detail.opponent} on "
        f"{date} ({detail.time_class}), result: {detail.result}{opening}. "
        f"id `{detail.id}`."
    )
    sections = [header, _render_move_sheet(detail)]
    if ply is not None:
        sections.append(_render_position_block(detail, ply))
    return "\n".join(sections)


def _numbered_san(san_moves: list[str]) -> list[str]:
    return [f"{_move_prefix(ply)}{san}" for ply, san in enumerate(san_moves, start=1)]


def _worth_annotating(move_eval: MoveEval, san: str) -> bool:
    """The move-sheet annotation rule (docs/06-coach.md, "Chat"): widened
    from "not the engine's best" alone, since a sound sacrifice is by
    definition engine-best and would otherwise render as bare SAN --
    invisible to a student asking about it. Captures are detected from
    the SAN token itself (no board replay needed here); mate scores are
    always worth showing regardless of judgment.
    """
    return move_eval.judgment != "best" or "x" in san or move_eval.eval_mate is not None


def _render_move_sheet(detail: GameDetail) -> str:
    if detail.analysis is None:
        return "Moves (unanalyzed): " + " ".join(_numbered_san(detail.san_moves))
    evals_by_ply = {e.ply: e for e in detail.analysis.evals}
    tokens: list[str] = []
    for ply, san in enumerate(detail.san_moves, start=1):
        token = f"{_move_prefix(ply)}{san}"
        move_eval = evals_by_ply.get(ply)
        if move_eval is not None and _worth_annotating(move_eval, san):
            eval_str = format_eval(move_eval.eval_cp, move_eval.eval_mate)
            if move_eval.judgment == "best":
                # A best-move annotation (a capture or a mate score) is
                # the eval alone -- there is no judgment word for "best"
                # to pair with it.
                token += f" ({eval_str})"
            else:
                token += f" ({move_eval.judgment}, {eval_str})"
        tokens.append(token)
    return (
        "Moves (evals in pawns; annotated on inaccuracies and worse, "
        "captures, and mate scores): "
    ) + " ".join(tokens)


def _render_position_block(detail: GameDetail, ply: int) -> str:
    """`get_game`'s optional `ply` addition (docs/06-coach.md, "Chat"):
    the position before and after one move, which is what hands
    `analyze_position` a position for any moment of any stored game.
    Degrades to a one-line note on an unanalyzed game or an out-of-range
    ply -- the rest of the move sheet still rendered above it.
    """
    analysis = detail.analysis
    if analysis is None:
        return f"Position at ply {ply}: not available -- this game is unanalyzed."
    try:
        ctx = build_move_context(detail, analysis, detail.opening, ply)
    except ValueError as exc:
        # build_move_context raises for an out-of-range ply AND for an
        # in-range ply with no recorded eval; its own message names
        # which, so don't overwrite it with a guess.
        return f"Position at ply {ply}: not available -- {exc}."
    evals_by_ply = {e.ply: e for e in analysis.evals}
    move_eval = evals_by_ply[ply]
    before = evals_by_ply.get(ply - 1)
    before_str = format_eval(
        before.eval_cp if before else None, before.eval_mate if before else None
    )
    after_str = format_eval(move_eval.eval_cp, move_eval.eval_mate)
    return (
        f"Position at ply {ply} ({ctx.san}):\n"
        f"FEN before: `{ctx.fen_before}`\n"
        f"FEN after: `{ctx.fen_after}`\n"
        f"Judgment: {ctx.judgment}, loss {format_cp_loss(ctx.cp_loss)}\n"
        f"Engine best move: {ctx.best_move}\n"
        f"Eval before: {before_str}, eval after: {after_str}"
    )


# Printed above every repertoire dump. The rows are (colour, ECO, name)
# groups and the moves beside them are the group's *commonest* line, not
# a filter -- docs/06-coach.md says so under "Repertoire", and saying it
# only there was not enough: a live narrative read
# "[1.e4 d6 2.d4 ...], 32g, 33%" as "32 games where White played 2.d4",
# and reported a prep hole at 33% where the real 2.d4 split is 46% over
# 153 games. A move sequence printed beside a count reads as a filter
# unless something says otherwise, so this says otherwise.
_OPENING_STATS_PREAMBLE = (
    "One row per (colour, ECO, name) group. The moves in brackets are "
    "that group's MOST COMMON line, not a filter: transpositions reach "
    "the same name by other move orders, so a row's games are not only "
    "the games with those moves, and its score says nothing about that "
    "move order specifically. To compare move orders, read games with "
    "find_games; to test whether a difference is real, use "
    f"{_COMPARE_TOOL_NAME}."
)


def _render_opening_stats(rows: list[OpeningStats]) -> str:
    if not rows:
        return "No repertoire data available."
    lines: list[str] = [_OPENING_STATS_PREAMBLE, ""]
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


# --- scan_games (docs/06-coach.md, "Chat") ---------------------------------


def _scan_event_spec(args: dict[str, Any]) -> ScanEventSpec:
    """One `match` array entry as a `ScanEventSpec`, ignoring any field
    the schema does not name for that call -- mirrors
    `_comparison_group`'s stance on stray model-supplied fields.
    """
    fields: dict[str, Any] = {"event": args.get("event")}
    for key in ("piece", "sound_only", "direction", "side"):
        if args.get(key) is not None:
            fields[key] = args[key]
    min_swing = args.get("min_swing_pawns")
    if isinstance(min_swing, (int, float)):
        # Clamped alongside the schema minimums: not every provider
        # enforces JSON-schema bounds, and a sub-pawn threshold (or a
        # negative gap) degrades to spam rather than erroring.
        fields["min_swing_pawns"] = max(1.0, float(min_swing))
    within_plies = _opt_int(args.get("within_plies"))
    if within_plies is not None:
        fields["within_plies"] = max(1, within_plies)
    return ScanEventSpec.model_validate(fields)


async def _call_scan_games(toolkit: ChatToolkit, args: dict[str, Any]) -> str:
    match_args = args.get("match")
    match_list = cast("list[Any]", match_args) if isinstance(match_args, list) else []
    steps = [_scan_event_spec(cast("dict[str, Any]", step)) for step in match_list]
    if not steps:
        # An empty `match` would scan nothing and render as "no games
        # matched" -- a wrong answer the model cannot distinguish from a
        # real miss. Say what actually happened so it can correct.
        return "scan_games needs at least one event in `match`; nothing was scanned."
    limit = args.get("limit")
    outcome = await toolkit.scan_games(
        ScanSpec(match=steps),
        opponent=_opt_str(args.get("opponent")),
        opening=_opt_str(args.get("opening")),
        result=_opt_result(args.get("result")),
        time_class=_opt_time_class(args.get("time_class")),
        since=_opt_int(args.get("since")),
        until=_opt_int(args.get("until")),
        min_rating=_opt_int(args.get("min_rating")),
        max_rating=_opt_int(args.get("max_rating")),
        limit=int(limit) if isinstance(limit, (int, float)) else 10,
    )
    return _render_scan_outcome(outcome)


def _scan_preamble(outcome: ScanOutcome) -> str:
    """The coverage-honesty statement every scan result opens with
    (docs/06-coach.md, "Chat"): scanned vs eligible, how many of those
    were unanalyzed (soundness on them is unverified, not assumed),
    whether the wall-time budget truncated the sweep, and -- only when
    it happened at all -- how many more an eval-reading event had to
    skip outright.

    A truncated sweep is always continuable exactly where it stopped:
    `resume_until` is the oldest scanned game's `end_time`, so passing
    it back as `until` picks up right after the covered slice. Stated
    here as well as carried on the outcome, since the model reads this
    preamble, not the struct.
    """
    truncated = "yes" if outcome.truncated else "no"
    # "all" belongs only to the untruncated case; a truncated sweep
    # covered the newest slice, and saying "all" there would be the
    # coverage lie this preamble exists to prevent.
    scope = (
        f"all {outcome.scanned}"
        if not outcome.truncated
        else (f"the newest {outcome.scanned}")
    )
    preamble = (
        f"Scanned {scope} of {outcome.eligible} games "
        f"matching the filters ({outcome.unverified_scanned} without "
        f"analysis: soundness unverified; truncated: {truncated})."
    )
    if outcome.skipped_unanalyzed:
        preamble += (
            f" {outcome.skipped_unanalyzed} more without analysis could "
            "not be scanned for this event at all."
        )
    if outcome.truncated and outcome.resume_until is not None:
        preamble += (
            f" Covered down to {_format_date(outcome.resume_until)}; to "
            f"continue the sweep, repeat the call with "
            f"until={outcome.resume_until}."
        )
    return preamble


def _render_scan_match(match: ScanMatch) -> str:
    g = match.game
    date = _format_date(g.end_time)
    opening = f", {g.opening.name}" if g.opening else ""
    unanalyzed = "" if g.analyzed else ", unanalyzed"
    header = (
        f"- {date}, {g.color} vs {g.opponent}, {g.result}, "
        f"{g.time_class}{opening}{unanalyzed} -- id `{g.id}`"
    )
    hit_lines = [
        f"  {_move_prefix(hit.ply)}{hit.san}: {hit.detail} (`{hit.fen_before}`)"
        for hit in match.hits
    ]
    return "\n".join([header, *hit_lines])


def _render_scan_outcome(outcome: ScanOutcome) -> str:
    lines = [_scan_preamble(outcome)]
    if not outcome.matches:
        lines.append("No games matched.")
        return "\n".join(lines)
    lines += [_render_scan_match(match) for match in outcome.matches]
    return "\n".join(lines)


def _chat_tool_names(toolkit: ChatToolkit) -> list[str]:
    names: list[str] = []
    if toolkit.analyst is not None:
        names.append(_ANALYZE_TOOL_NAME)
    names += [
        _FIND_GAMES_TOOL_NAME,
        _GET_GAME_TOOL_NAME,
        _GET_OPENING_STATS_TOOL_NAME,
        _COMPARE_TOOL_NAME,
        _SCAN_GAMES_TOOL_NAME,
    ]
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
    if name == _COMPARE_TOOL_NAME:
        return "checking whether a difference is real"
    if name == _GET_OPENING_STATS_TOOL_NAME:
        return "looking up the repertoire"
    if name == _SCAN_GAMES_TOOL_NAME:
        return "scanning games for events"
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


def _build_scan_games_tool(toolkit: ChatToolkit) -> SdkMcpTool[Any]:
    @tool(_SCAN_GAMES_TOOL_NAME, _SCAN_GAMES_TOOL_DESCRIPTION, _SCAN_GAMES_TOOL_SCHEMA)
    async def scan_games(args: dict[str, Any]) -> dict[str, Any]:
        text = await _call_scan_games(toolkit, args)
        return {"content": [{"type": "text", "text": text}]}

    return scan_games


class _ComparisonLedger:
    """The BH family for one run (docs/06-coach.md, "Reading a
    comparison").

    Seeded with whatever the profile already judged, then grown by each
    `compare_groups` call. Every call re-judges the whole family, so a
    run that fishes raises its own bar -- and a verdict may change
    between calls, which is not a bug: Benjamini-Hochberg is a property
    of the family, so the fourteenth question genuinely does change what
    the first one supports.

    One ledger per run, held by the tool closure, so two concurrent runs
    never share a family.
    """

    def __init__(self, prior: list[Comparison]) -> None:
        self._prior = list(prior)
        self._asked: list[ComparisonInput] = []

    def add(self, pair: ComparisonInput) -> tuple[Comparison, int]:
        """Judge `pair` against the whole family; return it and the
        family size."""
        self._asked.append(pair)
        family = [
            ComparisonInput(
                label=c.label,
                left_label=c.left_label,
                left=c.left,
                right_label=c.right_label,
                right=c.right,
            )
            for c in self._prior
        ] + self._asked
        judged = build_comparisons(family)
        return judged[-1], len(family)


def _render_comparison(row: Comparison, family: int) -> str:
    """The tool result: the two records, the verdict, and the family
    size -- never a sigma or a p-value, which are not this audience's
    vocabulary and invite the false confidence the guard removes."""
    if not row.measurable:
        return (
            f"{row.left.games} game(s) {row.left_label} against "
            f"{row.right.games} {row.right_label} -- too few to compare. "
            "This is not a tendency and must not be reported as one."
        )
    left = _score_pct(row.left)
    right = _score_pct(row.right)
    verdict = (
        "a real difference, larger than chance accounts for"
        if row.significant
        else (
            "WITHIN NOISE -- a difference this many games cannot tell apart "
            "from chance. Not a tendency; do not report it as one, and do "
            'not soften it into "worth watching"'
        )
    )
    return (
        f"{left} over {row.left.games} games {row.left_label}, against "
        f"{right} over {row.right.games} games {row.right_label}. "
        f"Verdict: {verdict}. "
        f"(Judged together with {family - 1} other comparison(s) in this "
        "profile; asking more makes each one harder to earn.)"
    )


def _score_pct(record: Record) -> str:
    if not record.games:
        return "n/a"
    return f"{(record.wins + record.draws / 2) / record.games * 100:.0f}%"


def _comparison_group(args: dict[str, Any]) -> ComparisonGroup:
    """A tool argument object as a group, ignoring anything the schema
    does not name -- notably `result`, which a model may try anyway and
    which must never reach a comparison (docs/06-coach.md)."""
    return ComparisonGroup.model_validate(
        {
            key: args[key]
            for key in ("color", "opening", "time_class", "since", "until")
            if args.get(key) is not None
        }
    )


async def _run_comparison(
    toolkit: ChatToolkit, ledger: _ComparisonLedger, args: dict[str, Any]
) -> str:
    group_args = cast("dict[str, Any]", args.get("group") or {})
    within_args = cast("dict[str, Any]", args.get("within") or {})
    group = _comparison_group(group_args)
    within = _comparison_group(within_args) if within_args else None
    left, right = await toolkit.compare_games(group, within)
    baseline = within.label() if within is not None else "in every game"
    row, family = ledger.add(
        ComparisonInput(
            label=group.label(),
            left_label=group.label(),
            left=left,
            right_label=f"in their other games {baseline}".replace(
                "in their other games in every game", "in their other games"
            ),
            right=right,
        )
    )
    return _render_comparison(row, family)


def _build_chat_tools(toolkit: ChatToolkit) -> list[SdkMcpTool[Any]]:
    tools: list[SdkMcpTool[Any]] = []
    if toolkit.analyst is not None:
        tools.append(_build_analyze_tool(toolkit.analyst))
    tools.append(_build_find_games_tool(toolkit))
    tools.append(_build_get_game_tool(toolkit))
    tools.append(_build_opening_stats_tool(toolkit))
    # One ledger per build, so the BH family belongs to this run alone.
    tools.append(
        _build_compare_tool(toolkit, _ComparisonLedger(toolkit.prior_comparisons))
    )
    tools.append(_build_scan_games_tool(toolkit))
    return tools


def _build_compare_tool(
    toolkit: ChatToolkit, ledger: _ComparisonLedger
) -> SdkMcpTool[Any]:
    @tool(_COMPARE_TOOL_NAME, _COMPARE_TOOL_DESCRIPTION, _COMPARE_TOOL_SCHEMA)
    async def compare_groups(args: dict[str, Any]) -> dict[str, Any]:
        text = await _run_comparison(toolkit, ledger, args)
        return {"content": [{"type": "text", "text": text}]}

    return compare_groups


async def _guarded_chat_tool_call(
    queue: asyncio.Queue[ChatEvent | Exception | None],
    budget: _ToolCallBudget,
    progress_text: str,
    call: Callable[[], Awaitable[str]],
    on_call: Callable[[], None],
) -> ToolResult:
    """Shared per-call plumbing for CopilotSdkProvider.chat's tools:
    budget check, then either the wrap-up steer or a progress event
    followed by the real call. Generalizes explain()'s single-tool budget
    handling (_ToolCallBudget) across chat's tools sharing one budget.

    `on_call` drops the text narrated before this call, the same rule the
    other three providers' paths apply inline (docs/06-coach.md,
    "Providers"). It runs here rather than where the caller drains the
    queue because `chunks` is appended at *enqueue* time: by the time a
    consumer dequeued this tool event, text belonging after it could
    already have arrived.
    """
    status = budget.record_call()
    if status != "cutoff":
        # Cutoff excluded for the same reason as complete()'s: it tears
        # the run down, so no further text is coming and clearing could
        # only destroy the last thing left to return.
        on_call()
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
    on_tool_call: Callable[[], None],
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
                queue, budget, _analyze_summary(fen), do_call, on_tool_call
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
            queue,
            budget,
            "looking up games",
            lambda: _call_find_games(toolkit, args),
            on_tool_call,
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
            on_tool_call,
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

    ledger = _ComparisonLedger(toolkit.prior_comparisons)

    async def handle_compare(invocation: ToolInvocation) -> ToolResult:
        args = cast("dict[str, Any]", invocation.arguments or {})
        return await _guarded_chat_tool_call(
            queue,
            budget,
            "checking whether a difference is real",
            lambda: _run_comparison(toolkit, ledger, args),
            on_tool_call,
        )

    tools.append(
        Tool(
            name=_COMPARE_TOOL_NAME,
            description=_COMPARE_TOOL_DESCRIPTION,
            parameters=_COMPARE_TOOL_SCHEMA,
            handler=handle_compare,
            skip_permission=True,
        )
    )
    names.append(_COMPARE_TOOL_NAME)

    async def handle_opening_stats(invocation: ToolInvocation) -> ToolResult:
        return await _guarded_chat_tool_call(
            queue,
            budget,
            "looking up the repertoire",
            lambda: _call_opening_stats(toolkit),
            on_tool_call,
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

    async def handle_scan_games(invocation: ToolInvocation) -> ToolResult:
        args = cast("dict[str, Any]", invocation.arguments or {})
        return await _guarded_chat_tool_call(
            queue,
            budget,
            "scanning games for events",
            lambda: _call_scan_games(toolkit, args),
            on_tool_call,
        )

    tools.append(
        Tool(
            name=_SCAN_GAMES_TOOL_NAME,
            description=_SCAN_GAMES_TOOL_DESCRIPTION,
            parameters=_SCAN_GAMES_TOOL_SCHEMA,
            handler=handle_scan_games,
            skip_permission=True,
        )
    )
    names.append(_SCAN_GAMES_TOOL_NAME)

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
