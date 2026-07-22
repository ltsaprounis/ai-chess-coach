"""Opt-in check that the submodule Stockfish builds and speaks UCI.

Excluded from the default run and from CI (no binary there); run
locally after `make engine` with: uv run pytest -m engine
"""

from collections.abc import Iterator
from pathlib import Path

import chess
import chess.engine
import pytest

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
