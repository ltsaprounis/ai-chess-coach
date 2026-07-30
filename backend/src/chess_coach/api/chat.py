"""Coach chat: per-thread toolkit + scope-seed builders (docs/07-api.md,
"Chat"; docs/future-improvements/coach-chat.md is the design record).

Kept out of `routes.py` because the five chat routes and their supporting
wiring are sizeable on their own -- mirrors `runs.py`'s separation of
state/logic from the HTTP surface.
"""

from typing import cast

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from chess_coach.coach import (
    PROMPT_VERSION,
    PositionAnalystFn,
    build_move_context,
    build_report,
    render_game_chat_context,
    render_report_chat_context,
)
from chess_coach.domain import (
    ChatMessage,
    EvalLine,
    GameDetail,
    GameSummary,
    OpeningStats,
    PlayerReport,
    Result,
    TimeClass,
)
from chess_coach.engine import EngineError
from chess_coach.storage import (
    ChatThread,
    Db,
    GameFilters,
    ReportKey,
    count_games,
    get_explanation,
    get_game,
    get_report,
    list_analyzed_games,
    list_games,
    opening_stats,
)

# Threads cap here; the send-message route 409s and directs the student to
# start a new thread (docs/future-improvements/coach-chat.md, "Persistence
# and cost").
CHAT_MESSAGE_CAP = 40

# find_games costs schema tokens like every tool, and (unlike opening_stats,
# which takes no parameters and always uses the thread's own window) the
# model chooses its own window per call -- capped so an ambitious limit
# can't render the whole archive into one tool result.
_FIND_GAMES_LIMIT_CAP = 25


def window_or_none(value: int) -> int | None:
    """`0` is the stored sentinel for "open" (the `ReportKey`/`ChatThread`
    convention); storage's since/until kwargs want `None` for "unbounded"
    instead."""
    return None if value == 0 else value


def time_class_or_none(value: str) -> TimeClass | None:
    """`''` is the stored sentinel for "all controls"."""
    return None if value == "" else cast(TimeClass, value)


class ApiChatToolkit:
    """`ChatToolkit` over storage + the engine pool, built fresh per chat
    message and pre-scoped to one thread's player: the model passes
    filters, never a username, and `get_game` refuses another player's
    game even when asked for its id directly (docs/07-api.md, "Chat").

    `find_games` takes the model's own filters as-is -- the model chooses
    its own window per call, the same way a student asks about one
    opponent or one month -- while `opening_stats` has no parameters of
    its own and always uses the thread's window.
    """

    def __init__(
        self,
        db: Db,
        username: str,
        *,
        since: int | None,
        until: int | None,
        time_class: TimeClass | None,
        analyst: PositionAnalystFn | None,
    ) -> None:
        self._db = db
        self._username = username
        self._since = since
        self._until = until
        # Explicit annotation: without it, pyright's inference of this
        # attribute's type from a keyword-only constructor parameter
        # widens the Literal alias to plain `str`.
        self._time_class: TimeClass | None = time_class
        self.analyst = analyst

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
        filters = GameFilters(
            opponent=opponent,
            opening_name_like=opening,
            result=result,
            time_class=time_class,
            since=since,
            until=until,
            limit=min(max(limit, 0), _FIND_GAMES_LIMIT_CAP),
        )
        return await run_in_threadpool(list_games, self._db, self._username, filters)

    async def get_game(self, game_id: str) -> GameDetail | None:
        game = await run_in_threadpool(get_game, self._db, game_id)
        if game is None or game.username != self._username:
            # Cross-player guard: a tool call can only ever read the
            # thread's own player, even for a game id belonging to someone
            # else (docs/07-api.md, "Chat").
            return None
        return game

    async def opening_stats(self) -> list[OpeningStats]:
        return await run_in_threadpool(
            opening_stats,
            self._db,
            self._username,
            since=self._since,
            until=self._until,
            time_class=self._time_class,
        )


async def game_scope_context(
    db: Db,
    thread: ChatThread,
    analyst: PositionAnalystFn | None,
) -> str:
    """The game-scope chat seed: identity plus, when the thread has a ply
    anchor, the same seeded eval lines `explain` uses (docs/07-api.md,
    "Chat"). A missing pool degrades to `lines=None`, mirroring `/coach`;
    an `EngineError` from a live pool maps to 502, mirroring `/explain`.
    """
    assert thread.game_id is not None  # scope=game is validated at creation
    game = await run_in_threadpool(get_game, db, thread.game_id)
    if game is None:
        # Games are never deleted once stored; this would only fire if
        # storage's own contract were violated.
        raise HTTPException(status_code=404, detail=f"unknown game: {thread.game_id}")

    lines: list[EvalLine] | None = None
    if thread.ply is not None and analyst is not None:
        assert game.analysis is not None  # ply anchors require analysis at creation
        ctx = build_move_context(game, game.analysis, game.opening, thread.ply)
        try:
            lines = await analyst(ctx.fen_before)
        except EngineError as exc:
            raise HTTPException(
                status_code=502, detail=f"engine failure: {exc}"
            ) from exc

    return render_game_chat_context(
        game, ply=thread.ply, lines=lines, engine_available=analyst is not None
    )


async def report_scope_context(
    db: Db, thread: ChatThread, engine_available: bool
) -> str:
    """The report-scope chat seed: the report built over the thread's own
    window/time-class, rendered without the coaching-brief instruction
    block (docs/07-api.md, "Chat")."""
    since = window_or_none(thread.since)
    until = window_or_none(thread.until)
    time_class = time_class_or_none(thread.time_class)

    def _load_report() -> PlayerReport:
        # Threadpool: build_report replays every game with python-chess,
        # not cheap at hundreds of games, mirroring /coach's own build.
        games = list_analyzed_games(
            db, thread.username, since=since, until=until, time_class=time_class
        )
        games_in_scope = count_games(
            db, thread.username, since=since, until=until, time_class=time_class
        )
        return build_report(
            thread.username,
            games,
            time_class=time_class,
            requested_since=since,
            requested_until=until,
            games_in_scope=games_in_scope,
        )

    report = await run_in_threadpool(_load_report)
    return render_report_chat_context(report, engine_available=engine_available)


async def cached_assistant_turn(db: Db, thread: ChatThread) -> ChatMessage | None:
    """A cached explanation (game scope, ply anchor) or cached advice
    (report scope) for the thread's agent, prepended to history as the
    first assistant turn (docs/07-api.md, "Chat"). `created_at` reuses
    the thread's own -- the cached turn predates the thread by an
    unrelated amount, so there is no better timestamp for it.
    """
    if thread.scope == "game":
        if thread.ply is None:
            return None
        assert thread.game_id is not None
        cached = await run_in_threadpool(
            get_explanation, db, thread.game_id, thread.ply, thread.agent_id
        )
    else:
        key = ReportKey(
            username=thread.username,
            agent_id=thread.agent_id,
            prompt_version=PROMPT_VERSION,
            since=thread.since,
            until=thread.until,
            time_class=thread.time_class,
        )
        cached_report = await run_in_threadpool(get_report, db, key)
        cached = cached_report.advice if cached_report is not None else None
    if cached is None:
        return None
    return ChatMessage(role="assistant", content=cached, created_at=thread.created_at)
