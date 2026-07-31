"""Coach chat tests (docs/06-coach.md, "Chat"; docs/future-improvements/
coach-chat.md is the design record).

Mirrors test_coach.py's provider-stubbing patterns (the SDKs are stubbed;
no real LLM ever runs).
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
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
    EvalLine,
    GameDetail,
    GameSummary,
    LlmConfig,
    Opening,
    OpeningStats,
    Result,
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


def test_render_report_chat_context_turning_points_carry_no_citation_handle() -> None:
    """docs/future-improvements/coach-chat.md, "Link discipline": chat has
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
        game_detail: GameDetail | None = None,
        openings: list[OpeningStats] | None = None,
    ) -> None:
        self.analyst = analyst
        self._games = games if games is not None else []
        self._game_detail = game_detail
        self._openings = openings if openings is not None else []
        self.find_games_calls: list[dict[str, object]] = []
        self.get_game_calls: list[str] = []
        self.opening_stats_calls = 0

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
    ) -> list[GameSummary]:
        self.find_games_calls.append(
            {
                "opponent": opponent,
                "opening": opening,
                "result": result,
                "time_class": time_class,
                "since": since,
                "until": until,
                "limit": limit,
            }
        )
        return self._games

    async def get_game(self, game_id: str) -> GameDetail | None:
        self.get_game_calls.append(game_id)
        return self._game_detail

    async def opening_stats(self) -> list[OpeningStats]:
        self.opening_stats_calls += 1
        return self._openings


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
        ChatEvent(
            type="done",
            text="Let's check your games. You've played hikaru twice.",
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
        ChatEvent(
            type="done",
            text="Let's check your games. You've played hikaru twice.",
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
            "limit": 10,
        }
    ]
    system_message = captured["create_system_message"]
    assert isinstance(system_message, dict)
    assert system_message["mode"] == "replace"
    assert "SEED CONTEXT" in system_message["content"]
    assert captured["prompts_sent"] == [
        render_chat_prompt(history, "Did I play hikaru?")
    ]


async def test_copilot_provider_chat_without_analyst_gets_three_tools(
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
        "custom:get_opening_stats",
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
            "limit": 10,
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
    events = [
        event
        async for event in provider.chat(
            system_context="SEED", history=[], message="hi", toolkit=toolkit
        )
    ]

    # The stream ends right after the in-budget tool events plus a final
    # done carrying whatever text streamed before the cutoff -- the grace
    # round produces no event, and the runaway call's drain sentinel cuts
    # the loop off before the trailing text/idle steps are ever reached.
    # The done carries NO provider_state: post-cutoff content lives in
    # the torn-down session but not in the persisted text, so resuming
    # it next turn would diverge from the stored transcript -- the
    # cutoff path must force a replay.
    assert events[0] == ChatEvent(type="text", text="Let's check a few things.")
    tool_events = [e for e in events if e.type == "tool"]
    assert len(tool_events) == max_calls
    assert events[-1] == ChatEvent(
        type="done",
        text="Let's check a few things.",
        provider_state=None,
    )
    assert "Should never be yielded" not in events[-1].text

    assert captured["session_disconnected"] is True
    assert captured["client_stopped"] is True


def test_copilot_provider_satisfies_chat_protocol() -> None:
    provider: CoachProvider = create_provider(LlmConfig(provider="github-copilot"))
    assert hasattr(provider, "chat")


# --- ChatToolkit protocol conformance ------------------------------------


def test_stub_toolkit_satisfies_chat_toolkit_protocol() -> None:
    toolkit: ChatToolkit = _StubChatToolkit()
    assert toolkit.analyst is None
