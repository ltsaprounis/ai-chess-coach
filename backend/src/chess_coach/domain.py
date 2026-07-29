"""Shared domain types — the contract between components.

Documented in docs/README.md; changes here are contract changes and
must be reflected in the affected component docs in the same commit.
"""

from typing import Literal

from pydantic import BaseModel

MATE_SCORE = 10_000  # mate folded to ±cp for loss arithmetic

# Phase boundaries. The engine tags every move it judges
# (docs/04-engine.md) and the coach re-derives the same tags when it
# aggregates from raw `evals` (docs/06-coach.md) — two components
# applying one rule, so the constants live here rather than drifting
# apart in each. `PIECE_POINTS` is keyed by lowercase piece symbol so
# domain stays free of a chess-library import.
OPENING_PLIES = 20  # 10 full moves, until book-exit refinement lands
ENDGAME_MATERIAL = 13  # per side, kings excluded
PIECE_POINTS: dict[str, int] = {"q": 9, "r": 5, "b": 3, "n": 3, "p": 1}

Color = Literal["white", "black"]
Result = Literal["win", "loss", "draw"]
TimeClass = Literal["bullet", "blitz", "rapid", "daily"]
Judgment = Literal["best", "good", "inaccuracy", "mistake", "blunder"]
Phase = Literal["opening", "middlegame", "endgame"]
# Implemented providers only: config validates against this Literal,
# so a value here must have a working class behind create_provider.
# Planned (anthropic, azure-foundry) join when they ship.
LlmProvider = Literal["claude-agent-sdk", "github-copilot"]


class Thresholds(BaseModel):
    """Centipawn-loss judgment cutoffs."""

    inaccuracy: int = 50
    mistake: int = 100
    blunder: int = 200


class BrilliantThresholds(BaseModel):
    """Cutoffs for tagging a stored move brilliant (docs/06-coach.md).

    A brilliant move is a sound piece sacrifice: engine-approved, giving
    up real material by static exchange, played from a position not
    already won, and still at least holding afterwards. Loaded by config,
    applied by coach — shared here like `Thresholds` for the same reason.
    """

    sac_points: int = 2  # min net material given up (SEE, pawn units)
    best_tolerance_cp: int = 0  # max cp_loss to still count engine-best
    winning_cap_cp: int = 200  # eval before (player POV) must be <= this
    sound_floor_cp: int = 0  # eval after (player POV) must be >= this


class LlmConfig(BaseModel):
    # claude-agent-sdk rides the local Claude Code login: no API key.
    # No token ceiling: neither shipped provider's SDK takes one (both
    # ride a local CLI login). A ceiling returns as a provider-specific
    # setting if an API-backed provider lands.
    provider: LlmProvider = "claude-agent-sdk"
    model: str = "claude-opus-4-8"


class CoachAgent(LlmConfig):
    """A selectable coach: an LLM configuration with an identity.

    Configured in the `coach.agents` YAML list; the API exposes the
    roster and routes each coaching request to the chosen agent.
    """

    id: str
    label: str


class Game(BaseModel):
    # Perspective id, minted by ingestion as "{chess.com uuid}:{username}":
    # every other field here is one player's view of the game, so a game
    # between two tracked players is stored once per side and the uuid
    # alone cannot be the identity (docs/02-ingestion.md).
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
    # The raw chess.com per-player result code ("timeout", "resigned",
    # "checkmated", "abandoned", "agreed"…). `result` collapses these
    # to win/loss/draw; how a game ended is coaching signal in its own
    # right, so the code is kept alongside. None on games stored before
    # the column existed, until they are re-synced.
    termination: str | None = None


class MoveEval(BaseModel):
    ply: int
    san: str
    eval_cp: int | None
    eval_mate: int | None
    best_move: str
    cp_loss: int
    judgment: Judgment


class EvalLine(BaseModel):
    """One candidate line from a MultiPV search of a single position.

    The score is the eval of the position assuming the line is played
    (best play by both sides) — i.e. "the eval after the first move".
    """

    multipv: int  # 1-based rank; 1 is the engine's best line
    depth: int
    eval_cp: int | None  # white's perspective, like MoveEval
    eval_mate: int | None  # signed moves to mate, white's view
    pv_san: list[str]  # SAN; first entry is the candidate move


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


