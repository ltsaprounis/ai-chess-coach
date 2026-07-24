"""Coach component tests (docs/06-coach.md)."""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from copilot import ToolSet
from copilot.session_events import (
    AssistantMessageData,
    SessionErrorData,
    SessionEvent,
    SessionEventType,
    SessionIdleData,
)
from copilot.tools import Tool, ToolInvocation, ToolResult

import chess_coach.coach.providers as providers_module
from chess_coach.coach import (
    ClaudeAgentSdkProvider,
    CoachProvider,
    CoachProviderError,
    CopilotSdkProvider,
    ExplainEvent,
    MoveContext,
    build_move_context,
    build_report,
    create_provider,
    render_explain_prompt,
    render_prompt,
)
from chess_coach.domain import (
    AnalyzedGame,
    EvalLine,
    GameAnalysis,
    LlmConfig,
    MoveEval,
    Opening,
)
from tests.factories import make_analysis, make_game

RUY = Opening(eco="C60", name="Ruy Lopez", ply=5)


def analyzed(
    game_id: str,
    *,
    result: str = "win",
    opening: Opening | None = RUY,
    analysis: GameAnalysis | None = None,
) -> AnalyzedGame:
    game = make_game(id=game_id, result=result)
    return AnalyzedGame.model_validate(
        {
            **game.model_dump(),
            "opening": opening,
            "analysis": analysis or make_analysis(game_id=game_id),
        }
    )


def test_build_report_aggregates_player_stats() -> None:
    report = build_report(
        "testuser",
        [
            analyzed("g-1", result="win"),
            analyzed("g-2", result="loss", opening=None),
        ],
    )

    assert report.username == "testuser"
    assert report.games_analyzed == 2
    assert report.overall_acpl == 2.5  # both factory analyses are 2.5
    assert report.acpl_by_phase["opening"] == 2.5
    assert report.judgment_counts["best"] == 2
    # Only the classified game contributes to the repertoire.
    assert [(s.eco, s.games, s.wins) for s in report.openings] == [("C60", 1, 1)]
    assert report.openings[0].avg_cp_loss == 2.5
    assert report.openings[0].analyzed_games == 1


def test_openings_sorted_worst_first() -> None:
    games = [
        analyzed("w1", result="win"),
        analyzed("w2", result="win"),
        analyzed(
            "l1",
            result="loss",
            opening=Opening(eco="D06", name="Queen's Gambit", ply=3),
        ),
    ]
    report = build_report("testuser", games)
    assert [s.eco for s in report.openings] == ["D06", "C60"]


def test_critical_positions_replay_to_fen() -> None:
    # White's third move (Nf3, index 2 -> ply 3) loses 300 cp; the
    # position before it is after 1. e4 e5.
    evals = [
        MoveEval(
            ply=1,
            san="e4",
            eval_cp=30,
            eval_mate=None,
            best_move="e2e4",
            cp_loss=0,
            judgment="best",
        ),
        MoveEval(
            ply=2,
            san="e5",
            eval_cp=30,
            eval_mate=None,
            best_move="e7e5",
            cp_loss=0,
            judgment="best",
        ),
        MoveEval(
            ply=3,
            san="Nf3",
            eval_cp=-270,
            eval_mate=None,
            best_move="d2d4",
            cp_loss=300,
            judgment="blunder",
        ),
    ]
    analysis = make_analysis(game_id="g-crit").model_copy(update={"evals": evals})
    game = make_game(id="g-crit", san_moves=["e4", "e5", "Nf3"], color="white")
    report = build_report(
        "testuser",
        [
            AnalyzedGame.model_validate(
                {**game.model_dump(), "opening": None, "analysis": analysis}
            )
        ],
    )

    assert len(report.critical_positions) == 1
    critical = report.critical_positions[0]
    assert critical.fen.startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w")
    assert critical.played == "Nf3"
    assert critical.best == "d4"  # UCI d2d4 rendered as SAN
    assert critical.cp_loss == 300
    assert critical.game_id == "g-crit"


