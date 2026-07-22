"""Engine component — see docs/04-engine.md."""

from chess_coach.engine.analysis import EngineOptions, EvaluateFn, analyze_game
from chess_coach.engine.pool import AnalysisPool, Progress, create_pool
from chess_coach.engine.uci import MATE_SCORE, EngineError, PositionEval

__all__ = [
    "MATE_SCORE",
    "AnalysisPool",
    "EngineError",
    "EngineOptions",
    "EvaluateFn",
    "PositionEval",
    "Progress",
    "analyze_game",
    "create_pool",
]
