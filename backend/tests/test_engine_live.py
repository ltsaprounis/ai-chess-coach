"""Live MultiPV eval stream/one-shot tests (docs/04-engine.md) — stub engine."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator

import chess
import chess.engine
import pytest

from chess_coach.domain import EvalLine
from chess_coach.engine import AnalysisPool, LiveEval
from chess_coach.engine.uci import Engine


def info(
    depth: int | None = None,
    score: chess.engine.PovScore | None = None,
    pv: list[str] | None = None,
    multipv: int | None = None,
) -> chess.engine.InfoDict:
    """A raw engine info line; omitted fields stay absent, as on the wire."""
    out = chess.engine.InfoDict()
    if depth is not None:
        out["depth"] = depth
    if score is not None:
        out["score"] = score
    if pv is not None:
        out["pv"] = [chess.Move.from_uci(uci) for uci in pv]
    if multipv is not None:
        out["multipv"] = multipv
    return out


def cp(value: int, turn: chess.Color = chess.WHITE) -> chess.engine.PovScore:
    """A relative cp score as the engine reports it (from `turn`'s POV)."""
    return chess.engine.PovScore(chess.engine.Cp(value), turn)


class StubEngine(Engine):
    """Engine double streaming canned infos; tracks stream lifecycle."""

    def __init__(self, infos: list[chess.engine.InfoDict]) -> None:
        self.infos = infos
        self.streams_started = 0
        self.streams_open = 0
        self.requested_multipv: list[int] = []

    async def stream_infos(
        self, board: chess.Board, depth: int, multipv: int = 1
    ) -> AsyncGenerator[chess.engine.InfoDict, None]:
        self.streams_started += 1
        self.streams_open += 1
        self.requested_multipv.append(multipv)
        try:
            for item in self.infos:
                yield item
        finally:  # runs on exhaustion *and* on early close
            self.streams_open -= 1

    async def close(self) -> None:
        pass


def make_pool(stub: StubEngine) -> AnalysisPool:
    engines: list[Engine] = [stub]
    return AnalysisPool(engines)


async def collect(stream: AsyncIterator[LiveEval]) -> list[LiveEval]:
    return [event async for event in stream]


def test_invalid_fen_raises_synchronously_before_any_engine_work() -> None:
    stub = StubEngine([])
    pool = make_pool(stub)

    with pytest.raises(ValueError):
        pool.stream_eval("not a fen", depth=12)

    assert stub.streams_started == 0


async def test_streams_one_live_eval_per_depth_from_whites_perspective() -> None:
    # Position after 1. e4 — black to move, so the engine's relative
    # scores are from black's POV and must flip for the white view.
    board = chess.Board()
    board.push_san("e4")
    black = chess.BLACK
    stub = StubEngine(
        [
            info(),  # no depth/score (e.g. a "string" line): skipped
            info(depth=1),  # depth but no score (currmove line): skipped
            info(depth=1, score=cp(-30, black), pv=["c7c5"]),
            info(depth=2, score=cp(-40, black), pv=["e7e5", "g1f3"]),
            info(depth=3, score=chess.engine.PovScore(chess.engine.Mate(-3), black)),
        ]
    )
    pool = make_pool(stub)

    evals = await collect(pool.stream_eval(board.fen(), depth=3))

    assert [
        (e.lines[0].depth, e.lines[0].eval_cp, e.lines[0].eval_mate, e.lines[0].pv_san)
        for e in evals
    ] == [
        (1, 30, None, ["c5"]),
        (2, 40, None, ["e5", "Nf3"]),
        (3, None, 3, []),  # mate for white, cp stays None like MoveEval
    ]
    assert all(e.lines[0].multipv == 1 for e in evals)


async def test_multi_line_snapshot_assembles_from_per_line_infos() -> None:
    # multipv=2: infos for each rank interleave; the snapshot accumulates
    # the latest known line per rank, sorted by rank.
    stub = StubEngine(
        [
            info(depth=1, score=cp(10), pv=["e2e4"], multipv=1),
            info(depth=1, score=cp(5), pv=["d2d4"], multipv=2),
            info(depth=2, score=cp(20), pv=["e2e4", "e7e5"], multipv=1),
            info(depth=2, score=cp(8), pv=["d2d4", "d7d5"], multipv=2),
        ]
    )
    pool = make_pool(stub)

    evals = await collect(pool.stream_eval(chess.STARTING_FEN, depth=2, multipv=2))

    assert [
        [(line.multipv, line.depth, line.eval_cp) for line in e.lines] for e in evals
    ] == [
        [(1, 1, 10)],  # rank 2 hasn't arrived yet: snapshot holds one line
        [(1, 1, 10), (2, 1, 5)],
        [(1, 2, 20), (2, 1, 5)],
        [(1, 2, 20), (2, 2, 8)],
    ]
    assert stub.requested_multipv == [2]


async def test_duplicate_info_emits_no_new_snapshot() -> None:
    stub = StubEngine(
        [
            info(depth=1, score=cp(10), multipv=1),
            info(depth=1, score=cp(10), multipv=1),  # exact repeat: no change
            info(depth=2, score=cp(20), multipv=1),
        ]
    )
    pool = make_pool(stub)

    evals = await collect(pool.stream_eval(chess.STARTING_FEN, depth=2))

    assert [e.lines[0].depth for e in evals] == [1, 2]


async def test_changed_info_at_the_same_depth_still_emits() -> None:
    # A bound update (same depth, refined score) is a real content
    # change, so it emits even though depth didn't advance.
    stub = StubEngine(
        [
            info(depth=1, score=cp(10)),
            info(depth=1, score=cp(15)),  # bound update at same depth
        ]
    )
    pool = make_pool(stub)

    evals = await collect(pool.stream_eval(chess.STARTING_FEN, depth=1))

    assert [e.lines[0].eval_cp for e in evals] == [10, 15]


async def test_pv_san_is_capped_at_ten_moves() -> None:
    shuffle = ["g1f3", "g8f6", "f3g1", "f6g8"] * 3  # 12 legal half-moves
    stub = StubEngine([info(depth=1, score=cp(0), pv=shuffle)])
    pool = make_pool(stub)

    evals = await collect(pool.stream_eval(chess.STARTING_FEN, depth=1))

    assert len(evals) == 1
    assert evals[0].lines[0].pv_san == ["Nf3", "Nf6", "Ng1", "Ng8"] * 2 + ["Nf3", "Nf6"]


async def test_early_close_stops_the_search_and_releases_the_worker() -> None:
    stub = StubEngine([info(depth=d, score=cp(10)) for d in range(1, 6)])
    pool = make_pool(stub)

    stream = pool.stream_eval(chess.STARTING_FEN, depth=5)
    first = await anext(stream)
    assert first.lines[0].depth == 1
    await stream.aclose()

    assert stub.streams_open == 0  # inner info stream was closed too

    # The single worker is back in the pool: a fresh stream runs to
    # completion instead of waiting forever on a lost engine.
    evals = await asyncio.wait_for(
        collect(pool.stream_eval(chess.STARTING_FEN, depth=5)), timeout=1
    )
    assert [e.lines[0].depth for e in evals] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    "san_moves",
    [
        ["f3", "e5", "g4", "Qh4#"],  # fool's mate: checkmate on the board
        [  # the fastest known stalemate (Sam Loyd's 10-move line)
            "e3",
            "a5",
            "Qh5",
            "Ra6",
            "Qxa5",
            "h5",
            "h4",
            "Rah6",
            "Qxc7",
            "f6",
            "Qxd7+",
            "Kf7",
            "Qxb7",
            "Qd3",
            "Qxb8",
            "Qh7",
            "Qxc8",
            "Kg6",
            "Qe6",
        ],
    ],
)
async def test_terminal_positions_yield_nothing_without_engine_work(
    san_moves: list[str],
) -> None:
    board = chess.Board()
    for san in san_moves:
        board.push_san(san)
    assert board.outcome() is not None  # sanity: the position is over

    stub = StubEngine([info(depth=1, score=cp(0))])
    pool = make_pool(stub)

    evals = await asyncio.wait_for(
        collect(pool.stream_eval(board.fen(), depth=12)), timeout=1
    )

    assert evals == []
    assert stub.streams_started == 0