def test_render_prompt_is_deterministic_and_complete() -> None:
    # Give the analysis a player loss so a critical position exists.
    evals = [
        MoveEval(
            ply=1,
            san="e4",
            eval_cp=-270,
            eval_mate=None,
            best_move="d2d4",
            cp_loss=300,
            judgment="blunder",
        ),
    ]
    analysis = make_analysis(game_id="g-1").model_copy(update={"evals": evals})
    report = build_report("testuser", [analyzed("g-1", analysis=analysis)])
    prompt = render_prompt(report)

    assert prompt == render_prompt(report)
    assert "## Player profile: testuser" in prompt
    assert "| C60 | Ruy Lopez | 1 | 1-0-0 | 2.5 |" in prompt
    assert "## Costliest moves" in prompt
    assert "played e4 (lost 300 cp; engine preferred d4)" in prompt
    assert "Training plan" in prompt


def test_mate_scale_losses_render_as_words_not_centipawns() -> None:
    evals = [
        MoveEval(
            ply=1,
            san="f3",
            eval_cp=None,
            eval_mate=-2,
            best_move="e2e4",
            cp_loss=10_050,  # walked into a forced mate
            judgment="blunder",
        ),
    ]
    analysis = make_analysis(game_id="g-mate").model_copy(update={"evals": evals})
    report = build_report("testuser", [analyzed("g-mate", analysis=analysis)])
    prompt = render_prompt(report)

    assert "forced-mate-scale blunder" in prompt
    assert "10050" not in prompt  # no nonsense centipawn numbers


def test_empty_report_prompt_has_no_empty_sections() -> None:
    prompt = render_prompt(build_report("testuser", []))
    assert "Repertoire" not in prompt
    assert "Costliest" not in prompt


