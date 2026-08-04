"""Coach chat tests (docs/06-coach.md, "Chat"; docs/archive/
coach-chat.md is the design record).

Mirrors test_coach.py's provider-stubbing patterns (the SDKs are stubbed;
no real LLM ever runs).
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import chess
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
    ChatEvent,
    ChatToolkit,
    CoachProvider,
    CoachProviderError,
    build_profile,
    build_report,
    create_provider,
    render_chat_prompt,
    render_game_chat_context,
    render_profile_context,
    render_report_chat_context,
)
from chess_coach.coach.providers import PositionAnalystFn
from chess_coach.domain import (
    ChatMessage,
    Comparison,
    ComparisonGroup,
    EvalLine,
    GameAnalysis,
    GameDetail,
    GameSearchPage,
    GameSummary,
    Judgment,
    LlmConfig,
    MoveEval,
    Opening,
    OpeningStats,
    Record,
    Result,
    ScanEventSpec,
    ScanHit,
    ScanMatch,
    ScanOutcome,
    ScanSpec,
    TimeClass,
)
from tests.coach_scenario import scenario_games
from tests.factories import make_analyzed, make_game
from tests.snapshots import write_or_check

RUY = Opening(eco="C60", name="Ruy Lopez", ply=5)
GAME_CONTEXT_SNAPSHOT = (
    Path(__file__).parent / "testdata" / "coach_chat_game_context.md"
)
GAME_CONTEXT_WITH_PROFILE_SNAPSHOT = (
    Path(__file__).parent / "testdata" / "coach_chat_game_context_with_profile.md"
)
REPORT_CONTEXT_SNAPSHOT = (
    Path(__file__).parent / "testdata" / "coach_chat_report_context.md"
)


# --- seed renderers ---------------------------------------------------------


def _game_detail(**overrides: object) -> GameDetail:
    analyzed = make_analyzed(
        "g-ctx",
        ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"],
        color="white",
        result="win",
        opening=RUY,
        losses=[0, 0, 40],
    )
    return GameDetail.model_validate({**analyzed.model_dump(), **overrides})


def test_render_game_chat_context_matches_snapshot() -> None:
    detail = _game_detail()
    lines = [
        EvalLine(
            multipv=1, depth=18, eval_cp=35, eval_mate=None,
            pv_san=["d4", "exd4", "Nxd4", "Nf6", "Nc3", "Bb4"],
        ),
    ]  # fmt: skip

    context = render_game_chat_context(
        detail, ply=5, lines=lines, engine_available=True
    )

    assert context == render_game_chat_context(
        detail, ply=5, lines=lines, engine_available=True
    ), "render_game_chat_context is not deterministic"
    write_or_check(GAME_CONTEXT_SNAPSHOT, context)


def test_render_game_chat_context_no_ply_no_engine() -> None:
    detail = _game_detail()
    context = render_game_chat_context(detail, engine_available=False)

    assert "## Game" in context
    assert "testuser played white against hikaru" in context
    assert "result: win, Ruy Lopez" in context
    assert "Positions (FEN)" not in context  # no ply anchor -- no MoveContext
    assert "not available in this conversation" in context
    assert "How to respond" in context
    assert "Stated facts, or a tool result" in context


def test_render_game_chat_context_out_of_range_ply_raises() -> None:
    detail = _game_detail()
    with pytest.raises(ValueError, match="out of range"):
        render_game_chat_context(detail, ply=99, engine_available=True)


def test_render_game_chat_context_unanalyzed_game_with_ply_raises() -> None:
    game = make_game(id="g-unanalyzed", san_moves=["e4", "e5"])
    detail = GameDetail.model_validate(
        {**game.model_dump(), "opening": None, "analysis": None}
    )
    with pytest.raises(ValueError, match="no analysis"):
        render_game_chat_context(detail, ply=1, engine_available=True)


def test_render_game_chat_context_profile_none_is_byte_identical_to_default() -> None:
    """docs/06-coach.md: with `profile=None` (the default), the seed
    renders exactly as it always did."""
    detail = _game_detail()

    assert render_game_chat_context(
        detail, engine_available=False, profile=None
    ) == render_game_chat_context(detail, engine_available=False)


def test_render_game_chat_context_with_profile_opens_with_profile_block() -> None:
    """docs/06-coach.md: given a `profile`, the student-profile context
    block opens the seed exactly as in render_explain_prompt; everything
    after it is the profile-less rendering, unchanged.
    """
    detail = _game_detail()
    profile = build_profile(build_report("testuser", scenario_games()))

    with_profile = render_game_chat_context(
        detail, ply=5, engine_available=True, profile=profile
    )
    without_profile = render_game_chat_context(detail, ply=5, engine_available=True)

    assert with_profile.startswith(render_profile_context(profile))
    assert with_profile == f"{render_profile_context(profile)}\n\n{without_profile}"
    write_or_check(GAME_CONTEXT_WITH_PROFILE_SNAPSHOT, with_profile)


def test_render_report_chat_context_matches_snapshot() -> None:
    report = build_report(
        "testuser",
        scenario_games(),
        requested_since=1_767_225_600,
        requested_until=1_785_110_400,
        games_in_scope=30,
    )
    context = render_report_chat_context(report, engine_available=True)

    assert context == render_report_chat_context(report, engine_available=True), (
        "render_report_chat_context is not deterministic"
    )
    write_or_check(REPORT_CONTEXT_SNAPSHOT, context)


def test_render_report_chat_context_omits_coaching_brief_instructions() -> None:
    report = build_report("testuser", scenario_games())
    context = render_report_chat_context(report, engine_available=False)

    # The report's own coaching-brief instruction block must not appear --
    # the chat instructions replace it.
    assert "Write the coaching brief now" not in context
    assert "two-week training plan" not in context
    assert "How to respond" in context
    assert "not available in this conversation" in context


def test_chat_instructions_do_not_ban_the_seed_they_ship_with() -> None:
    """docs/06-coach.md, "Chat": the tool rule covers what the seed does
    not state. Banning recall of the seed itself banned the report
    scope's whole briefing before the first message, when no tool has
    run and the model may therefore assert nothing at all."""
    report = build_report("testuser", scenario_games())
    context = render_report_chat_context(report, engine_available=True)

    assert "never from memory of the context above" not in context
    assert "The facts stated in the context above are established" in context


def test_chat_instructions_coverage_honesty_bullet_covers_scan_continuation() -> None:
    """docs/06-coach.md, "Chat" > "Instructions": the coverage-honesty
    bullet also tells the model to continue a truncated scan from its
    own resume cursor before concluding, when the question spans the
    student's whole history -- rather than answering from the partial
    sweep that missed the live recall failure this behaviour fixes."""
    report = build_report("testuser", scenario_games())
    context = render_report_chat_context(report, engine_available=True)

    assert (
        "- **Coverage honesty.** When you answer from a find_games or "
        "scan_games result, state its own totals and denominators -- how "
        "many matched, how many were scanned, how many had no analysis "
        "-- and offer to widen the search rather than presenting a "
        "partial look as the whole picture. Matches are EXAMPLES to "
        "read, never a tendency: only compare_groups establishes one. "
        "When a scan_games result is truncated and the question spans "
        "the student's whole history, continue the sweep from the "
        "result's own resume cursor -- repeat scan_games with until set "
        "to the stated resume value -- before concluding, rather than "
        "answering from the partial sweep."
    ) in context


def test_render_report_chat_context_turning_points_carry_no_citation_handle() -> None:
    """docs/archive/coach-chat.md, "Link discipline": chat has
    no append_game_links pass, so a [gN] handle here would never resolve --
    the seed must not offer one."""
    report = build_report("testuser", scenario_games())
    assert report.critical_positions  # scenario has turning points to render
    context = render_report_chat_context(report, engine_available=True)

    assert "cite [g" not in context


# --- render_chat_prompt ------------------------------------------------------


def test_render_chat_prompt_formats_transcript_oldest_first_then_message() -> None:
    history = [
        ChatMessage(role="user", content="What went wrong here?", created_at=1),
        ChatMessage(role="assistant", content="You hung the knight.", created_at=2),
    ]

    prompt = render_chat_prompt(history, "But what if I take with the queen?")

    assert prompt == (
        "Student: What went wrong here?\n\n"
        "Coach: You hung the knight.\n\n"
        "Student: But what if I take with the queen?"
    )


def test_render_chat_prompt_empty_history_is_just_the_message() -> None:
    assert render_chat_prompt([], "hello") == "Student: hello"


# --- stub ChatToolkit ---------------------------------------------------------


class _StubChatToolkit:
    def __init__(
        self,
        *,
        analyst: PositionAnalystFn | None = None,
        games: list[GameSummary] | None = None,
        games_total: int | None = None,
        game_detail: GameDetail | None = None,
        openings: list[OpeningStats] | None = None,
        prior_comparisons: list[Comparison] | None = None,
        # (group record, the rest) the compare tool should receive.
        compare_result: tuple[Record, Record] | None = None,
        scan_outcome: ScanOutcome | None = None,
    ) -> None:
        self.analyst = analyst
        self._games = games if games is not None else []
        self._games_total = games_total if games_total is not None else len(self._games)
        self._game_detail = game_detail
        self._openings = openings if openings is not None else []
        self.prior_comparisons = list(prior_comparisons or [])
        self._compare_result = compare_result
        self._scan_outcome = scan_outcome
        # One entry per compare_games call, so a test can assert what the
        # model actually asked for.
        self.compare_calls: list[tuple[ComparisonGroup, ComparisonGroup | None]] = []
        self.find_games_calls: list[dict[str, object]] = []
        self.get_game_calls: list[str] = []
        self.opening_stats_calls = 0
        self.scan_games_calls: list[dict[str, object]] = []

    async def compare_games(
        self,
        group: ComparisonGroup,
        within: ComparisonGroup | None = None,
    ) -> tuple[Record, Record]:
        self.compare_calls.append((group, within))
        if self._compare_result is not None:
            return self._compare_result
        empty = Record(games=0, wins=0, losses=0, draws=0)
        return empty, empty

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
    ) -> GameSearchPage:
        self.find_games_calls.append(
            {
                "opponent": opponent,
                "opening": opening,
                "result": result,
                "time_class": time_class,
                "since": since,
                "until": until,
                "min_rating": min_rating,
                "max_rating": max_rating,
                "limit": limit,
                "offset": offset,
            }
        )
        return GameSearchPage(games=self._games, total=self._games_total, offset=offset)

    async def get_game(self, game_id: str) -> GameDetail | None:
        self.get_game_calls.append(game_id)
        return self._game_detail

    async def opening_stats(self) -> list[OpeningStats]:
        self.opening_stats_calls += 1
        return self._openings

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
    ) -> ScanOutcome:
        self.scan_games_calls.append(
            {
                "spec": spec,
                "opponent": opponent,
                "opening": opening,
                "result": result,
                "time_class": time_class,
                "since": since,
                "until": until,
                "min_rating": min_rating,
                "max_rating": max_rating,
                "limit": limit,
            }
        )
        if self._scan_outcome is not None:
            return self._scan_outcome
        return ScanOutcome(
            eligible=0,
            scanned=0,
            unverified_scanned=0,
            skipped_unanalyzed=0,
            truncated=False,
            matches=[],
        )


async def stub_analyst(fen: str) -> list[EvalLine]:
    return [
        EvalLine(multipv=1, depth=12, eval_cp=-40, eval_mate=None,
                  pv_san=["Bxf7+", "Kxf7"]),
    ]  # fmt: skip


def _sample_game_summary() -> GameSummary:
    return GameSummary(
        id="g-1",
        color="white",
        time_class="blitz",
        result="win",
        end_time=1_780_300_000,
        opponent="hikaru",
        player_rating=1500,
        opponent_rating=1490,
        first_plies=["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"],
        opening=RUY,
        analyzed=True,
    )


def _sample_opening_stats() -> OpeningStats:
    return OpeningStats(
        eco="C60",
        name="Ruy Lopez",
        color="white",
        system="1.e4 2.Nf3 3.Bb5",
        first_moves="1.e4 e5 2.Nf3 Nc6 3.Bb5",
        faced=False,
        games=6,
        wins=4,
        losses=1,
        draws=1,
        analyzed_games=6,
        opening_acpl=12.0,
        avg_cp_loss=20.0,
        opening_moves=18,
        player_moves=90,
    )


# --- ClaudeAgentSdkProvider.chat ---------------------------------------------


async def test_agent_sdk_provider_chat_streams_text_tool_then_done(
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
                content=[TextBlock(text="Let's check your games.")],
                model="claude-opus-4-8",
            )
            yield AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="t1",
                        name="mcp__engine__find_games",
                        input={"opponent": "hikaru"},
                    )
                ],
                model="claude-opus-4-8",
            )
            yield AssistantMessage(
                content=[TextBlock(text=" You've played hikaru twice.")],
                model="claude-opus-4-8",
            )
            yield ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=2, session_id="chat-session-1",
            )  # fmt: skip

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    toolkit = _StubChatToolkit(analyst=stub_analyst, games=[_sample_game_summary()])
    history = [ChatMessage(role="user", content="hi", created_at=1)]

    events = [
        event
        async for event in provider.chat(
            system_context="SEED CONTEXT",
            history=history,
            message="Did I play hikaru?",
            toolkit=toolkit,
        )
    ]

    assert events == [
        ChatEvent(type="text", text="Let's check your games."),
        ChatEvent(type="tool", text="looking up games"),
        ChatEvent(type="text", text=" You've played hikaru twice."),
        # The `done` text is what the model wrote after its last tool call
        # (docs/06-coach.md, "Providers"). "Let's check your games." still
        # streams as its own event -- the student watches the coach work --
        # but it is narration, and the API persists this `done` text as the
        # assistant turn and replays it into later prompts.
        ChatEvent(
            type="done",
            text="You've played hikaru twice.",
            provider_state="chat-session-1",
        ),
    ]
    assert captured["prompt"] == render_chat_prompt(history, "Did I play hikaru?")
    options = captured["options"]
    assert isinstance(options, ClaudeAgentOptions)
    assert options.max_turns == 8
    assert options.resume is None
    assert options.tools == []
    assert options.allowed_tools == [
        "mcp__engine__analyze_position",
        "mcp__engine__find_games",
        "mcp__engine__get_game",
        "mcp__engine__get_opening_stats",
        "mcp__engine__compare_groups",
        "mcp__engine__scan_games",
    ]
    system_prompt = options.system_prompt
    assert isinstance(system_prompt, str)
    assert "chess coach" in system_prompt
    assert "SEED CONTEXT" in system_prompt


async def test_agent_sdk_provider_chat_without_analyst_omits_analyze_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        captured["options"] = options

        async def stream() -> AsyncIterator[object]:
            yield AssistantMessage(
                content=[TextBlock(text="ok")], model="claude-opus-4-8"
            )
            yield ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="s",
            )  # fmt: skip

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    toolkit = _StubChatToolkit(analyst=None)
    events = [
        event
        async for event in provider.chat(
            system_context="SEED", history=[], message="hi", toolkit=toolkit
        )
    ]

    assert events[-1] == ChatEvent(type="done", text="ok", provider_state="s")
    options = captured["options"]
    assert isinstance(options, ClaudeAgentOptions)
    assert options.allowed_tools == [
        "mcp__engine__find_games",
        "mcp__engine__get_game",
        "mcp__engine__get_opening_stats",
        "mcp__engine__compare_groups",
        "mcp__engine__scan_games",
    ]


async def test_agent_sdk_provider_chat_resumes_sending_only_new_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        captured["prompt"] = prompt
        captured["resume"] = options.resume

        async def stream() -> AsyncIterator[object]:
            yield AssistantMessage(
                content=[TextBlock(text="Sure, the knight was hanging.")],
                model="claude-opus-4-8",
            )
            yield ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="warm-session",
            )  # fmt: skip

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    toolkit = _StubChatToolkit()
    history = [
        ChatMessage(role="user", content="What went wrong?", created_at=1),
        ChatMessage(role="assistant", content="You hung a knight.", created_at=2),
    ]

    events = [
        event
        async for event in provider.chat(
            system_context="SEED",
            history=history,
            message="Why though?",
            toolkit=toolkit,
            provider_state="warm-session",
        )
    ]

    assert events[-1] == ChatEvent(
        type="done",
        text="Sure, the knight was hanging.",
        provider_state="warm-session",
    )
    # Resume sends only the new message -- not the replayed transcript.
    assert captured["prompt"] == "Why though?"
    assert captured["resume"] == "warm-session"


async def test_agent_sdk_provider_chat_resume_failure_falls_back_to_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/06-coach.md, "Providers": any resume failure falls back
    silently to render_chat_prompt replay, handing back whatever session
    id the fresh run reports."""
    captured: dict[str, object] = {"prompts": [], "resumes": []}

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        cast("list[object]", captured["prompts"]).append(prompt)
        cast("list[object]", captured["resumes"]).append(options.resume)

        async def stream() -> AsyncIterator[object]:
            if options.resume is not None:
                # The stored session no longer exists -- the CLI reports
                # the failure before any assistant content, exactly like
                # an unknown/expired session should.
                yield ResultMessage(
                    subtype="error_during_execution", duration_ms=1,
                    duration_api_ms=1, is_error=True, num_turns=0,
                    session_id="stale-session",
                    result="No conversation found for session ID stale-session",
                )  # fmt: skip
            else:
                yield AssistantMessage(
                    content=[TextBlock(text="Let's look at that again.")],
                    model="claude-opus-4-8",
                )
                yield ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id="fresh-session",
                )  # fmt: skip

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    toolkit = _StubChatToolkit()
    history = [ChatMessage(role="user", content="hi", created_at=1)]

    events = [
        event
        async for event in provider.chat(
            system_context="SEED",
            history=history,
            message="new question",
            toolkit=toolkit,
            provider_state="stale-session",
        )
    ]

    assert events == [
        ChatEvent(type="text", text="Let's look at that again."),
        ChatEvent(
            type="done",
            text="Let's look at that again.",
            provider_state="fresh-session",
        ),
    ]
    assert captured["resumes"] == ["stale-session", None]
    assert captured["prompts"] == [
        "new question",  # the failed resume attempt: only the new message
        render_chat_prompt(history, "new question"),  # the fallback replay
    ]


