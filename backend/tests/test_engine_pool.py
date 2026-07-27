"""Worker-pool recycling tests (docs/04-engine.md) — stub engines.

A worker whose call raised `EngineError` must never go back into
rotation: its process may be dead, and a dead engine in the queue fails
every later checkout of that slot until the server restarts. The pool
retires it and respawns a replacement at the next checkout.
"""

import asyncio
from collections.abc import AsyncGenerator

import chess
import chess.engine
import pytest

from chess_coach.domain import Thresholds
from chess_coach.engine import AnalysisPool, EngineError, EngineOptions, PositionEval
from chess_coach.engine.uci import Engine
from tests.factories import make_game

OPTS = EngineOptions(depth=8, thresholds=Thresholds())


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


async def test_crashed_worker_is_retired_and_replaced() -> None:
    crashed = CrashingEngine()
    spawned: list[HealthyEngine] = []

    async def respawn() -> Engine:
        engine = HealthyEngine()
        spawned.append(engine)
        return engine

    pool = AnalysisPool([crashed], respawn=respawn)
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

    pool = AnalysisPool([CrashingEngine()], respawn=respawn)
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

    pool = AnalysisPool([crashed], respawn=respawn)

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
    pool = AnalysisPool([CrashingEngine()])
    game = make_game()

    with pytest.raises(EngineError):
        await pool.analyze_game(game, OPTS)
    with pytest.raises(EngineError):
        await asyncio.wait_for(pool.analyze_game(game, OPTS), timeout=1)