async def test_agent_sdk_provider_collects_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        captured["prompt"] = prompt
        captured["model"] = options.model
        captured["system_prompt"] = options.system_prompt

        async def stream() -> AsyncIterator[object]:
            yield AssistantMessage(
                content=[TextBlock(text="Work on your endgames.")],
                model="claude-opus-4-8",
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    advice = await provider.complete("coach me")

    assert advice == "Work on your endgames."
    assert captured["prompt"] == "coach me"
    assert captured["model"] == "claude-opus-4-8"
    # The coach persona must replace Claude Code's coding persona.
    system_prompt = captured["system_prompt"]
    assert isinstance(system_prompt, str) and "chess coach" in system_prompt


async def test_agent_sdk_provider_surfaces_error_detail_from_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            yield ResultMessage(
                subtype="success",  # the SDK really does this on auth errors
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=0,
                session_id="s",
                result="Not logged in · Please run /login",
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    with pytest.raises(CoachProviderError, match="Not logged in"):
        await provider.complete("coach me")


async def test_agent_sdk_provider_wraps_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        raise FileNotFoundError("claude binary not found")

    monkeypatch.setattr(providers_module, "query", broken_query)

    provider = create_provider(LlmConfig())
    with pytest.raises(CoachProviderError, match="installed and logged in"):
        await provider.complete("coach me")


def test_unimplemented_providers_raise_clearly() -> None:
    with pytest.raises(CoachProviderError, match="not implemented") as excinfo:
        create_provider(LlmConfig(provider="anthropic"))
    # Both shipping providers are suggested now that github-copilot joined
    # claude-agent-sdk.
    assert "'claude-agent-sdk' or 'github-copilot'" in str(excinfo.value)


def test_provider_satisfies_protocol() -> None:
    provider: CoachProvider = create_provider(LlmConfig())
    assert isinstance(provider, ClaudeAgentSdkProvider)


# --- build_move_context ---------------------------------------------------

_EXPLAIN_EVALS = [
    MoveEval(
        ply=1, san="e4", eval_cp=30, eval_mate=None,
        best_move="e2e4", cp_loss=0, judgment="best",
    ),
    MoveEval(
        ply=2, san="e5", eval_cp=30, eval_mate=None,
        best_move="e7e5", cp_loss=0, judgment="best",
    ),
    MoveEval(
        ply=3, san="Nf3", eval_cp=-270, eval_mate=None,
        best_move="d2d4", cp_loss=300, judgment="blunder",
    ),
]  # fmt: skip


def test_build_move_context_normal_ply() -> None:
    analysis = make_analysis(game_id="g-ctx").model_copy(
        update={"evals": _EXPLAIN_EVALS}
    )
    game = make_game(id="g-ctx", san_moves=["e4", "e5", "Nf3"], color="white")

    ctx = build_move_context(game, analysis, RUY, ply=3)

    assert ctx.username == "testuser"
    assert ctx.color == "white"
    assert ctx.opening_name == "Ruy Lopez"
    assert ctx.ply == 3
    assert ctx.san == "Nf3"
    assert ctx.fen_before.startswith(
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w"
    )
    assert ctx.fen_after.startswith(
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b"
    )
    assert ctx.best_move == "d4"  # UCI d2d4 rendered as SAN on fen_before
    assert ctx.cp_loss == 300
    assert ctx.judgment == "blunder"


def test_build_move_context_first_ply_uses_starting_position() -> None:
    evals = [_EXPLAIN_EVALS[0]]
    analysis = make_analysis(game_id="g-ctx").model_copy(update={"evals": evals})
    game = make_game(id="g-ctx", san_moves=["e4"], color="white")

    ctx = build_move_context(game, analysis, opening=None, ply=1)

    assert ctx.fen_before == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    assert ctx.san == "e4"
    assert ctx.opening_name is None


def test_build_move_context_out_of_range_ply_raises() -> None:
    analysis = make_analysis(game_id="g-ctx").model_copy(
        update={"evals": _EXPLAIN_EVALS}
    )
    game = make_game(id="g-ctx", san_moves=["e4", "e5", "Nf3"])

    with pytest.raises(ValueError, match="out of range"):
        build_move_context(game, analysis, opening=None, ply=4)
    with pytest.raises(ValueError, match="out of range"):
        build_move_context(game, analysis, opening=None, ply=0)


# --- render_explain_prompt -------------------------------------------------

_EXPLAIN_CTX = MoveContext(
    username="testuser",
    color="white",
    opening_name="Ruy Lopez",
    ply=3,
    san="Nf3",
    fen_before="rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1",
    fen_after="rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 1",
    best_move="d4",
    cp_loss=300,
    judgment="blunder",
)


def test_render_explain_prompt_is_deterministic_and_complete() -> None:
    lines = [
        EvalLine(
            multipv=1,
            depth=18,
            eval_cp=35,
            eval_mate=None,
            pv_san=["d4", "exd4", "Nxd4", "Nf6", "Nc3", "Bb4"],
        ),
        EvalLine(
            multipv=2, depth=18, eval_cp=10, eval_mate=None, pv_san=["Bc4", "Nf6"]
        ),
    ]

    prompt = render_explain_prompt(_EXPLAIN_CTX, lines)

    assert prompt == render_explain_prompt(_EXPLAIN_CTX, lines)
    assert "## Move explanation for testuser" in prompt
    assert "testuser was playing white in a Ruy Lopez game." in prompt
    assert f"`{_EXPLAIN_CTX.fen_before}`" in prompt
    assert f"`{_EXPLAIN_CTX.fen_after}`" in prompt
    assert "played **Nf3** (lost about 3.0 pawns; judged **blunder**)" in prompt
    assert "engine's preferred **d4**" in prompt
    assert "| 1 | 18 | +0.35 | d4 exd4 Nxd4 Nf6 Nc3 … |" in prompt  # truncated pv
    assert "| 2 | 18 | +0.10 | Bc4 Nf6 |" in prompt
    assert "analyze_position" in prompt
    assert "300" not in prompt  # raw centipawns never reach the model
    assert "club player" in prompt
    assert "never centipawns" in prompt


def test_render_explain_prompt_mate_scale_no_opening_no_lines() -> None:
    ctx = _EXPLAIN_CTX.model_copy(
        update={"opening_name": None, "color": "black", "cp_loss": 10_050}
    )

    prompt = render_explain_prompt(ctx, [])

    assert "testuser was playing black." in prompt
    assert "walked into a forced mate; judged **blunder**" in prompt
    assert "10050" not in prompt
    assert "Candidate lines" not in prompt


# --- ClaudeAgentSdkProvider.explain -----------------------------------------


async def stub_analyst(fen: str) -> list[EvalLine]:
    return [
        EvalLine(multipv=1, depth=12, eval_cp=-40, eval_mate=None,
                  pv_san=["Bxf7+", "Kxf7"]),
    ]  # fmt: skip


async def test_agent_sdk_provider_explain_streams_text_and_tool_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        captured["prompt"] = prompt
        captured["options"] = options

        async def stream() -> AsyncIterator[object]:
            yield AssistantMessage(
                content=[TextBlock(text="Let's look at this position.")],
                model="claude-opus-4-8",
            )
            yield AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="t1",
                        name="mcp__engine__analyze_position",
                        input={"fen": "fen-after"},
                    )
                ],
                model="claude-opus-4-8",
            )
            yield AssistantMessage(
                content=[TextBlock(text="Black wins the exchange next.")],
                model="claude-opus-4-8",
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=2,
                session_id="s",
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    events = [
        event async for event in provider.explain("explain this move", stub_analyst)
    ]

    assert events == [
        ExplainEvent(type="text", text="Let's look at this position."),
        ExplainEvent(type="tool", text="engine: analyzing fen-after"),
        ExplainEvent(type="text", text="Black wins the exchange next."),
    ]
    assert captured["prompt"] == "explain this move"
    options = captured["options"]
    assert isinstance(options, ClaudeAgentOptions)
    assert options.max_turns == 8
    assert options.tools == []
    assert options.allowed_tools == ["mcp__engine__analyze_position"]
    mcp_servers = options.mcp_servers
    assert isinstance(mcp_servers, dict)
    assert "engine" in mcp_servers


async def test_agent_sdk_provider_explain_surfaces_error_detail_from_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=0,
                session_id="s",
                result="Not logged in · Please run /login",
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    with pytest.raises(CoachProviderError, match="Not logged in"):
        async for _ in provider.explain("explain this move", stub_analyst):
            pass


async def test_agent_sdk_provider_explain_wraps_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        raise FileNotFoundError("claude binary not found")

    monkeypatch.setattr(providers_module, "query", broken_query)

    provider = create_provider(LlmConfig())
    with pytest.raises(CoachProviderError, match="installed and logged in"):
        async for _ in provider.explain("explain this move", stub_analyst):
            pass


async def test_agent_sdk_provider_explain_raises_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s",
                result=None,
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    with pytest.raises(CoachProviderError, match="returned no text"):
        async for _ in provider.explain("explain this move", stub_analyst):
            pass


async def test_agent_sdk_provider_explain_early_close_closes_the_sdk_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A client disconnect closes explain() mid-stream; that close must
    # reach the SDK query generator now, not wait for GC finalization.
    sdk_stream_closed = False

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            nonlocal sdk_stream_closed
            try:
                yield AssistantMessage(
                    content=[TextBlock(text="First thought.")],
                    model="claude-opus-4-8",
                )
                yield AssistantMessage(
                    content=[TextBlock(text="Never consumed.")],
                    model="claude-opus-4-8",
                )
            finally:
                sdk_stream_closed = True

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    events = provider.explain("explain this move", stub_analyst)
    first = await anext(events)
    assert first == ExplainEvent(type="text", text="First thought.")
    await events.aclose()
    assert sdk_stream_closed


# --- CopilotSdkProvider -----------------------------------------------------
#
# copilot.CopilotClient/CopilotSession are callback-based (session.on(...)
# dispatches SessionEvents synchronously), unlike the Claude Agent SDK's
# async-generator query(). These fakes replay a scripted step list against
# the same on()/send() surface: "text" dispatches an AssistantMessageData
# event, "tool_call" invokes the registered analyze_position tool handler
# directly (standing in for the CLI runtime deciding to call it), "error"
# dispatches a SessionErrorData event, and "idle" dispatches SessionIdleData.


class _FakeCopilotSession:
    def __init__(
        self,
        script: list[tuple[str, str]],
        tools: list[Tool] | None,
        captured: dict[str, object],
    ) -> None:
        self._script = script
        self._tools = {t.name: t for t in (tools or [])}
        self._handlers: list[Callable[[SessionEvent], None]] = []
        self._captured = captured

    def on(self, handler: Callable[[SessionEvent], None]) -> Callable[[], None]:
        self._handlers.append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return unsubscribe

    async def send(self, prompt: str, **_: object) -> str:
        self._captured["prompt"] = prompt
        tool_results: list[ToolResult] = []
        for kind, value in self._script:
            if kind == "text":
                self._dispatch(
                    AssistantMessageData(content=value, message_id="m"),
                    SessionEventType.ASSISTANT_MESSAGE,
                )
            elif kind == "tool_call":
                tool_obj = self._tools["analyze_position"]
                assert tool_obj.handler is not None
                # ToolHandler's declared return type is ToolResult |
                # Awaitable[ToolResult]; the provider always registers an
                # async handler, so this narrowing is safe.
                handler = cast(
                    "Callable[[ToolInvocation], Awaitable[ToolResult]]",
                    tool_obj.handler,
                )
                result = await handler(
                    ToolInvocation(
                        tool_name="analyze_position", arguments={"fen": value}
                    )
                )
                tool_results.append(result)
            elif kind == "error":
                self._dispatch(
                    SessionErrorData(error_type="model_error", message=value),
                    SessionEventType.SESSION_ERROR,
                )
            elif kind == "idle":
                self._dispatch(SessionIdleData(), SessionEventType.SESSION_IDLE)
        self._captured["tool_results"] = tool_results
        return "message-id"

    def _dispatch(self, data: object, event_type: SessionEventType) -> None:
        event = SessionEvent(
            data=data,  # type: ignore[arg-type]  -- fake narrows by construction
            id=uuid4(),
            timestamp=datetime.now(UTC),
            type=event_type,
        )
        for handler in list(self._handlers):
            handler(event)

    async def __aenter__(self) -> "_FakeCopilotSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._captured["session_disconnected"] = True


class _FakeCopilotClient:
    def __init__(
        self,
        script: list[tuple[str, str]],
        captured: dict[str, object],
        *,
        fail_on_enter: Exception | None = None,
    ) -> None:
        self._script = script
        self._captured = captured
        self._fail_on_enter = fail_on_enter

    async def __aenter__(self) -> "_FakeCopilotClient":
        if self._fail_on_enter is not None:
            raise self._fail_on_enter
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._captured["client_stopped"] = True

    async def create_session(
        self,
        *,
        model: str | None = None,
        system_message: object | None = None,
        available_tools: object | None = None,
        tools: list[Tool] | None = None,
        **_: object,
    ) -> _FakeCopilotSession:
        self._captured["model"] = model
        self._captured["system_message"] = system_message
        self._captured["available_tools"] = available_tools
        return _FakeCopilotSession(self._script, tools, self._captured)


def _fake_copilot_client(
    script: list[tuple[str, str]],
    captured: dict[str, object],
    *,
    fail_on_enter: Exception | None = None,
) -> Callable[[], _FakeCopilotClient]:
    def factory(*_: object, **__: object) -> _FakeCopilotClient:
        return _FakeCopilotClient(script, captured, fail_on_enter=fail_on_enter)

    return factory


async def test_copilot_provider_complete_collects_text_across_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [("text", "Work on your "), ("text", "endgames."), ("idle", "")]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    advice = await provider.complete("coach me")

    assert advice == "Work on your endgames."
    assert captured["prompt"] == "coach me"
    system_message = captured["system_message"]
    assert isinstance(system_message, dict)
    assert system_message["mode"] == "replace"
    assert "chess coach" in system_message["content"]
    # A one-shot completion gets no tools at all.
    available_tools = captured["available_tools"]
    assert isinstance(available_tools, ToolSet)
    assert available_tools.to_list() == []


async def test_copilot_provider_complete_surfaces_error_detail_from_session_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [("error", "Not logged in · Please run /login")]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="Not logged in"):
        await provider.complete("coach me")


