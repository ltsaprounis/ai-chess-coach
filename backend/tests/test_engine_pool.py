"""Worker-pool recycling tests (docs/04-engine.md) — stub engines.

A worker whose call raised `EngineError` must never go back into
rotation: its process may be dead, and a dead engine in the queue fails
every later checkout of that slot until the server restarts. The pool
retires it and respawns a replacement at the next checkout.
"""

import asyncio
import time
from collections.abc import AsyncGenerator

import chess
import chess.engine
import pytest

from chess_coach.domain import Thresholds
from chess_coach.engine import AnalysisPool, EngineError, EngineOptions, PositionEval
from chess_coach.engine.uci import Engine
from tests.factories import make_game

OPTS = EngineOptions(depth=8, thresholds=Thresholds())
# Generous default for tests that aren't exercising eval_timeout itself
# (the timeout tests below pass their own, much smaller, value).
TEST_EVAL_TIMEOUT = 5.0


class HealthyEngine(Engine):
    """Engine double: flat eval everywhere, empty live streams."""

    def __init__(self) -> None:
        self.closed = False

    async def evaluate(self, fen: str, depth: int) -> PositionEval:
        return PositionEval(cp=0, mate=None, best_uci=None)

    async def stream_infos(
        self, board: chess.Board, depth: int, multipv: int = 1
    ) -> AsyncGenerator[chess.engine.InfoDict, None]:
        return
        yield  # unreachable; makes this an async generator

    async def close(self) -> None:
        self.closed = True


class CrashingEngine(HealthyEngine):
    """Engine double whose process dies on first analysis use."""

    async def evaluate(self, fen: str, depth: int) -> PositionEval:
        raise EngineError("engine process died")


class CrashingStreamEngine(HealthyEngine):
    """Engine double whose process dies on first live-eval use."""

    async def stream_infos(
        self, board: chess.Board, depth: int, multipv: int = 1
    ) -> AsyncGenerator[chess.engine.InfoDict, None]:
        raise EngineError("engine process died")
        yield  # unreachable; makes this an async generator


class WedgedEngine(HealthyEngine):
    """Engine double whose evaluate never returns — a mid-search hang.

    Unlike `CrashingEngine`, this never raises: the pool has to notice
    the silence itself (`eval_timeout`), not react to a thrown error.
    """

    def __init__(self) -> None:
        super().__init__()
        self.killed = False

    async def evaluate(self, fen: str, depth: int) -> PositionEval:
        await asyncio.Event().wait()  # never set: hangs forever
        raise AssertionError("unreachable")

    def kill(self) -> None:
        self.killed = True


class WedgedStreamEngine(HealthyEngine):
    """Engine double whose live stream never yields an info."""

    def __init__(self) -> None:
        super().__init__()
        self.killed = False

    async def stream_infos(
        self, board: chess.Board, depth: int, multipv: int = 1
    ) -> AsyncGenerator[chess.engine.InfoDict, None]:
        await asyncio.Event().wait()  # never set: hangs forever
        yield chess.engine.InfoDict()  # unreachable; satisfies the type

    def kill(self) -> None:
        self.killed = True


