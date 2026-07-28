"""`Engine` unit tests against a fake UCI protocol (docs/04-engine.md).

`AnalysisPool`'s stub-based tests (test_engine_pool.py, test_engine_live.py)
replace `Engine.evaluate`/`stream_infos` wholesale, so they never exercise
the real implementation's call into python-chess. These tests do: they
assert `Engine.evaluate`/`stream_infos` pass a fresh `game` token to
`analyse`/`analysis` on every call, which is what makes python-chess send
`ucinewgame` before every search and stops a worker's transposition table
from carrying state across unrelated positions
(docs/future-improvements/engine-search-hangs.md).
"""

import asyncio
from collections.abc import Iterable
from typing import cast

import chess
import chess.engine

from chess_coach.engine.uci import Engine


class FakeAnalysis:
    """Stand-in for `chess.engine.AnalysisResult`: a sync context manager
    that also async-iterates canned infos.
    """

    def __init__(self, infos: Iterable[chess.engine.InfoDict]) -> None:
        self._infos = list(infos)
        self.stopped = False

    def __enter__(self) -> "FakeAnalysis":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stopped = True

    def __aiter__(self) -> "FakeAnalysis":
        return self

    async def __anext__(self) -> chess.engine.InfoDict:
        if not self._infos:
            raise StopAsyncIteration
        return self._infos.pop(0)


class FakeUciProtocol:
    """Stand-in for `chess.engine.UciProtocol`: records the `game` token
    each call receives instead of actually driving a process.
    """

    def __init__(self, infos: Iterable[chess.engine.InfoDict] = ()) -> None:
        self._infos = list(infos)
        self.analyse_games: list[object] = []
        self.analysis_games: list[object] = []
        self.analysis_multipvs: list[int | None] = []

    async def analyse(
        self,
        board: chess.Board,
        limit: chess.engine.Limit,
        *,
        game: object = None,
        **_kwargs: object,
    ) -> chess.engine.InfoDict:
        self.analyse_games.append(game)
        info = chess.engine.InfoDict()
        info["score"] = chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE)
        return info

    async def analysis(
        self,
        board: chess.Board,
        limit: chess.engine.Limit,
        *,
        multipv: int | None = None,
        game: object = None,
        **_kwargs: object,
    ) -> FakeAnalysis:
        self.analysis_games.append(game)
        self.analysis_multipvs.append(multipv)
        return FakeAnalysis(self._infos)


class FakeTransport:
    """Stand-in for `asyncio.SubprocessTransport`: records `kill()`."""

    def __init__(self) -> None:
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def make_engine(
    infos: Iterable[chess.engine.InfoDict] = (),
) -> tuple[Engine, FakeUciProtocol, FakeTransport]:
    protocol = FakeUciProtocol(infos)
    transport = FakeTransport()
    # The fakes duck-type asyncio.SubprocessTransport/chess.engine.UciProtocol
    # closely enough to drive Engine; cast tells pyright to trust it.
    engine = Engine(
        cast(asyncio.SubprocessTransport, transport),
        cast(chess.engine.UciProtocol, protocol),
    )
    return engine, protocol, transport


async def test_evaluate_sends_a_fresh_game_token_every_call() -> None:
    engine, protocol, _transport = make_engine()

    await engine.evaluate(chess.STARTING_FEN, depth=4)
    board = chess.Board()
    board.push_san("e4")
    await engine.evaluate(board.fen(), depth=4)

    assert len(protocol.analyse_games) == 2
    first, second = protocol.analyse_games
    assert first is not None
    assert second is not None
    # A changed `game` object each call is what makes python-chess send
    # `ucinewgame` before the search (chess/engine.py: `if ... game != game`).
    assert first is not second


async def test_stream_infos_sends_a_fresh_game_token_every_call() -> None:
    info = chess.engine.InfoDict()
    info["depth"] = 1
    info["score"] = chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE)
    engine, protocol, _transport = make_engine(infos=[info])

    board = chess.Board()
    async for _ in engine.stream_infos(board, depth=1):
        pass
    async for _ in engine.stream_infos(board, depth=1):
        pass

    assert len(protocol.analysis_games) == 2
    first, second = protocol.analysis_games
    assert first is not None
    assert second is not None
    assert first is not second


async def test_stream_infos_passes_multipv_through() -> None:
    engine, protocol, _transport = make_engine()

    async for _ in engine.stream_infos(chess.Board(), depth=1, multipv=3):
        pass

    assert protocol.analysis_multipvs == [3]


def test_kill_force_terminates_via_the_transport() -> None:
    engine, _protocol, transport = make_engine()

    engine.kill()

    assert transport.killed


async def test_kill_is_safe_to_call_when_the_process_is_already_gone() -> None:
    engine, _protocol, transport = make_engine()

    def raise_process_lookup_error() -> None:
        transport.killed = True
        raise ProcessLookupError()

    transport.kill = raise_process_lookup_error  # type: ignore[method-assign]

    engine.kill()  # must not raise

    assert transport.killed