async def test_copilot_provider_complete_wraps_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        providers_module,
        "CopilotClient",
        _fake_copilot_client(
            [], captured, fail_on_enter=FileNotFoundError("copilot runtime missing")
        ),
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="installed and logged in"):
        await provider.complete("coach me")


async def test_copilot_provider_complete_raises_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [("idle", "")]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="returned no text"):
        await provider.complete("coach me")


async def test_copilot_provider_explain_streams_tool_and_text_events_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [
        ("text", "Let's look at this position."),
        ("tool_call", "fen-after"),
        ("text", "Black wins the exchange next."),
        ("idle", ""),
    ]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    events = [
        event async for event in provider.explain("explain this move", stub_analyst)
    ]

    assert events == [
        ExplainEvent(type="text", text="Let's look at this position."),
        ExplainEvent(type="tool", text="engine: analyzing fen-after"),
        ExplainEvent(type="text", text="Black wins the exchange next."),
    ]
    assert captured["prompt"] == "explain this move"
    # Only the engine tool is admitted — no shell/file/web built-ins.
    available_tools = captured["available_tools"]
    assert isinstance(available_tools, ToolSet)
    assert available_tools.to_list() == ["custom:analyze_position"]


async def test_copilot_provider_explain_surfaces_error_detail_from_session_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [("error", "Not logged in · Please run /login")]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="Not logged in"):
        async for _ in provider.explain("explain this move", stub_analyst):
            pass


