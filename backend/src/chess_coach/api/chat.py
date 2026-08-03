"""Coach chat: per-thread toolkit + scope-seed builders (docs/07-api.md,
"Chat"; docs/archive/coach-chat.md is the design record).

Kept out of `routes.py` because the five chat routes and their supporting
wiring are sizeable on their own -- mirrors `runs.py`'s separation of
state/logic from the HTTP surface.
"""

import logging
import time
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
    run_scan,
    spec_needs_evals,
)
from chess_coach.domain import (
    ChatMessage,
    Comparison,
    ComparisonGroup,
    EvalLine,
    GameDetail,
    GameSearchPage,
    OpeningStats,
    PlayerProfile,
    PlayerReport,
    Record,
    Result,
    ScanMatch,
    ScanOutcome,
    ScanSpec,
    TimeClass,
)
from chess_coach.engine import EngineError
from chess_coach.storage import (
    ChatThread,
    Db,
    GameFilters,
    ReportKey,
    game_record,
    get_explanation,
    get_game,
    get_player_profile,
    get_report,
    list_analyzed_games,
    list_game_summaries,
    list_games,
    opening_stats,
    scan_candidates,
)

logger = logging.getLogger(__name__)

# Threads cap here; the send-message route 409s and directs the student to
# start a new thread (docs/archive/coach-chat.md, "Persistence and cost").
CHAT_MESSAGE_CAP = 40

# find_games costs schema tokens like every tool, and (unlike opening_stats,
# which takes no parameters and always uses the thread's own window) the
# model chooses its own window per call -- capped so an ambitious limit
# can't render the whole archive into one tool result.
_FIND_GAMES_LIMIT_CAP = 25

# scan_games' fetch chunk: games fetched and scanned (replay plus SEE for
# sacrifice steps) per iteration of the budgeted sweep below. Chunking
# keeps memory flat across an archive-scale sweep and makes the wall-time
# budget check granular -- the check only runs between chunks, so a
# smaller chunk bounds how far one call can overrun the budget.
_SCAN_CHUNK = 200

# scan_games' wall-time budget per call: bounds turn latency, which is the
# real constraint a candidate-count cap only approximated -- and badly. A
# live recall failure exposed it: 983 eligible games, the old 800-candidate
# cap cut the sweep at late April with the measured scan cost around
# 7ms/game (983 games is ~7s), so the cap gave up three seconds before an
# answer that was well within budget. The outcome's `truncated` flag plus
# `resume_until` tell the model how to continue instead of just how far a
# fixed cap got.
_SCAN_TIME_BUDGET_S = 12.0

# scan_games' match cap, independent of the candidate cap above: bounds the
# tool-result size the same way _FIND_GAMES_LIMIT_CAP bounds find_games'.
_SCAN_MATCH_CAP = 25


