"""Engine component — see docs/04-engine.md."""

from chess_coach.engine.analysis import EngineOptions, EvaluateFn, analyze_game
from chess_coach.engine.pool import (
    AnalysisPool,
    LiveEval,
    Progress,
    ProgressCallback,
    create_pool,
)
from chess_coach.engine.uci import (
    ANALYSIS_VERSION,
    MATE_SCORE,
    EngineError,
    PositionEval,
)

__all__ = [
    "ANALYSIS_VERSION",
    "MATE_SCORE",
    "AnalysisPool",
    "EngineError",
    "EngineOptions",
    "EvaluateFn",
    "LiveEval",
    "PositionEval",
    "Progress",
    "ProgressCallback",
    "analyze_game",
    "create_pool",
]