async def test_copilot_provider_explain_wraps_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        providers_module,
        "CopilotClient",
        _fake_copilot_client(
            [], captured, fail_on_enter=FileNotFoundError("copilot runtime missing")
        ),
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="installed and logged in"):
        async for _ in provider.explain("explain this move", stub_analyst):
            pass


async def test_copilot_provider_explain_raises_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [("idle", "")]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    with pytest.raises(CoachProviderError, match="returned no text"):
        async for _ in provider.explain("explain this move", stub_analyst):
            pass


async def test_copilot_provider_explain_caps_engine_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unlike ClaudeAgentSdkProvider's SDK-enforced max_turns == 8 hard stop
    # (see test_agent_sdk_provider_explain_streams_text_and_tool_events),
    # the Copilot SDK has no built-in turn limit, so the provider counts
    # engine-tool calls itself: every call past the budget — the one grace
    # round and any runaway call after it — gets steered to wrap up instead
    # of reaching the engine. See
    # test_copilot_provider_explain_runaway_tool_calls_end_the_stream for
    # the hard-stop behavior once the grace round is also exhausted.
    max_engine_calls = 8
    captured: dict[str, object] = {}
    over_budget = 3
    script = [("tool_call", f"fen-{n}") for n in range(max_engine_calls + over_budget)]
    script.append(("text", "Here's what I found."))
    script.append(("idle", ""))
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    events = [
        event async for event in provider.explain("explain this move", stub_analyst)
    ]

    tool_events = [e for e in events if e.type == "tool"]
    assert len(tool_events) == max_engine_calls

    # _FakeCopilotSession.send stores a list[ToolResult]; captured erases
    # that to plain object for its other (str, bool) entries.
    tool_results = cast("list[ToolResult]", captured["tool_results"])
    assert len(tool_results) == max_engine_calls + over_budget
    # Calls past the budget don't reach the engine — they get steered to
    # wrap up instead.
    for result in tool_results[max_engine_calls:]:
        assert isinstance(result, ToolResult)
        assert result.result_type == "success"
        assert "budget" in result.text_result_for_llm