class Record(BaseModel):
    """A win/loss/draw tally. Score is (wins + draws/2) / games."""

    games: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0


class OpeningStats(BaseModel):
    """One opening as the player met it, from one side of the board.

    Rows are keyed by (color, eco, name): an opening is a property of
    the *game*, so without `color` the table silently merges the lines
    the player chose with the ones their opponents chose against them.

    The two move strings do the work no opening name can. `system` is
    the player's *own* first moves and nothing else — it is what a
    repertoire actually is, and grouping on it separates the openings
    they chose from the ones they merely faced. `first_moves` shows the
    same line with both sides answering, which is what makes a gambit
    visibly the opponent's. Consumers partition rows by `faced`, then
    roll the chosen partition up by (color, system) and the faced one
    by (color, name root); the rules are stated once in
    docs/06-coach.md and implemented against this type by both the
    report and the Dashboard.
    """

    eco: str
    name: str
    color: Color  # the side the player had in these games
    system: str  # the player's own moves, e.g. "1.d4 2.Nf3 3.Bg5"
    first_moves: str  # the line as played, e.g. "1.d4 e5 2.dxe5"
    # True when the name describes the opponent's choice, not the
    # player's. Per game: `Opening.ply` is the 1-based ply of the book
    # move that fixed the name — White moves are odd plies, Black even —
    # so the name is opponent-named iff that parity belongs to the
    # opponent (player is white → even ply, player is black → odd ply).
    # Per row: transpositions can reach one name at different plies, so
    # the flag is a strict majority over the group's games — true iff
    # opponent-named games * 2 > `games`; ties are chosen. Both
    # producers implement exactly this rule (docs/06-coach.md).
    faced: bool
    games: int
    wins: int
    losses: int
    draws: int
    analyzed_games: int  # how many of `games` have engine analysis
    # Two different questions, so two columns: does the player come out
    # of the opening safely, and do these games go well overall. Only
    # the first is opening advice. Both are None until games are
    # analyzed, and both are move-weighted, never a mean of per-game
    # means.
    opening_acpl: float | None = None  # opening-phase moves only
    avg_cp_loss: float | None = None  # whole game, all phases
    # The denominators behind those two columns. Consumers roll these
    # rows up into families, and a rollup can only stay move-weighted
    # if it can re-weight by moves — without these it has to fall back
    # to weighting by games, which is the mean-of-per-game-means this
    # type exists to stamp out, reintroduced one level up.
    opening_moves: int = 0  # player moves behind `opening_acpl`
    player_moves: int = 0  # player moves behind `avg_cp_loss`


class PlayerSummary(BaseModel):
    """A stored player, for the saved-players picker."""

    username: str
    games: int
    last_played: int  # epoch seconds of the most recent stored game


class PhaseStats(BaseModel):
    """Player-move aggregates for one phase, with its denominator.

    `acpl` is total centipawn loss ÷ `moves` — never a mean of per-game
    means — and is None when the player made no moves in the phase. A
    phase the games never reached must read as "no moves", not as 0.0
    centipawn loss, which is indistinguishable from flawless play.
    """

    moves: int
    acpl: float | None
    judgment_counts: dict[Judgment, int]


class TimeClassStats(BaseModel):
    """Play and rating movement within one time control."""

    time_class: TimeClass
    record: Record
    rating_start: int  # oldest game in the window
    rating_end: int  # newest game in the window
    rating_min: int
    rating_max: int


class MonthStats(BaseModel):
    """One calendar month — the trend row that says whether it helped."""

    month: str  # "2026-07"
    games: int
    rating_end: int | None  # last rating seen that month
    acpl: float | None
    blunder_rate: float | None  # blunders ÷ player moves


class OpponentStats(BaseModel):
    """How the player scores against stronger and weaker opposition."""

    avg_rating_diff: float  # mean of player_rating - opponent_rating
    vs_stronger: Record  # opponent rated clearly above
    vs_similar: Record
    vs_weaker: Record  # opponent rated clearly below


class TerminationStats(BaseModel):
    """How games ended — 'lost on time' and 'resigned' are not the same."""

    result: Result
    termination: str  # raw chess.com code; "unknown" before re-sync
    games: int