async def test_agent_sdk_provider_chat_propagates_error_once_streaming_began(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume that fails *after* already streaming content is a real
    failure, not a silent-fallback candidate -- the user has already seen
    part of the resumed turn."""

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            yield AssistantMessage(
                content=[TextBlock(text="Partial answer before failure.")],
                model="claude-opus-4-8",
            )
            yield ResultMessage(
                subtype="error_during_execution", duration_ms=1,
                duration_api_ms=1, is_error=True, num_turns=1,
                session_id="warm-session", result="stream interrupted",
            )  # fmt: skip

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    toolkit = _StubChatToolkit()

    with pytest.raises(CoachProviderError, match="stream interrupted"):
        async for _ in provider.chat(
            system_context="SEED",
            history=[],
            message="hi",
            toolkit=toolkit,
            provider_state="warm-session",
        ):
            pass


async def test_agent_sdk_provider_chat_wraps_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        raise FileNotFoundError("claude binary not found")

    monkeypatch.setattr(providers_module, "query", broken_query)

    provider = create_provider(LlmConfig())
    toolkit = _StubChatToolkit()
    with pytest.raises(CoachProviderError, match="installed and logged in"):
        async for _ in provider.chat(
            system_context="SEED", history=[], message="hi", toolkit=toolkit
        ):
            pass


async def test_agent_sdk_provider_chat_raises_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            yield ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="s", result=None,
            )  # fmt: skip

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    toolkit = _StubChatToolkit()
    with pytest.raises(CoachProviderError, match="returned no text"):
        async for _ in provider.chat(
            system_context="SEED", history=[], message="hi", toolkit=toolkit
        ):
            pass


def test_agent_sdk_provider_satisfies_chat_protocol() -> None:
    provider: CoachProvider = create_provider(LlmConfig())
    assert hasattr(provider, "chat")


# --- CopilotSdkProvider.chat --------------------------------------------------
#
# Mirrors test_coach.py's _FakeCopilotSession/_FakeCopilotClient, extended
# with resume_session support: chat's resume attempt (unlike a query()
# resume) fails or succeeds before any session.send() happens, so there is
# no partial-output edge case to script here.


# One scripted step per kind: "text" dispatches an AssistantMessageData
# event, "tool_call" invokes the named registered tool's handler directly
# (standing in for the runtime deciding to call it), "error" dispatches a
# SessionErrorData event, "idle" dispatches SessionIdleData.
_ScriptStep = (
    tuple[Literal["text"], str]
    | tuple[Literal["tool_call"], str, dict[str, object]]
    | tuple[Literal["error"], str]
    | tuple[Literal["idle"]]
)


class _FakeChatSession:
    def __init__(
        self,
        session_id: str,
        script: list[_ScriptStep],
        tools: list[Tool] | None,
        captured: dict[str, object],
    ) -> None:
        self.session_id = session_id
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
        cast("list[object]", self._captured.setdefault("prompts_sent", [])).append(
            prompt
        )
        tool_results: list[ToolResult] = []
        for entry in self._script:
            match entry:
                case ("text", content):
                    self._dispatch(
                        AssistantMessageData(content=content, message_id="m"),
                        SessionEventType.ASSISTANT_MESSAGE,
                    )
                case ("tool_call", tool_name, args):
                    tool_obj = self._tools[tool_name]
                    assert tool_obj.handler is not None
                    handler = cast(
                        "Callable[[ToolInvocation], Awaitable[ToolResult]]",
                        tool_obj.handler,
                    )
                    result = await handler(
                        ToolInvocation(tool_name=tool_name, arguments=args)
                    )
                    tool_results.append(result)
                case ("error", message_text):
                    self._dispatch(
                        SessionErrorData(
                            error_type="model_error", message=message_text
                        ),
                        SessionEventType.SESSION_ERROR,
                    )
                case ("idle",):
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

    async def __aenter__(self) -> "_FakeChatSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._captured["session_disconnected"] = True


class _FakeChatClient:
    def __init__(
        self,
        script: list[_ScriptStep],
        captured: dict[str, object],
        *,
        fail_on_enter: Exception | None = None,
        resume_ok: bool = True,
        fresh_session_id: str = "fresh-session",
    ) -> None:
        self._script = script
        self._captured = captured
        self._fail_on_enter = fail_on_enter
        self._resume_ok = resume_ok
        self._fresh_session_id = fresh_session_id

    async def __aenter__(self) -> "_FakeChatClient":
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
    ) -> _FakeChatSession:
        self._captured["create_model"] = model
        self._captured["create_system_message"] = system_message
        self._captured["create_available_tools"] = available_tools
        return _FakeChatSession(
            self._fresh_session_id, self._script, tools, self._captured
        )

    async def resume_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        system_message: object | None = None,
        available_tools: object | None = None,
        tools: list[Tool] | None = None,
        **_: object,
    ) -> _FakeChatSession:
        self._captured["resume_requested_id"] = session_id
        if not self._resume_ok:
            raise RuntimeError(f"unknown session {session_id}")
        self._captured["resume_system_message"] = system_message
        return _FakeChatSession(session_id, self._script, tools, self._captured)


def _fake_chat_client(
    script: list[_ScriptStep],
    captured: dict[str, object],
    *,
    fail_on_enter: Exception | None = None,
    resume_ok: bool = True,
    fresh_session_id: str = "fresh-session",
) -> Callable[[], _FakeChatClient]:
    def factory(*_: object, **__: object) -> _FakeChatClient:
        return _FakeChatClient(
            script,
            captured,
            fail_on_enter=fail_on_enter,
            resume_ok=resume_ok,
            fresh_session_id=fresh_session_id,
        )

    return factory


async def test_agent_sdk_provider_complete_with_a_toolkit_offers_every_chat_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agentic narrative run (docs/06-coach.md, "Narrative"): given a
    toolkit, complete() registers chat's whole read-only roster rather
    than the engine tool alone, so the run can read the repertoire and
    pull games instead of paraphrasing the aggregates it was handed.
    """
    captured: dict[str, object] = {}

    def fake_query(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        captured["options"] = options

        async def stream() -> AsyncIterator[object]:
            yield AssistantMessage(
                content=[TextBlock(text="This student plays the Pirc.")],
                model="claude-opus-4-8",
            )

        return stream()

    monkeypatch.setattr(providers_module, "query", fake_query)

    provider = create_provider(LlmConfig())
    toolkit = _StubChatToolkit(analyst=stub_analyst, games=[_sample_game_summary()])

    narrative = await provider.complete("write the profile", toolkit=toolkit)

    assert narrative == "This student plays the Pirc."
    options = captured["options"]
    assert isinstance(options, ClaudeAgentOptions)
    allowed = options.allowed_tools
    assert "mcp__engine__get_opening_stats" in allowed
    assert "mcp__engine__find_games" in allowed
    assert "mcp__engine__analyze_position" in allowed
    # Built-in Claude Code tools stay locked out on every coach path.
    assert options.tools == []


def _compare_tool(
    toolkit: _StubChatToolkit,
) -> Callable[[dict[str, object]], Awaitable[str]]:
    """The registered compare tool, invoked the way the SDK would.

    Built **once** per test, because the BH ledger lives in the tool
    closure -- one family per run is the whole point, and rebuilding the
    roster between calls would silently reset it.
    """
    # Same convention as test_coach.py's threshold imports: reaching for
    # the private builder is how a test drives the real registered
    # handler without standing up an SDK session.
    tools = providers_module._build_chat_tools(  # pyright: ignore[reportPrivateUsage]
        cast("ChatToolkit", toolkit)
    )
    compare = next(t for t in tools if t.name == "compare_groups")

    async def call(args: dict[str, object]) -> str:
        result = await compare.handler(args)
        blocks = cast("list[dict[str, str]]", result["content"])
        return blocks[0]["text"]

    return call


async def _run_compare_tool(toolkit: _StubChatToolkit, args: dict[str, object]) -> str:
    return await _compare_tool(toolkit)(args)


async def test_opening_stats_says_its_moves_are_not_a_filter() -> None:
    """docs/06-coach.md, "Repertoire": a row's moves are the group's
    commonest line, not an invariant -- transpositions reach the same
    name by other move orders.

    Stating that only in the doc was not enough. A live narrative read
    "[1.e4 d6 2.d4 Nf6 3.Nc3 g6], 32g, 33%" as "32 games where White
    played 2.d4" and reported a prep hole at 33%, where the real 2.d4
    split is 46% over 153 games. A move sequence printed beside a count
    reads as a filter unless something says otherwise.
    """
    toolkit = _StubChatToolkit(
        openings=[
            OpeningStats(
                eco="B07", name="Pirc Defense", color="black",
                system="1...d6 2...Nf6 3...g6",
                first_moves="1.e4 d6 2.d4 Nf6 3.Nc3 g6",
                faced=False, games=32, wins=9, losses=20, draws=3,
                analyzed_games=32, opening_moves=320, player_moves=1200,
                opening_acpl=27.0, avg_cp_loss=170.0,
            )
        ]
    )  # fmt: skip

    # Same convention as test_coach.py's threshold imports: the private
    # renderer is what the model actually reads.
    text = await providers_module._call_opening_stats(  # pyright: ignore[reportPrivateUsage]
        cast("ChatToolkit", toolkit)
    )

    assert "MOST COMMON line, not a filter" in text
    assert "not only the games with those moves" in text
    # The row itself is unchanged -- the figures were never wrong.
    assert "32g, 33%" in text


# --- renderer goldens: find_games header, move sheet, position block,
# --- scan_games preamble (docs/06-coach.md, "Chat") -------------------


def test_find_games_header_states_total_and_page_and_unanalyzed_marker() -> None:
    page = GameSearchPage(
        games=[
            _sample_game_summary(),
            _sample_game_summary().model_copy(
                update={"id": "g-2", "opponent": "magnus", "analyzed": False}
            ),
        ],
        total=489,
        offset=10,
    )

    text = providers_module._render_game_summaries(page)  # pyright: ignore[reportPrivateUsage]

    assert text == (
        "Matched 489 games; showing 11-12, newest first.\n"
        "- 2026-06-01, white vs hikaru (1500 vs 1490), win, blitz, "
        "Ruy Lopez -- id `g-1`\n"
        "- 2026-06-01, white vs magnus (1500 vs 1490), win, blitz, "
        "Ruy Lopez, unanalyzed -- id `g-2`"
    )


def test_find_games_no_matches() -> None:
    page = GameSearchPage(games=[], total=0, offset=0)

    text = providers_module._render_game_summaries(page)  # pyright: ignore[reportPrivateUsage]

    assert text == "No games matched."


def _widened_sheet_game() -> GameDetail:
    """A game exercising every branch of the widened annotation rule
    (docs/06-coach.md, "Chat"): a best move that is a plain quiet move
    (never annotated), a non-best move (the pre-existing rule), a
    best-move CAPTURE (annotated with the eval alone, no judgment
    word -- the fix that makes a sound sacrifice findable at all), and a
    best move carrying a mate score (also eval-only)."""
    san_moves = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Bxc6", "dxc6"]
    overrides: dict[int, dict[str, object]] = {
        2: {"judgment": "inaccuracy", "cp_loss": 60, "eval_cp": -40},
        7: {"judgment": "best", "cp_loss": 0, "eval_cp": None, "eval_mate": 4},
    }
    board = chess.Board()
    evals: list[MoveEval] = []
    for idx, san in enumerate(san_moves):
        ply = idx + 1
        move = board.parse_san(san)
        fields: dict[str, object] = {
            "ply": ply,
            "san": san,
            "eval_cp": 20,
            "eval_mate": None,
            "best_move": move.uci(),
            "cp_loss": 0,
            "judgment": "best",
        }
        fields.update(overrides.get(ply, {}))
        evals.append(MoveEval.model_validate(fields))
        board.push(move)
    judgments: list[Judgment] = [e.judgment for e in evals]
    counts: dict[Judgment, int] = {
        j: judgments.count(j)
        for j in ("best", "good", "inaccuracy", "mistake", "blunder")
    }
    analysis = GameAnalysis(
        game_id="g-sheet",
        depth=16,
        evals=evals,
        overall_acpl=0.0,
        acpl_by_phase={"opening": 0.0, "middlegame": 0.0, "endgame": 0.0},
        judgment_counts=counts,
    )
    game = make_game(id="g-sheet", san_moves=san_moves, opponent="hikaru")
    return GameDetail.model_validate(
        {**game.model_dump(), "opening": None, "analysis": analysis}
    )


def test_move_sheet_widens_to_captures_and_mate_scores_eval_only() -> None:
    detail = _widened_sheet_game()

    text = providers_module._render_move_sheet(detail)  # pyright: ignore[reportPrivateUsage]

    assert text == (
        "Moves (evals in pawns; annotated on inaccuracies and worse, "
        "captures, and mate scores): "
        "1.e4 1...e5 (inaccuracy, -0.40) 2.Nf3 2...Nc6 3.Bb5 3...a6 "
        "4.Bxc6 (White mates in 4) 4...dxc6 (+0.20)"
    )


def test_move_sheet_unanalyzed_game_lists_bare_san() -> None:
    game = make_game(id="g-bare", san_moves=["e4", "e5"])
    detail = GameDetail.model_validate(
        {**game.model_dump(), "opening": None, "analysis": None}
    )

    text = providers_module._render_move_sheet(detail)  # pyright: ignore[reportPrivateUsage]

    assert text == "Moves (unanalyzed): 1.e4 1...e5"


def test_position_block_golden() -> None:
    detail = _widened_sheet_game()

    text = providers_module._render_position_block(detail, 7)  # pyright: ignore[reportPrivateUsage]

    assert text == (
        "Position at ply 7 (Bxc6):\n"
        "FEN before: `r1bqkbnr/1ppp1ppp/p1n5/1B2p3/4P3/5N2/PPPP1PPP/"
        "RNBQK2R w KQkq - 0 4`\n"
        "FEN after: `r1bqkbnr/1ppp1ppp/p1B5/4p3/4P3/5N2/PPPP1PPP/"
        "RNBQK2R b KQkq - 0 4`\n"
        "Judgment: best, loss about 0.0 pawns\n"
        "Engine best move: Bxc6\n"
        "Eval before: +0.20, eval after: White mates in 4"
    )


def test_position_block_degrades_on_unanalyzed_game() -> None:
    game = make_game(id="g-bare", san_moves=["e4", "e5"])
    detail = GameDetail.model_validate(
        {**game.model_dump(), "opening": None, "analysis": None}
    )

    text = providers_module._render_position_block(detail, 1)  # pyright: ignore[reportPrivateUsage]

    assert text == "Position at ply 1: not available -- this game is unanalyzed."


def test_position_block_degrades_out_of_range() -> None:
    detail = _widened_sheet_game()

    text = providers_module._render_position_block(detail, 99)  # pyright: ignore[reportPrivateUsage]

    assert text == (
        "Position at ply 99: not available -- ply 99 is out of range for a 8-ply game."
    )


def test_get_game_tool_schema_offers_optional_ply() -> None:
    assert "ply" in providers_module._GET_GAME_TOOL_SCHEMA["properties"]  # pyright: ignore[reportPrivateUsage]
    assert providers_module._GET_GAME_TOOL_SCHEMA["required"] == ["game_id"]  # pyright: ignore[reportPrivateUsage]


def test_find_games_and_scan_games_schemas_offer_the_rating_band() -> None:
    """docs/06-coach.md: `min_rating`/`max_rating` filter on the
    student's own rating at game time and are declared once, in the
    shared `_GAME_FILTER_PROPERTIES` dict, so find_games and scan_games
    cannot drift apart on them (the comment above that dict says so).
    """
    filter_props = providers_module._GAME_FILTER_PROPERTIES  # pyright: ignore[reportPrivateUsage]
    assert filter_props["min_rating"]["type"] == "integer"
    assert filter_props["max_rating"]["type"] == "integer"

    find_props = providers_module._FIND_GAMES_TOOL_SCHEMA["properties"]  # pyright: ignore[reportPrivateUsage]
    scan_props = providers_module._SCAN_GAMES_TOOL_SCHEMA["properties"]  # pyright: ignore[reportPrivateUsage]
    for props in (find_props, scan_props):
        assert props["min_rating"]["type"] == "integer"
        assert props["max_rating"]["type"] == "integer"


async def test_get_game_ply_appends_position_block_after_move_sheet() -> None:
    detail = _widened_sheet_game()
    toolkit = _StubChatToolkit(game_detail=detail)

    text = await providers_module._call_get_game(  # pyright: ignore[reportPrivateUsage]
        cast("ChatToolkit", toolkit), {"game_id": "g-sheet", "ply": 7}
    )

    assert "Moves (evals in pawns" in text
    assert "Position at ply 7 (Bxc6):" in text


def _scan_outcome() -> ScanOutcome:
    # The detail carries a forced-reply provenance clause (docs/06-
    # coach.md, "Chat"): ply 39 (20.Qxg7+) answered a check, so it
    # names the checking move and the last free move before it, with
    # that move's own eval pair and judgment when covered -- the
    # two-move shape a real scan_games sacrifice hit can now render,
    # not just the not-in-check case the previous golden here pinned.
    hit = ScanHit(
        ply=39,
        san="Qxg7+",
        fen_before="8/6k1/6q1/8/8/8/6K1/8 w - - 0 1",
        detail=(
            "queen sac, net 5 (gave the queen for a pawn); realizes in 1; "
            "sound; already winning before; eval +9.20 -> #5; in check "
            "since 19...Nf3+; last free move 19.Qh5 (eval +7.80 -> "
            "+8.10, good)"
        ),
    )
    match = ScanMatch(game=_sample_game_summary(), hits=[hit])
    return ScanOutcome(
        eligible=489,
        scanned=489,
        unverified_scanned=177,
        skipped_unanalyzed=0,
        truncated=False,
        matches=[match],
    )


def test_render_scan_outcome_preamble_and_match_golden() -> None:
    text = providers_module._render_scan_outcome(_scan_outcome())  # pyright: ignore[reportPrivateUsage]

    assert text == (
        "Scanned all 489 of 489 games matching the filters (177 without "
        "analysis: soundness unverified; truncated: no).\n"
        "- 2026-06-01, white vs hikaru, win, blitz, Ruy Lopez -- id `g-1`\n"
        "  20.Qxg7+: queen sac, net 5 (gave the queen for a pawn); "
        "realizes in 1; sound; already winning before; eval +9.20 -> #5; "
        "in check since 19...Nf3+; last free move 19.Qh5 (eval +7.80 -> "
        "+8.10, good) (`8/6k1/6q1/8/8/8/6K1/8 w - - 0 1`)"
    )


def test_render_scan_outcome_no_matches_still_states_denominators() -> None:
    outcome = ScanOutcome(
        eligible=12,
        scanned=12,
        unverified_scanned=3,
        skipped_unanalyzed=0,
        truncated=False,
        matches=[],
    )

    text = providers_module._render_scan_outcome(outcome)  # pyright: ignore[reportPrivateUsage]

    assert text == (
        "Scanned all 12 of 12 games matching the filters (3 without "
        "analysis: soundness unverified; truncated: no).\n"
        "No games matched."
    )


def test_render_scan_outcome_states_skipped_unanalyzed_when_nonzero() -> None:
    outcome = ScanOutcome(
        eligible=100,
        scanned=80,
        unverified_scanned=0,
        skipped_unanalyzed=20,
        truncated=True,
        matches=[],
    )

    text = providers_module._render_scan_outcome(outcome)  # pyright: ignore[reportPrivateUsage]

    assert text.startswith(
        "Scanned the newest 80 of 100 games matching the filters (0 without "
        "analysis: soundness unverified; truncated: yes). 20 more "
        "without analysis could not be scanned for this event at all."
    )


def test_render_scan_outcome_truncated_with_resume_appends_continuation() -> None:
    """docs/06-coach.md, "Chat": a truncated sweep is always continuable
    exactly where it stopped -- the preamble names the resume date and
    the epoch to pass back as `until`."""
    outcome = ScanOutcome(
        eligible=983,
        scanned=800,
        unverified_scanned=0,
        skipped_unanalyzed=0,
        truncated=True,
        resume_until=1_704_067_200,
        matches=[],
    )

    text = providers_module._render_scan_outcome(outcome)  # pyright: ignore[reportPrivateUsage]

    assert text == (
        "Scanned the newest 800 of 983 games matching the filters (0 "
        "without analysis: soundness unverified; truncated: yes). "
        "Covered down to 2024-01-01; to continue the sweep, repeat the "
        "call with until=1704067200.\n"
        "No games matched."
    )


def test_render_scan_outcome_truncated_without_resume_until_omits_continuation() -> (
    None
):
    """Defensive case: the contract sets `resume_until` iff `truncated`,
    but a truncated outcome with no resume cursor must not crash and
    must render exactly as it did before the continuation sentence
    existed."""
    outcome = ScanOutcome(
        eligible=100,
        scanned=80,
        unverified_scanned=0,
        skipped_unanalyzed=0,
        truncated=True,
        resume_until=None,
        matches=[],
    )

    text = providers_module._render_scan_outcome(outcome)  # pyright: ignore[reportPrivateUsage]

    assert text == (
        "Scanned the newest 80 of 100 games matching the filters (0 "
        "without analysis: soundness unverified; truncated: yes).\n"
        "No games matched."
    )
    assert "Covered down to" not in text


async def test_call_scan_games_parses_match_and_filters() -> None:
    toolkit = _StubChatToolkit(scan_outcome=_scan_outcome())

    text = await providers_module._call_scan_games(  # pyright: ignore[reportPrivateUsage]
        cast("ChatToolkit", toolkit),
        {
            "match": [{"event": "sacrifice", "piece": "queen", "sound_only": True}],
            "opponent": "hikaru",
            "min_rating": 1400,
            "max_rating": 1600,
            "limit": 5,
        },
    )

    assert "Scanned all 489 of 489 games" in text
    [call] = toolkit.scan_games_calls
    assert call["opponent"] == "hikaru"
    assert call["limit"] == 5
    assert call["min_rating"] == 1400
    assert call["max_rating"] == 1600
    spec = cast("ScanSpec", call["spec"])
    assert spec.match == [
        ScanEventSpec(event="sacrifice", piece="queen", sound_only=True)
    ]


async def test_call_scan_games_omits_rating_band_by_default() -> None:
    """Both filters default to None -- a call that never mentions the
    rating band must not silently apply one."""
    toolkit = _StubChatToolkit(scan_outcome=_scan_outcome())

    await providers_module._call_scan_games(  # pyright: ignore[reportPrivateUsage]
        cast("ChatToolkit", toolkit),
        {"match": [{"event": "castled"}]},
    )

    [call] = toolkit.scan_games_calls
    assert call["min_rating"] is None
    assert call["max_rating"] is None


async def test_call_find_games_passes_the_rating_band_through() -> None:
    toolkit = _StubChatToolkit(games=[_sample_game_summary()])

    await providers_module._call_find_games(  # pyright: ignore[reportPrivateUsage]
        cast("ChatToolkit", toolkit),
        {"opponent": "hikaru", "min_rating": 1400, "max_rating": 1600},
    )

    [call] = toolkit.find_games_calls
    assert call["min_rating"] == 1400
    assert call["max_rating"] == 1600


async def test_call_find_games_omits_rating_band_by_default() -> None:
    toolkit = _StubChatToolkit(games=[_sample_game_summary()])

    await providers_module._call_find_games(  # pyright: ignore[reportPrivateUsage]
        cast("ChatToolkit", toolkit), {"opponent": "hikaru"}
    )

    [call] = toolkit.find_games_calls
    assert call["min_rating"] is None
    assert call["max_rating"] is None


async def test_call_scan_games_parses_a_chain_with_the_slice_2_fields() -> None:
    """docs/06-coach.md build plan: `eval_swing`, `comeback`,
    `delivered_mate`, `castled` and their own parameters (`direction`,
    `min_swing_pawns`, `side`) all reach `ScanEventSpec` intact, not
    just `sacrifice`'s."""
    toolkit = _StubChatToolkit(scan_outcome=_scan_outcome())

    await providers_module._call_scan_games(  # pyright: ignore[reportPrivateUsage]
        cast("ChatToolkit", toolkit),
        {
            "match": [
                {"event": "castled", "side": "long"},
                {
                    "event": "eval_swing",
                    "direction": "lost",
                    "min_swing_pawns": 2.5,
                    "within_plies": 10,
                },
            ]
        },
    )

    [call] = toolkit.scan_games_calls
    spec = cast("ScanSpec", call["spec"])
    assert spec.match == [
        ScanEventSpec(event="castled", side="long"),
        ScanEventSpec(
            event="eval_swing",
            direction="lost",
            min_swing_pawns=2.5,
            within_plies=10,
        ),
    ]


def test_scan_games_tool_schema_event_enum_covers_every_event() -> None:
    event_schema = providers_module._SCAN_GAMES_TOOL_SCHEMA["properties"]["match"][  # pyright: ignore[reportPrivateUsage]
        "items"
    ]
    assert set(event_schema["properties"]["event"]["enum"]) == {
        "sacrifice",
        "eval_swing",
        "comeback",
        "delivered_mate",
        "castled",
    }


async def test_compare_tool_never_lets_the_caller_pick_the_other_side() -> None:
    """docs/06-coach.md, "Reading a comparison": the model names one
    group and the tool subtracts, so a run cannot compare a group
    against a set that contains it -- the double counting that made
    "48% after a loss against 52% overall" understate its own gap.
    """
    toolkit = _StubChatToolkit(
        compare_result=(
            Record(games=307, wins=140, losses=155, draws=12),
            Record(games=275, wins=133, losses=130, draws=12),
        )
    )

    text = await _run_compare_tool(
        toolkit,
        {"group": {"opening": "Pirc", "color": "black"}, "within": {"color": "black"}},
    )

    group, within = toolkit.compare_calls[0]
    assert group.opening == "Pirc"
    assert group.color == "black"
    assert within is not None and within.color == "black"
    assert "307 games" in text
    assert "275 games" in text


async def test_compare_tool_drops_a_result_filter_it_is_never_given() -> None:
    """A group named by its outcome and then measured by its outcome
    proves nothing -- the same defect as a +/-100 rating window. The
    schema offers no `result`, and a model that passes one anyway must
    not have it silently applied.
    """
    toolkit = _StubChatToolkit()

    await _run_compare_tool(toolkit, {"group": {"color": "white", "result": "win"}})

    group, _ = toolkit.compare_calls[0]
    assert group.color == "white"
    assert not hasattr(group, "result")


async def test_compare_tool_states_the_verdict_and_never_the_arithmetic() -> None:
    """The live tilt split: 4.8 points over 380 games against 778, which
    is 1.6 standard errors. The verdict is stated; the sigma is not."""
    toolkit = _StubChatToolkit(
        compare_result=(
            Record(games=380, wins=173, losses=186, draws=21),
            Record(games=778, wins=391, losses=343, draws=44),
        )
    )

    text = await _run_compare_tool(toolkit, {"group": {"color": "white"}})

    assert "WITHIN NOISE" in text
    assert "not a tendency" in text.lower()
    assert "sigma" not in text.lower()
    assert "p-value" not in text.lower()


async def test_compare_tool_family_grows_with_the_asking() -> None:
    """Benjamini-Hochberg is a property of the family, so a run that
    fishes raises its own bar -- and the result says how big the family
    has become, which is the honest answer to "can I just ask fourteen
    more ways"."""
    toolkit = _StubChatToolkit(
        compare_result=(
            Record(games=380, wins=173, losses=186, draws=21),
            Record(games=778, wins=391, losses=343, draws=44),
        )
    )

    compare = _compare_tool(toolkit)
    first = await compare({"group": {"color": "white"}})
    second = await compare({"group": {"color": "black"}})

    assert "0 other comparison(s)" in first
    assert "1 other comparison(s)" in second


async def test_compare_tool_family_includes_what_the_profile_already_judged() -> None:
    """The facts block already states several splits; a question the run
    asks is weighed alongside them, not in a family of its own."""
    prior = [
        Comparison(
            label="Tilt",
            left_label="after a loss",
            left=Record(games=380, wins=173, losses=186, draws=21),
            right_label="every other game",
            right=Record(games=778, wins=391, losses=343, draws=44),
            gap=-4.8,
            resolution=6.1,
            significant=False,
        )
    ]
    toolkit = _StubChatToolkit(
        prior_comparisons=prior,
        compare_result=(
            Record(games=100, wins=50, losses=45, draws=5),
            Record(games=100, wins=48, losses=47, draws=5),
        ),
    )

    text = await _run_compare_tool(toolkit, {"group": {"color": "white"}})

    assert "1 other comparison(s)" in text


async def test_compare_tool_says_when_a_group_is_too_thin_to_compare() -> None:
    toolkit = _StubChatToolkit(
        compare_result=(
            Record(games=1, wins=1, losses=0, draws=0),
            Record(games=500, wins=250, losses=240, draws=10),
        )
    )

    text = await _run_compare_tool(toolkit, {"group": {"opening": "Latvian"}})

    assert "too few to compare" in text
    assert "not a tendency" in text.lower()


async def test_copilot_provider_complete_with_a_toolkit_offers_every_chat_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Copilot half of the agentic narrative run. Chat's tools report
    progress onto a queue a streaming consumer drains and complete() has
    no stream, so it runs its own drain task -- exercised here, along
    with the roster, since the Claude path had three tests and this one
    none.
    """
    captured: dict[str, object] = {}
    script: list[_ScriptStep] = [("text", "This student plays the Pirc."), ("idle",)]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_chat_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    toolkit = _StubChatToolkit(analyst=stub_analyst, games=[_sample_game_summary()])

    narrative = await provider.complete("write the profile", toolkit=toolkit)

    assert narrative == "This student plays the Pirc."
    available = cast("ToolSet", captured["create_available_tools"])
    assert "custom:compare_groups" in available.to_list()
    assert "custom:get_opening_stats" in available.to_list()
    assert "custom:analyze_position" in available.to_list()


async def test_copilot_provider_complete_leaves_no_task_behind_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GUIDELINES.md: no fire-and-forget tasks. The drain task was
    cancelled only on the success path, so every failed agentic run left
    one suspended on `queue.get()` forever -- and a failed run is the
    common case when the runtime is missing or not logged in.
    """
    before = len(asyncio.all_tasks())

    class _Boom:
        async def __aenter__(self) -> "_Boom":
            raise RuntimeError("runtime missing")

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(providers_module, "CopilotClient", _Boom)

    provider = create_provider(LlmConfig(provider="github-copilot"))
    toolkit = _StubChatToolkit(analyst=stub_analyst)

    with pytest.raises(CoachProviderError):
        await provider.complete("write the profile", toolkit=toolkit)

    # Let anything still scheduled run, then check nothing lingers.
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) <= before


async def test_copilot_provider_chat_streams_text_tool_then_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script: list[_ScriptStep] = [
        ("text", "Let's check your games."),
        ("tool_call", "find_games", {"opponent": "hikaru"}),
        ("text", " You've played hikaru twice."),
        ("idle",),
    ]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_chat_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    toolkit = _StubChatToolkit(analyst=stub_analyst, games=[_sample_game_summary()])
    history = [ChatMessage(role="user", content="hi", created_at=1)]

    events = [
        event
        async for event in provider.chat(
            system_context="SEED CONTEXT",
            history=history,
            message="Did I play hikaru?",
            toolkit=toolkit,
        )
    ]

    assert events == [
        ChatEvent(type="text", text="Let's check your games."),
        ChatEvent(type="tool", text="looking up games"),
        ChatEvent(type="text", text=" You've played hikaru twice."),
        # Narration before a tool call still streams, but is not the reply
        # the API persists and replays (docs/06-coach.md, "Providers") --
        # the same rule ClaudeAgentSdkProvider.chat() applies.
        ChatEvent(
            type="done",
            text="You've played hikaru twice.",
            provider_state="fresh-session",
        ),
    ]
    assert toolkit.find_games_calls == [
        {
            "opponent": "hikaru",
            "opening": None,
            "result": None,
            "time_class": None,
            "since": None,
            "until": None,
            "min_rating": None,
            "max_rating": None,
            "limit": 10,
            "offset": 0,
        }
    ]
    system_message = captured["create_system_message"]
    assert isinstance(system_message, dict)
    assert system_message["mode"] == "replace"
    assert "SEED CONTEXT" in system_message["content"]
    assert captured["prompts_sent"] == [
        render_chat_prompt(history, "Did I play hikaru?")
    ]


async def test_copilot_provider_chat_without_analyst_omits_only_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script: list[_ScriptStep] = [("text", "ok"), ("idle",)]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_chat_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    toolkit = _StubChatToolkit(analyst=None)
    events = [
        event
        async for event in provider.chat(
            system_context="SEED", history=[], message="hi", toolkit=toolkit
        )
    ]

    assert events[-1].type == "done"
    available_tools = cast("ToolSet", captured["create_available_tools"])
    assert available_tools.to_list() == [
        "custom:find_games",
        "custom:get_game",
        "custom:compare_groups",
        "custom:get_opening_stats",
        "custom:scan_games",
    ]


async def test_copilot_provider_chat_renders_all_three_data_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises the real registered tool handlers end to end (unlike the
    Claude fake, this session actually invokes them) -- coach's own job,
    per docs/06-coach.md: find_games as compact rows, get_game as identity
    plus a compact move sheet, opening_stats as repertoire rows, pawns
    never centipawns."""
    captured: dict[str, object] = {}
    game_detail = _game_detail()
    script: list[_ScriptStep] = [
        ("tool_call", "find_games", {"opponent": "hikaru"}),
        ("tool_call", "get_game", {"game_id": "g-ctx"}),
        ("tool_call", "get_opening_stats", {}),
        ("text", "Here's what I found."),
        ("idle",),
    ]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_chat_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    toolkit = _StubChatToolkit(
        games=[_sample_game_summary()],
        game_detail=game_detail,
        openings=[_sample_opening_stats()],
    )
    events = [
        event
        async for event in provider.chat(
            system_context="SEED", history=[], message="hi", toolkit=toolkit
        )
    ]

    assert events[-1] == ChatEvent(
        type="done", text="Here's what I found.", provider_state="fresh-session"
    )
    tool_results = cast("list[ToolResult]", captured["tool_results"])
    assert len(tool_results) == 3
    assert "id `g-1`" in tool_results[0].text_result_for_llm  # find_games row
    assert "id `g-ctx`" in tool_results[1].text_result_for_llm  # get_game identity
    assert "Ruy Lopez" in tool_results[2].text_result_for_llm  # opening_stats row
    # Pawns, never centipawns, anywhere a tool result reaches the model
    # -- and never the acronym either (docs/06-coach.md, "Units"): a
    # tool result lands mid-conversation, with no header above it to
    # define one and a seed three lines up demanding pawns.
    for result in tool_results:
        assert "centipawn" not in result.text_result_for_llm.lower()
        assert "acpl" not in result.text_result_for_llm.lower()
    assert toolkit.find_games_calls == [
        {
            "opponent": "hikaru",
            "opening": None,
            "result": None,
            "time_class": None,
            "since": None,
            "until": None,
            "min_rating": None,
            "max_rating": None,
            "limit": 10,
            "offset": 0,
        }
    ]
    assert toolkit.get_game_calls == ["g-ctx"]
    assert toolkit.opening_stats_calls == 1


async def test_copilot_provider_chat_resumes_sending_only_new_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script: list[_ScriptStep] = [("text", "Sure, that knight was hanging."), ("idle",)]
    monkeypatch.setattr(
        providers_module,
        "CopilotClient",
        _fake_chat_client(script, captured, resume_ok=True),
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    toolkit = _StubChatToolkit()
    history = [
        ChatMessage(role="user", content="What went wrong?", created_at=1),
        ChatMessage(role="assistant", content="You hung a knight.", created_at=2),
    ]

    events = [
        event
        async for event in provider.chat(
            system_context="SEED",
            history=history,
            message="Why though?",
            toolkit=toolkit,
            provider_state="warm-session",
        )
    ]

    assert events[-1] == ChatEvent(
        type="done",
        text="Sure, that knight was hanging.",
        provider_state="warm-session",
    )
    assert captured["resume_requested_id"] == "warm-session"
    assert captured["prompts_sent"] == ["Why though?"]
    assert "create_model" not in captured  # no fresh session was created


async def test_copilot_provider_chat_resume_failure_falls_back_to_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script: list[_ScriptStep] = [("text", "Let's look at that again."), ("idle",)]
    monkeypatch.setattr(
        providers_module,
        "CopilotClient",
        _fake_chat_client(
            script, captured, resume_ok=False, fresh_session_id="fresh-session"
        ),
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    toolkit = _StubChatToolkit()
    history = [ChatMessage(role="user", content="hi", created_at=1)]

    events = [
        event
        async for event in provider.chat(
            system_context="SEED",
            history=history,
            message="new question",
            toolkit=toolkit,
            provider_state="stale-session",
        )
    ]

    assert events == [
        ChatEvent(type="text", text="Let's look at that again."),
        ChatEvent(
            type="done",
            text="Let's look at that again.",
            provider_state="fresh-session",
        ),
    ]
    assert captured["resume_requested_id"] == "stale-session"
    # The fallback creates a fresh session and replays the full transcript.
    assert captured["prompts_sent"] == [render_chat_prompt(history, "new question")]


async def test_copilot_provider_chat_surfaces_error_detail_from_session_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script: list[_ScriptStep] = [("error", "Not logged in · Please run /login")]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_chat_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    toolkit = _StubChatToolkit()
    with pytest.raises(CoachProviderError, match="Not logged in"):
        async for _ in provider.chat(
            system_context="SEED", history=[], message="hi", toolkit=toolkit
        ):
            pass


async def test_copilot_provider_chat_wraps_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        providers_module,
        "CopilotClient",
        _fake_chat_client(
            [], captured, fail_on_enter=FileNotFoundError("copilot runtime missing")
        ),
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    toolkit = _StubChatToolkit()
    with pytest.raises(CoachProviderError, match="installed and logged in"):
        async for _ in provider.chat(
            system_context="SEED", history=[], message="hi", toolkit=toolkit
        ):
            pass


async def test_copilot_provider_chat_raises_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    script: list[_ScriptStep] = [("idle",)]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_chat_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    toolkit = _StubChatToolkit()
    with pytest.raises(CoachProviderError, match="returned no text"):
        async for _ in provider.chat(
            system_context="SEED", history=[], message="hi", toolkit=toolkit
        ):
            pass


async def test_copilot_provider_chat_enforces_shared_tool_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/06-coach.md, "Chat" -- "Budgets": the counted-budget pattern
    applies across all four tools sharing one _CHAT_MAX_TURNS, not per
    tool -- calling find_games repeatedly still trips the same budget.
    Exactly one grace-round call past the budget here (a well-behaved
    model that takes the one nudge and wraps up on its own); see
    test_copilot_provider_chat_runaway_tool_calls_cut_the_run_off for a
    model that keeps calling past the grace round.
    """
    max_calls = 8  # _CHAT_MAX_TURNS
    captured: dict[str, object] = {}
    script: list[_ScriptStep] = [
        ("tool_call", "find_games", {})
        for _ in range(max_calls + 1)  # +1 grace round
    ]
    script.append(("text", "Here's what I found."))
    script.append(("idle",))
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_chat_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    toolkit = _StubChatToolkit(games=[_sample_game_summary()])
    events = [
        event
        async for event in provider.chat(
            system_context="SEED", history=[], message="hi", toolkit=toolkit
        )
    ]

    tool_events = [e for e in events if e.type == "tool"]
    assert len(tool_events) == max_calls
    assert events[-1] == ChatEvent(
        type="done", text="Here's what I found.", provider_state="fresh-session"
    )
    tool_results = cast("list[ToolResult]", captured["tool_results"])
    assert len(tool_results) == max_calls + 1
    assert tool_results[-1].result_type == "success"
    assert "budget" in tool_results[-1].text_result_for_llm
    # The toolkit itself is only ever called within budget.
    assert len(toolkit.find_games_calls) == max_calls


async def test_copilot_provider_chat_runaway_tool_calls_cut_the_run_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After the one grace round past the budget, a further tool call is a
    # runaway: the provider must cut the stream off (the generator ends and
    # the session is disconnected) rather than let a looping model keep
    # calling tools forever -- mirrors
    # test_copilot_provider_explain_runaway_tool_calls_end_the_stream.
    max_calls = 8  # _CHAT_MAX_TURNS
    captured: dict[str, object] = {}
    script: list[_ScriptStep] = [
        ("text", "Let's check a few things."),
        *[("tool_call", "find_games", {}) for _ in range(max_calls)],  # in budget
        ("tool_call", "find_games", {}),  # budget + 1: the one grace round
        ("tool_call", "find_games", {}),  # past the grace round: hard stop
        ("text", "Should never be yielded."),
        ("idle",),
    ]
    monkeypatch.setattr(
        providers_module, "CopilotClient", _fake_chat_client(script, captured)
    )

    provider = create_provider(LlmConfig(provider="github-copilot"))
    toolkit = _StubChatToolkit(games=[_sample_game_summary()])
    events: list[ChatEvent] = []
    # This run never wrote anything after a tool call, so once the
    # narration is dropped (docs/06-coach.md, "Providers") there is no
    # reply to persist. Erroring is the honest end: the alternative
    # stores "Let's check a few things." as a complete coach turn and
    # replays it into every later message in the thread.
    with pytest.raises(CoachProviderError, match="returned no text"):
        async for event in provider.chat(
            system_context="SEED", history=[], message="hi", toolkit=toolkit
        ):
            events.append(event)

    # The narration and the in-budget tool events still streamed -- the
    # grace round produces no event, and the runaway call's drain sentinel
    # cuts the loop off before the trailing text/idle steps are reached, so
    # the post-cutoff text never escapes.
    assert events[0] == ChatEvent(type="text", text="Let's check a few things.")
    tool_events = [e for e in events if e.type == "tool"]
    assert len(tool_events) == max_calls
    assert not [e for e in events if e.type == "done"]
    assert not [e for e in events if "Should never be yielded" in e.text]

    assert captured["session_disconnected"] is True
    assert captured["client_stopped"] is True


def test_copilot_provider_satisfies_chat_protocol() -> None:
    provider: CoachProvider = create_provider(LlmConfig(provider="github-copilot"))
    assert hasattr(provider, "chat")


# --- ChatToolkit protocol conformance ------------------------------------


def test_stub_toolkit_satisfies_chat_toolkit_protocol() -> None:
    toolkit: ChatToolkit = _StubChatToolkit()
    assert toolkit.analyst is None