async def test_copilot_provider_explain_runaway_tool_calls_end_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After the one grace round past the budget, a further engine-tool call
    # is a runaway: the provider must cut the stream off — the generator
    # ends and the session gets disconnected — rather than let a looping
    # model keep calling the engine forever.
    max_engine_calls = 8
    captured: dict[str, object] = {}
    script = [
        ("text", "Let's check a few lines."),
        *[("tool_call", f"fen-{n}") for n in range(max_engine_calls)],  # in budget
        ("tool_call", "fen-grace"),  # budget + 1: the one grace round
        ("tool_call", "fen-runaway"),  # past the grace round: hard stop
        ("text", "Should never be yielded."),
        ("idle", ""),
    ]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    events = [
        event async for event in provider.explain("explain this move", stub_analyst)
    ]

    # The stream ends right after the in-budget tool events: the grace
    # round produces no event, and the runaway call's drain sentinel cuts
    # the loop off before the trailing text/idle steps are ever reached.
    assert events[0] == ExplainEvent(type="text", text="Let's check a few lines.")
    tool_events = [e for e in events if e.type == "tool"]
    assert len(tool_events) == max_engine_calls
    assert events[-1] == tool_events[-1]

    # Teardown still ran even though nothing called aclose() explicitly —
    # the generator's own `async with` blocks disconnected on completion.
    assert captured["session_disconnected"] is True
    assert captured["client_stopped"] is True


async def test_copilot_provider_explain_early_close_disconnects_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script = [
        ("text", "First thought."),
        ("text", "Never consumed."),
        ("idle", ""),
    ]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_copilot_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    events = provider.explain("explain this move", stub_analyst)
    first = await anext(events)
    assert first == ExplainEvent(type="text", text="First thought.")
    await events.aclose()

    assert captured["session_disconnected"] is True
    assert captured["client_stopped"] is True


def test_copilot_provider_satisfies_protocol() -> None:
    provider: CoachProvider = create_provider(LlmConfig(provider="github-copilot"))
    assert isinstance(provider, CopilotSdkProvider)