class ErrorPattern(BaseModel):
    """A recurring mistake, tagged deterministically with python-chess.

    Counts generalize where anecdotes do not: "you hung a piece to a
    check 34 times" outweighs five sample positions and costs no LLM
    tokens. Tags are assigned by static analysis of the position, never
    by a model.
    """

    pattern: str  # stable id, e.g. "hangs_piece_to_check"
    label: str  # human phrasing for the prompt and the UI
    count: int
    share_of_blunders: float  # count ÷ total blunders, 0-1
    # One instance the student can go and look at. Carries the same
    # identity a CriticalPosition does — the prompt cites it by
    # opponent, date, and move number through a [gN] link handle, and
    # game_id/ply are what the handle's /games/{id}?ply= link is
    # minted from (docs/06-coach.md, "Game links").
    example_game_id: str | None = None
    example_ply: int | None = None
    example_end_time: int | None = None
    example_move_number: int | None = None
    example_opponent: str | None = None


class CriticalPosition(BaseModel):
    """A turning point the student can actually find and act on.

    Identity (opponent, date, time class, color, opening, move number)
    so the prompt can say "in your game against marko77 on June 14,
    26...Nb6" instead of "position 1" — opponent and date because that
    is how players remember games — and the eval either side of the
    move so a blunder that lost a won game is distinguishable from one
    played in an already-lost position.
    """

    game_id: str
    end_time: int
    time_class: TimeClass
    color: Color  # the side the player had
    opponent: str
    opening_name: str | None
    ply: int  # 1-based, matches MoveEval.ply
    move_number: int  # the "26" in "26...Nb6"
    fen: str  # the position before the move
    leading_up: list[str]  # the SAN plies into this position
    played: str
    best: str  # SAN when it parses in this position, else raw UCI
    cp_loss: int
    eval_before_cp: int | None
    eval_before_mate: int | None
    eval_after_cp: int | None
    eval_after_mate: int | None


class PlayerReport(BaseModel):
    """Everything the coaching prompt and the Dashboard read.

    Aggregated over analyzed games only; `window_start`/`window_end`
    report the extent of the data actually covered, so neither the
    model nor the student has to guess what period the numbers describe.
    """

    username: str
    games_analyzed: int
    player_moves: int  # the denominator for judgment_counts
    window_start: int | None  # epoch seconds of the oldest game covered
    window_end: int | None  # epoch seconds of the newest
    time_class: TimeClass | None  # the filter applied; None = all mixed
    # The scope the caller asked for, as opposed to what analysis covers:
    # requested_* are the window bounds of the request (None = unbounded)
    # and games_in_scope counts every stored game matching the same
    # filters, analyzed or not. Together they let the prompt state
    # coverage ("450 of 1,010 games in the window are analyzed") instead
    # of presenting the analyzed span as if it were the whole story.
    # None throughout = the caller supplied no scope information.
    requested_since: int | None = None
    requested_until: int | None = None
    games_in_scope: int | None = None
    record: Record
    overall_acpl: float  # total loss ÷ player_moves
    phases: dict[Phase, PhaseStats]
    judgment_counts: dict[Judgment, int]
    time_classes: list[TimeClassStats]
    months: list[MonthStats]  # oldest first
    terminations: list[TerminationStats]
    opponents: OpponentStats | None  # None with no games
    openings: list[OpeningStats]
    error_patterns: list[ErrorPattern]
    critical_positions: list[CriticalPosition]


class GameSummary(BaseModel):
    """One row of the games list — everything the list views render,
    nothing they don't.

    Deliberately not a `Game`: the full record ships `pgn` (~2.3 KB a
    row) and every SAN move, which no list view renders, and at
    thousands of rows that weight is what forced the frontend to cap
    its fetch-everything helper — truncating the Dashboard's stats.
    `first_plies` is the first 6 SAN plies: the exact prefix the
    repertoire drill-through needs to derive the player's three-move
    system client-side. It is not the game record; that stays on
    `GameDetail`.
    """

    id: str
    color: Color
    time_class: TimeClass
    result: Result
    end_time: int
    opponent: str
    player_rating: int
    opponent_rating: int
    accuracy: float | None = None
    termination: str | None = None
    first_plies: list[str]
    opening: Opening | None = None
    analyzed: bool = False


class GameDetail(Game):
    opening: Opening | None = None
    analysis: GameAnalysis | None = None


class AnalyzedGame(Game):
    analysis: GameAnalysis
    opening: Opening | None = None