def profile_for_game(
    db: Db, username: str, time_class: TimeClass
) -> PlayerProfile | None:
    """The stored profile to embed when coaching one game
    (docs/06-coach.md, "Player profile", "Embedding").

    Prefers the row for the game's own time control -- a bullet game
    should be explained to a coach who knows this student's *bullet*
    tendencies, not an average over controls they play very differently
    -- and falls back to the all-controls row, which is what existed
    before profiles were scoped and remains what a student who only ever
    generated the mixed profile has. None when neither is stored, which
    renders the prompt exactly as it was before profiles existed.

    Shared by the explain miss path and the game-scope chat seed so the
    two can never disagree about which profile a game gets.
    """
    scoped = get_player_profile(db, username, time_class=time_class)
    if scoped is not None:
        return scoped.profile
    mixed = get_player_profile(db, username)
    return mixed.profile if mixed is not None else None


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
        prior_comparisons: list[Comparison] | None = None,
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
        # Seeds the compare tool's BH family with whatever the caller
        # already judged and put in the context, so a run's questions are
        # judged alongside them rather than in a family of their own
        # (docs/06-coach.md, "Reading a comparison"). Chat passes none.
        self.prior_comparisons: list[Comparison] = list(prior_comparisons or [])

    async def compare_games(
        self,
        group: ComparisonGroup,
        within: ComparisonGroup | None = None,
    ) -> tuple[Record, Record]:
        """The group's record, and the rest of `within` by subtraction.

        Subtraction rather than a second query with the group negated:
        the group is a subset of `within` by construction, so the two
        records are exactly disjoint and no SQL has to express "not this
        opening". `within` defaults to the thread's own scope.

        Both sides are volume-layer counts over every stored game -- a
        record needs no engine, and restricting it to analyzed games
        would reintroduce, one level down, the bias the volume/quality
        split exists to remove.
        """
        baseline = await run_in_threadpool(
            game_record, self._db, self._username, self._filters(within)
        )
        inner = await run_in_threadpool(
            game_record, self._db, self._username, self._filters(within, group)
        )
        # Clamped: `group` is a subset of `within` whenever the model
        # nests them as intended, but nothing forces it to -- asking for
        # White within Black subtracts two disjoint sets and would hand
        # back a Record with negative games, which no consumer of
        # `Record` is prepared for. `coach/profile.py:_without` clamps
        # for the same reason.
        rest = Record(
            games=max(0, baseline.games - inner.games),
            wins=max(0, baseline.wins - inner.wins),
            losses=max(0, baseline.losses - inner.losses),
            draws=max(0, baseline.draws - inner.draws),
        )
        return inner, rest

    def _filters(self, *groups: ComparisonGroup | None) -> GameFilters:
        """The toolkit's own scope, narrowed by each group in turn.

        Narrowed, never widened. The window bounds **intersect** rather
        than overwrite: `since`/`until` are model-supplied through the
        tool schema, so an overwrite would let a run pass `since: 0` and
        get an archive-wide comparison beside a windowed facts block --
        exactly the one-document-two-denominators defect the toolkit was
        scoped to close. The other fields are single-valued, so a later
        group's value simply wins there.
        """
        filters = GameFilters(
            time_class=self._time_class,
            since=self._since,
            until=self._until,
            limit=0,
        )
        for group in groups:
            if group is None:
                continue
            update: dict[str, object] = {
                key: value
                for key, value in (
                    ("color", group.color),
                    ("opening_name_like", group.opening),
                    ("time_class", group.time_class),
                )
                if value is not None
            }
            if group.since is not None:
                update["since"] = max(filters.since or 0, group.since)
            if group.until is not None:
                update["until"] = min(
                    filters.until if filters.until is not None else group.until,
                    group.until,
                )
            filters = filters.model_copy(update=update)
        return filters

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
        """The page plus its total (docs/06-coach.md, "Chat"): `list_games`
        and `game_record` run over the identical `GameFilters`, so the
        total can never drift from what the page actually shows -- both
        share `_game_filter_clauses` in storage. Rendering is coach's job;
        this returns the model.
        """
        filters = GameFilters(
            opponent=opponent,
            opening_name_like=opening,
            result=result,
            time_class=time_class,
            since=since,
            until=until,
            min_rating=min_rating,
            max_rating=max_rating,
            limit=min(max(limit, 0), _FIND_GAMES_LIMIT_CAP),
            offset=max(offset, 0),
        )
        games = await run_in_threadpool(list_games, self._db, self._username, filters)
        record = await run_in_threadpool(game_record, self._db, self._username, filters)
        return GameSearchPage(games=games, total=record.games, offset=filters.offset)

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
        """The event scan (docs/06-coach.md, "Chat"): a chunked
        newest-first sweep, the outcome's denominators, and `run_scan`
        itself all run inside one `run_in_threadpool` call, unlike every
        other tool on this toolkit -- the replay-plus-SEE work is
        CPU-bound, so batching it keeps the event loop free for SSE
        instead of bouncing back to it between chunks that don't need it.

        Like `find_games`, the metadata filters are the model's own as
        given, with no intersection against the toolkit's own
        since/until/time_class -- unlike `_filters`, which narrows
        `compare_games`' groups against the thread's scope. Cross-player
        safety holds by construction here, not by an explicit check:
        `scan_candidates` and `game_record` are both scoped by
        `self._username`, so there is no id path across players the way
        `get_game` has to guard against directly.

        The sweep fetches `_SCAN_CHUNK`-sized pages via `scan_candidates`,
        `offset` advancing by one chunk per iteration, and runs `run_scan`
        per chunk. Offset paging is only ever stable within this one call,
        on the shared WAL connection -- the cursor a caller replays across
        *calls* is `resume_until` (until-based, see `ScanOutcome`'s
        docstring), not an offset. The loop stops when a fetch comes back
        under `_SCAN_CHUNK` rows (every matching candidate has now been
        seen) or, after a chunk, the wall-time budget has run out; the
        first chunk always runs regardless of the budget, since the
        budget is only ever checked between chunks. Matches are capped at
        `match_cap` like before, but the sweep keeps scanning chunks for
        coverage even once the cap is full -- the denominators and the
        resume cursor have to describe the whole sweep, so matches past
        the cap are dropped from the response while still counted for
        coverage.

        Denominators (docs/06-coach.md, "Chat"): `eligible` is every
        stored game the metadata filters match, analyzed or not, read
        from `game_record` exactly as before. Sequences that read stored
        evals (`spec_needs_evals`) restrict every chunk's fetch to
        analyzed games and report the rest as `skipped_unanalyzed`;
        moves-only sequences fetch everything and instead report
        `unverified_scanned` -- the scanned candidates with no stored
        analysis, whose eval-backed annotations render as unverified.
        `truncated` flags a sweep the budget cut short of `eligible`; when
        it does, `resume_until` is the oldest scanned game's `end_time`
        so the caller can pass it back as `until` and continue exactly
        where this call stopped.
        """
        needs_evals = spec_needs_evals(spec)
        match_cap = min(max(limit, 0), _SCAN_MATCH_CAP)

        def make_filters(
            *, analyzed: bool | None, limit: int, offset: int = 0
        ) -> GameFilters:
            return GameFilters(
                opponent=opponent,
                opening_name_like=opening,
                result=result,
                time_class=time_class,
                since=since,
                until=until,
                min_rating=min_rating,
                max_rating=max_rating,
                analyzed=analyzed,
                limit=limit,
                offset=offset,
            )

        def scan() -> ScanOutcome:
            start = time.monotonic()
            fetch_analyzed = True if needs_evals else None
            matches: list[ScanMatch] = []
            scanned = 0
            unverified_scanned = 0
            oldest_scanned_end_time: int | None = None
            truncated = False
            offset = 0
            while True:
                chunk = scan_candidates(
                    self._db,
                    self._username,
                    make_filters(
                        analyzed=fetch_analyzed, limit=_SCAN_CHUNK, offset=offset
                    ),
                )
                if chunk:
                    oldest_scanned_end_time = chunk[-1].summary.end_time
                scanned += len(chunk)
                if not needs_evals:
                    unverified_scanned += sum(
                        1 for candidate in chunk if candidate.evals is None
                    )
                matches.extend(run_scan(chunk, spec))
                if len(chunk) < _SCAN_CHUNK:
                    break  # every matching candidate has now been fetched
                if time.monotonic() - start > _SCAN_TIME_BUDGET_S:
                    truncated = True
                    break
                offset += _SCAN_CHUNK

            eligible = game_record(
                self._db, self._username, make_filters(analyzed=None, limit=0)
            ).games
            skipped_unanalyzed = (
                game_record(
                    self._db, self._username, make_filters(analyzed=False, limit=0)
                ).games
                if needs_evals
                else 0
            )
            matches = matches[:match_cap]
            resume_until = oldest_scanned_end_time if truncated else None
            elapsed = time.monotonic() - start
            logger.info(
                "scan_games: scanned %d of %d eligible in %.2fs, "
                "%d matches, truncated=%s",
                scanned,
                eligible,
                elapsed,
                len(matches),
                truncated,
            )
            return ScanOutcome(
                eligible=eligible,
                scanned=scanned,
                unverified_scanned=unverified_scanned,
                skipped_unanalyzed=skipped_unanalyzed,
                truncated=truncated,
                resume_until=resume_until,
                matches=matches,
            )

        return await run_in_threadpool(scan)

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
    The thread player's stored profile, when one exists, opens the seed
    exactly as it opens the explain prompt (docs/06-coach.md, "Player
    profile", "Embedding") -- stored row only, never a fresh aggregation,
    and the row for this game's own time control by preference.
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

    profile = await run_in_threadpool(
        profile_for_game, db, thread.username, game.time_class
    )
    return render_game_chat_context(
        game,
        ply=thread.ply,
        lines=lines,
        engine_available=analyst is not None,
        profile=profile,
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
        all_games = list_game_summaries(
            db, thread.username, since=since, until=until, time_class=time_class
        )
        return build_report(
            thread.username,
            games,
            all_games=all_games,
            time_class=time_class,
            requested_since=since,
            requested_until=until,
            games_in_scope=len(all_games),
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