async def test_terminal_position_eval_lines_returns_empty_list() -> None:
    board = chess.Board()
    for san in ["f3", "e5", "g4", "Qh4#"]:
        board.push_san(san)

    stub = StubEngine([info(depth=1, score=cp(0))])
    pool = make_pool(stub)

    lines = await pool.eval_lines(board.fen(), depth=12)

    assert lines == []
    assert stub.streams_started == 0


async def test_eval_lines_returns_the_final_snapshot_sorted_by_rank() -> None:
    stub = StubEngine(
        [
            info(depth=1, score=cp(10), pv=["e2e4"], multipv=1),
            info(depth=1, score=cp(5), pv=["d2d4"], multipv=2),
            info(depth=2, score=cp(20), pv=["e2e4", "e7e5"], multipv=1),
            info(depth=2, score=cp(8), pv=["d2d4", "d7d5"], multipv=2),
        ]
    )
    pool = make_pool(stub)

    lines = await pool.eval_lines(chess.STARTING_FEN, depth=2, multipv=2)

    assert lines == [
        EvalLine(multipv=1, depth=2, eval_cp=20, eval_mate=None, pv_san=["e4", "e5"]),
        EvalLine(multipv=2, depth=2, eval_cp=8, eval_mate=None, pv_san=["d4", "d5"]),
    ]
    assert stub.requested_multipv == [2]


async def test_eval_lines_invalid_fen_raises_synchronously() -> None:
    stub = StubEngine([])
    pool = make_pool(stub)

    with pytest.raises(ValueError):
        await pool.eval_lines("not a fen", depth=12)

    assert stub.streams_started == 0
