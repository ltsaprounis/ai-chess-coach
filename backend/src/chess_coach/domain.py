"""Shared domain types — the contract between components.

Documented in docs/README.md; changes here are contract changes and
must be reflected in the affected component docs in the same commit.
"""

from typing import Literal

from pydantic import BaseModel

MATE_SCORE = 10_000  # mate folded to ±cp for loss arithmetic

Color = Literal["white", "black"]
Result = Literal["win", "loss", "draw"]
TimeClass = Literal["bullet", "blitz", "rapid", "daily"]
Judgment = Literal["best", "good", "inaccuracy", "mistake", "blunder"]
Phase = Literal["opening", "middlegame", "endgame"]
LlmProvider = Literal["claude-agent-sdk", "anthropic", "azure-foundry"]


class Thresholds(BaseModel):
    """Centipawn-loss judgment cutoffs."""

    inaccuracy: int = 50
    mistake: int = 100
    blunder: int = 200


class LlmConfig(BaseModel):
    # claude-agent-sdk rides the local Claude Code login: no API key.
    provider: LlmProvider = "claude-agent-sdk"
    model: str = "claude-opus-4-8"
    max_tokens: int = 4096


class CoachAgent(LlmConfig):
    """A selectable coach: an LLM configuration with an identity.

    Configured in the `coach.agents` YAML list; the API exposes the
    roster and routes each coaching request to the chosen agent.
    """

    id: str
    label: str


class Game(BaseModel):
    id: str
    username: str
    color: Color
    pgn: str
    san_moves: list[str]
    time_control: str
    time_class: TimeClass
    result: Result
    end_time: int
    opponent: str
    player_rating: int
    opponent_rating: int
    accuracy: float | None = None  # chess.com's own, when provided


class MoveEval(BaseModel):
    ply: int
    san: str
    eval_cp: int | None
    eval_mate: int | None
    best_move: str
    cp_loss: int
    judgment: Judgment


class GameAnalysis(BaseModel):
    game_id: str
    depth: int
    evals: list[MoveEval]
    overall_acpl: float  # mean cp loss of the player's moves
    acpl_by_phase: dict[Phase, float]
    judgment_counts: dict[Judgment, int]


class Opening(BaseModel):
    eco: str
    name: str
    ply: int


class OpeningStats(BaseModel):
    eco: str
    name: str
    games: int
    wins: int
    losses: int
    draws: int
    avg_cp_loss: float | None = None  # None until games are analyzed


class CriticalPosition(BaseModel):
    fen: str
    played: str
    best: str
    cp_loss: int
    game_id: str


class PlayerReport(BaseModel):
    username: str
    games_analyzed: int
    overall_acpl: float
    acpl_by_phase: dict[Phase, float]
    judgment_counts: dict[Judgment, int]
    openings: list[OpeningStats]
    critical_positions: list[CriticalPosition]


class GameSummary(Game):
    opening: Opening | None = None
    analyzed: bool = False


class GameDetail(Game):
    opening: Opening | None = None
    analysis: GameAnalysis | None = None


class AnalyzedGame(Game):
    analysis: GameAnalysis
    opening: Opening | None = None
