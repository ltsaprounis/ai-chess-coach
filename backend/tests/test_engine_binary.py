"""Opt-in check that the submodule Stockfish builds and speaks UCI.

Excluded from the default run and from CI (no binary there); run
locally after `make engine` with: uv run pytest -m engine
"""

from collections.abc import Iterator
from pathlib import Path

import chess
import chess.engine
import pytest

from chess_coach.domain import Thresholds
from chess_coach.engine import EngineOptions, Progress, create_pool
from tests.factories import make_game

STOCKFISH_BIN = (
    Path(__file__).resolve().parents[2] / "engines" / "stockfish" / "src" / "stockfish"
)

pytestmark = pytest.mark.engine


@pytest.fixture
def engine() -> Iterator[chess.engine.SimpleEngine]:
    if not STOCKFISH_BIN.exists():
        pytest.skip(f"no Stockfish binary at {STOCKFISH_BIN}; run `make engine`")
    eng = chess.engine.SimpleEngine.popen_uci(str(STOCKFISH_BIN))
    yield eng
    eng.quit()


def test_identifies_as_stockfish(engine: chess.engine.SimpleEngine) -> None:
    assert "stockfish" in engine.id.get("name", "").lower()


def test_evaluates_the_start_position(engine: chess.engine.SimpleEngine) -> None:
    info = engine.analyse(chess.Board(), chess.engine.Limit(depth=8))
    pov_score = info.get("score")
    assert pov_score is not None
    score = pov_score.relative.score(mate_score=10_000)
    # White's start-position eval is a modest edge, not a decided game.
    assert abs(score) < 200


async def test_pool_analyzes_a_real_game() -> None:
    if not STOCKFISH_BIN.exists():
        pytest.skip(f"no Stockfish binary at {STOCKFISH_BIN}; run `make engine`")

    # Fool's mate: white's 2. g4?? loses to 2... Qh4#.
    game = make_game(
        color="white", san_moves=["f3", "e5", "g4", "Qh4#"], pgn="fools mate"
    )
    progress: list[Progress] = []
    pool = await create_pool(STOCKFISH_BIN, workers=1)
    try:
        analysis = await pool.analyze_game(
            game,
            EngineOptions(depth=8, thresholds=Thresholds()),
            on_progress=progress.append,
        )
    finally:
        await pool.close()

    assert analysis.evals[2].judgment == "blunder"  # g4??
    assert analysis.evals[3].eval_mate == -1  # board is mated
    assert analysis.overall_acpl > 2_000  # the blunder dominates
    assert analysis.judgment_counts["blunder"] == 1
    assert progress and progress[-1].ply == progress[-1].total_plies == 4


async def test_stream_eval_reports_increasing_depths_live() -> None:
    if not STOCKFISH_BIN.exists():
        pytest.skip(f"no Stockfish binary at {STOCKFISH_BIN}; run `make engine`")

    pool = await create_pool(STOCKFISH_BIN, workers=1)
    try:
        evals = [e async for e in pool.stream_eval(chess.STARTING_FEN, depth=8)]
    finally:
        await pool.close()

    depths = [e.lines[0].depth for e in evals]
    assert depths and depths == sorted(depths)  # non-decreasing
    assert depths[-1] == 8  # runs to the target depth, then stops
    last = evals[-1].lines[0]
    assert last.multipv == 1
    # Start position: a modest white-POV cp eval and a real PV.
    assert last.eval_cp is not None and abs(last.eval_cp) < 200
    assert last.eval_mate is None
    assert last.pv_san


async def test_eval_lines_reports_multiple_candidate_lines() -> None:
    if not STOCKFISH_BIN.exists():
        pytest.skip(f"no Stockfish binary at {STOCKFISH_BIN}; run `make engine`")

    pool = await create_pool(STOCKFISH_BIN, workers=1)
    try:
        lines = await pool.eval_lines(chess.STARTING_FEN, depth=8, multipv=3)
    finally:
        await pool.close()

    assert [line.multipv for line in lines] == sorted(line.multipv for line in lines)
    assert len(lines) <= 3  # capped at the number of legal moves naturally
    assert all(line.pv_san for line in lines)