class HangingEngine(HealthyEngine):
    """Engine double whose evaluate hangs on its *first* call only, then
    behaves like `HealthyEngine`.

    Unlike `WedgedEngine`, the point isn't `eval_timeout` — it's letting
    a test cancel the *caller* mid-search, check that the in-flight
    `evaluate()` call actually gets cancelled rather than leaking, and
    then prove the worker still works on a second, normal call.
    """

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = False
        self._hung_once = False

    async def evaluate(self, fen: str, depth: int) -> PositionEval:
        if self._hung_once:
            return await super().evaluate(fen, depth)
        self._hung_once = True
        self.started.set()
        try:
            await asyncio.Event().wait()  # never set: hangs until cancelled
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class HangingStreamEngine(HealthyEngine):
    """Engine double whose live stream hangs on its *first* call only,
    then behaves like `HealthyEngine` — the streaming counterpart of
    `HangingEngine`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self._hung_once = False

    async def stream_infos(
        self, board: chess.Board, depth: int, multipv: int = 1
    ) -> AsyncGenerator[chess.engine.InfoDict, None]:
        if self._hung_once:
            return
        self._hung_once = True
        self.started.set()
        await asyncio.Event().wait()  # never set: hangs until cancelled
        yield chess.engine.InfoDict()  # unreachable; satisfies the type


async def test_crashed_worker_is_retired_and_replaced() -> None:
    crashed = CrashingEngine()
    spawned: list[HealthyEngine] = []

    async def respawn() -> Engine:
        engine = HealthyEngine()
        spawned.append(engine)
        return engine

    pool = AnalysisPool([crashed], respawn=respawn, eval_timeout=TEST_EVAL_TIMEOUT)
    game = make_game()

    with pytest.raises(EngineError):
        await pool.analyze_game(game, OPTS)

    # The dead worker was closed and dropped, not recycled: the next
    # call gets a fresh worker and succeeds instead of failing forever.
    assert crashed.closed
    analysis = await asyncio.wait_for(pool.analyze_game(game, OPTS), timeout=1)
    assert analysis.game_id == game.id
    assert len(spawned) == 1

    await pool.close()
    assert spawned[0].closed  # close() covers respawned workers too


async def test_failed_respawn_keeps_the_slot_for_the_next_attempt() -> None:
    attempts = 0

    async def respawn() -> Engine:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise EngineError("binary missing")
        return HealthyEngine()

    pool = AnalysisPool(
        [CrashingEngine()], respawn=respawn, eval_timeout=TEST_EVAL_TIMEOUT
    )
    game = make_game()

    with pytest.raises(EngineError):
        await pool.analyze_game(game, OPTS)  # the crash retires the worker
    with pytest.raises(EngineError):
        # Respawn fails: the call fails fast rather than hanging on an
        # empty queue, and the slot survives for the next attempt.
        await asyncio.wait_for(pool.analyze_game(game, OPTS), timeout=1)
    analysis = await asyncio.wait_for(pool.analyze_game(game, OPTS), timeout=1)
    assert analysis.game_id == game.id


async def test_stream_crash_retires_the_worker_too() -> None:
    crashed = CrashingStreamEngine()
    spawned: list[HealthyEngine] = []

    async def respawn() -> Engine:
        engine = HealthyEngine()
        spawned.append(engine)
        return engine

    pool = AnalysisPool([crashed], respawn=respawn, eval_timeout=TEST_EVAL_TIMEOUT)

    with pytest.raises(EngineError):
        await pool.eval_lines(chess.STARTING_FEN, depth=2)

    assert crashed.closed
    # An empty stream is the healthy stub's normal answer — the point is
    # the call completes on a fresh worker instead of hitting the corpse.
    lines = await asyncio.wait_for(
        pool.eval_lines(chess.STARTING_FEN, depth=2), timeout=1
    )
    assert lines == []
    assert len(spawned) == 1


async def test_without_a_respawner_the_worker_is_recycled_as_before() -> None:
    # Stub pools in tests construct AnalysisPool without a respawner;
    # they keep the legacy recycle behavior rather than shrinking.
    pool = AnalysisPool([CrashingEngine()], eval_timeout=TEST_EVAL_TIMEOUT)
    game = make_game()

    with pytest.raises(EngineError):
        await pool.analyze_game(game, OPTS)
    with pytest.raises(EngineError):
        await asyncio.wait_for(pool.analyze_game(game, OPTS), timeout=1)


async def test_wedged_worker_trips_the_eval_timeout_and_is_killed() -> None:
    """A worker that never answers must not block `analyze_game` forever.

    docs/future-improvements/engine-search-hangs.md: a wedged Stockfish
    process can spin at 100% CPU for 20-40 minutes with nothing raising
    an error. `eval_timeout` bounds the wait, and the worker must be
    force-killed (not just cancelled) so it stops burning a core.
    """
    wedged = WedgedEngine()
    spawned: list[HealthyEngine] = []

    async def respawn() -> Engine:
        engine = HealthyEngine()
        spawned.append(engine)
        return engine

    pool = AnalysisPool([wedged], respawn=respawn, eval_timeout=0.05)
    game = make_game()

    start = time.monotonic()
    with pytest.raises(EngineError):
        # The outer bound is only a safety net for a broken test; the
        # real assertion is `elapsed` below staying near eval_timeout.
        await asyncio.wait_for(pool.analyze_game(game, OPTS), timeout=2)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # nowhere near the old 20-40 minute hangs
    assert wedged.killed  # the process was force-terminated, not just
    # asked nicely — a wedged engine ignores `quit` too
    assert wedged.closed  # retirement's close() step still ran

    # The pool recovers on the next call instead of staying wedged.
    analysis = await asyncio.wait_for(pool.analyze_game(game, OPTS), timeout=1)
    assert analysis.game_id == game.id
    assert len(spawned) == 1


async def test_wedged_stream_trips_the_eval_timeout_and_is_killed() -> None:
    """Same as above for the streaming path: no info within the gap
    between infos (which also bounds time-to-first-info) must not block
    `stream_eval`/`eval_lines` forever.
    """
    wedged = WedgedStreamEngine()
    spawned: list[HealthyEngine] = []

    async def respawn() -> Engine:
        engine = HealthyEngine()
        spawned.append(engine)
        return engine

    pool = AnalysisPool([wedged], respawn=respawn, eval_timeout=0.05)

    start = time.monotonic()
    with pytest.raises(EngineError):
        await asyncio.wait_for(pool.eval_lines(chess.STARTING_FEN, depth=2), timeout=2)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert wedged.killed
    assert wedged.closed

    lines = await asyncio.wait_for(
        pool.eval_lines(chess.STARTING_FEN, depth=2), timeout=1
    )
    assert lines == []
    assert len(spawned) == 1


async def test_cancelling_analyze_game_does_not_leak_the_inner_task() -> None:
    """A caller cancellation (shutdown, a request cancelled) is not a
    timeout: it must not strand the in-flight `engine.evaluate(...)`
    task. `asyncio.wait` does not cancel the futures it was given when
    it is itself cancelled, so `_bounded` has to cancel and drain the
    inner task itself before re-raising, or the worker can come back to
    the idle queue with a search still attached to it.
    """
    engine = HangingEngine()
    pool = AnalysisPool([engine], eval_timeout=TEST_EVAL_TIMEOUT)
    game = make_game()

    task = asyncio.ensure_future(pool.analyze_game(game, OPTS))
    await asyncio.wait_for(engine.started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The in-flight evaluate() actually received the cancellation — if
    # it had merely been abandoned (the leak), this would never fire.
    assert engine.cancelled

    # Cancellation isn't EngineError, so the worker was never retired;
    # it must still be usable for the next call.
    analysis = await asyncio.wait_for(pool.analyze_game(game, OPTS), timeout=1)
    assert analysis.game_id == game.id


async def test_cancelling_a_stream_consumer_does_not_break_the_worker() -> None:
    """Streaming counterpart: cancelling the task consuming
    `eval_lines`/`stream_eval` mid-search must not leave the underlying
    async generator "running". Before the fix, a leaked `__anext__` task
    left `infos.aclose()` (called by `aclosing()` while unwinding) to
    raise `RuntimeError: aclose(): ... already running`, and the worker
    would go back to the idle queue with a live search still attached.
    """
    engine = HangingStreamEngine()
    pool = AnalysisPool([engine], eval_timeout=TEST_EVAL_TIMEOUT)

    task = asyncio.ensure_future(pool.eval_lines(chess.STARTING_FEN, depth=2))
    await asyncio.wait_for(engine.started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # A fresh call on the same single-worker pool proves the aborted one
    # didn't leave the worker stuck mid-search (no RuntimeError above,
    # and the queue actually has the worker back).
    lines = await asyncio.wait_for(
        pool.eval_lines(chess.STARTING_FEN, depth=2), timeout=1
    )
    assert lines == []
