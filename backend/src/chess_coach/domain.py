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


class ChatMessage(BaseModel):
    """One chat turn — the unit both the transcript store and the
    provider replay path share.

    Stored per thread by storage (docs/03-storage.md) and passed as
    `history` through the coach provider seam (docs/06-coach.md,
    `CoachProvider.chat`), which is what makes it a domain type. The
    stored transcript is the conversation's single source of truth;
    provider-side sessions are only ever a cache of it.
    """

    role: Literal["user", "assistant"]
    content: str  # markdown
    created_at: int  # unix seconds


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
    cp_loss: int  # centipawns given up by this one move
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
    overall_acpl: float  # centipawns per player move
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
    # Both in CENTIPAWNS per player move, like every other loss
    # aggregate on this module -- renderers divide by 100 at the point
    # they turn one into text and nowhere earlier (docs/06-coach.md,
    # "Units").
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
    acpl: float | None  # centipawns per player move
    judgment_counts: dict[Judgment, int]


class TimeClassStats(BaseModel):
    """Play and rating movement within one time control.

    The extremes carry their dates: "peaked at 1723" is trivia,
    "peaked at 1723 last March and has been below it since" is the
    fact a student measures themselves against. Both are the extreme
    *of the games in scope* — the archive stored here, not chess.com's
    own all-time best — and both are stamped at the **first** game that
    reached the value, since a peak is when they got there.
    """

    time_class: TimeClass
    record: Record
    rating_start: int  # oldest game in the window
    rating_end: int  # newest game in the window
    rating_min: int
    rating_max: int
    # Epoch seconds of the first game at each extreme. None only on a
    # profile snapshot stored before the fields existed (the same
    # backward-compatible absence `Game.termination` carries); every
    # freshly built row sets both.
    rating_max_at: int | None = None
    rating_min_at: int | None = None


class MonthStats(BaseModel):
    """One calendar month — the trend row that says whether it helped."""

    month: str  # "2026-07"
    games: int
    rating_end: int | None  # last rating seen that month
    # The month's median rating, which is what the profile window's
    # drift rule compares (docs/06-coach.md, "Window") -- `rating_end`
    # is one game's outcome and swings with it, and a boundary decided
    # by a single game is not a boundary. Defaulted for snapshots
    # written before the column existed.
    rating_median: int | None = None
    acpl: float | None  # centipawns per player move
    blunder_rate: float | None  # blunders ÷ player moves


class PeriodStats(BaseModel):
    """Performance over one trailing window, so recent form outweighs
    ancient history.

    The months table answers "what happened in March"; this answers
    "how is the student playing *now*", which is the question a profile
    exists to answer. Windows are cumulative and nested (last 30 days
    ⊂ last 90 ⊂ the whole covered span) rather than disjoint buckets:
    a month with four analyzed games produces an ACPL that swings on a
    single bad game, and a narrative reading that wobble as a trend is
    worse than one reading nothing. Nesting means a thin recent window
    is backed by a wider one that is still more recent than the span.

    Carries both denominators for the same reason the report does:
    `games`/`record` are volume over every stored game in the window,
    `analyzed_games`/`player_moves` are the quality figures' own
    sample. A window can have games and no analysis at all, which is
    exactly when `acpl` must read as absent rather than as 0.0.
    """

    label: str  # "last 30 days", "last 90 days", "whole span"
    days: int | None  # trailing length; None = the whole covered span
    games: int  # every stored game in the window
    record: Record
    analyzed_games: int  # how many carry analysis — the quality sample
    player_moves: int  # denominator for acpl and blunder_rate
    # Centipawns per player move; None when the window has no
    # analyzed moves.
    acpl: float | None
    blunder_rate: float | None  # blunders ÷ player_moves
    rating_end: int | None  # last rating seen in the window


class BestWin(BaseModel):
    """The strongest opponent the player beat in scope.

    A milestone, not an aggregate: the one game a student actually
    remembers, and the only figure in a profile that says the ceiling
    is higher than the average. Volume-layer — beating a 1900 is a fact
    about the game, not about whether an engine has looked at it — so
    it is drawn from every stored game, analyzed or not.

    Carries the game's identity for the same reason `CriticalPosition`
    does: the UI deep-links it (`/games/{id}`) and a coach can name the
    opponent and date rather than "one of your games".
    """

    game_id: str
    end_time: int
    time_class: TimeClass
    color: Color  # the side the player had
    opponent: str
    opponent_rating: int
    player_rating: int  # the player's own rating in that game


class StreakStats(BaseModel):
    """Runs and rebounds — what the volume layer knows about momentum.

    Two questions no lifetime average answers. **Are they on a run
    right now**: `current_*` describe the run the most recent game in
    scope belongs to, where a run is consecutive games with the same
    result (a draw is a run of draws, not a break in one), and
    `longest_*` are the longest such runs anywhere in scope. **Do they
    tilt**: `after_loss` is the record over games played in the same
    sitting as, and immediately after, a loss — set against the
    player's overall record it is the difference between "lost six
    today" and "lost one, then lost five chasing it".

    `after_loss` is a strict subset of `record`, and its own `games`
    is the denominator: on a correspondence archive, or one with no
    back-to-back games at all, it is legitimately empty and must read
    as "no sample", never as a 0% score.
    """

    current_result: Result
    current_length: int
    longest_win: int
    longest_loss: int
    after_loss: Record


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
    cp_loss: int  # centipawns given up by this one move
    eval_before_cp: int | None
    eval_before_mate: int | None
    eval_after_cp: int | None
    eval_after_mate: int | None


class PlayerReport(BaseModel):
    """Everything the coaching prompt and the Dashboard read.

    Two layers with two different denominators (docs/06-coach.md,
    "Volume and quality"). **Volume** — `record`, `time_classes` and
    their ratings, `months` game counts, `terminations`, `opponents`,
    `color_records`, `best_win`, `streaks`, and the repertoire's
    `games`/win-loss columns — describes every
    stored game in scope. **Quality** — `overall_acpl`,
    `judgment_counts`, `phases`, `error_patterns`, `critical_positions`,
    the ACPL columns, and `months` ACPL/blunder rate — describes the
    analyzed subset, because nothing else can produce it.

    Mixing the two is what made a partly-analyzed archive report a
    rating from whichever game happened to be analyzed last, months
    after the fact. `games_analyzed` and `games_in_scope` are the two
    denominators, stated rather than implied.

    `window_start`/`window_end` report the extent of the data actually
    covered, so neither the model nor the student has to guess what
    period the numbers describe.
    """

    username: str
    games_analyzed: int
    player_moves: int  # the denominator for judgment_counts
    window_start: int | None  # epoch seconds of the oldest game covered
    window_end: int | None  # epoch seconds of the newest
    # The same bounds over the *analyzed* subset alone. Volume and
    # quality have two denominators (above) and, on a partly-analyzed
    # archive, two spans: an engine that has only reached this year's
    # games makes "average loss over the whole span" a claim about this
    # year wearing two years' clothes. Defaulted so stored snapshots
    # written before the fields existed still parse.
    analyzed_window_start: int | None = None
    analyzed_window_end: int | None = None
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
    overall_acpl: float  # centipawns per player move
    phases: dict[Phase, PhaseStats]
    judgment_counts: dict[Judgment, int]
    time_classes: list[TimeClassStats]
    months: list[MonthStats]  # oldest first
    periods: list[PeriodStats] = []  # trailing windows, narrowest first
    terminations: list[TerminationStats]
    opponents: OpponentStats | None  # None with no games
    # Volume-layer milestones and splits (docs/06-coach.md,
    # "Milestones"). All three default to empty so a stored profile
    # snapshot written before they existed still parses.
    color_records: dict[Color, Record] = {}  # score as White / as Black
    best_win: BestWin | None = None  # None with no win in scope
    streaks: StreakStats | None = None  # None with no games
    openings: list[OpeningStats]
    error_patterns: list[ErrorPattern]
    critical_positions: list[CriticalPosition]


class RatingDelta(BaseModel):
    """Rating movement over one trailing span (docs/06-coach.md,
    "Trajectory").

    `games` rides along because a delta over three games and a delta
    over three hundred are different claims, and the renderers say so.
    """

    days: int  # 30, 90, 180, 365
    rating_then: int
    delta: int  # rating_now - rating_then
    games: int  # games played inside the span


class Drawdown(BaseModel):
    """The largest peak-to-trough fall in the archive, with its
    recovery (docs/06-coach.md, "Trajectory").

    A window is a stationarity assumption and a drawdown is the
    opposite, so this is reported as its own fact rather than left for
    whichever window happens to contain it. `record` covers the fall
    itself; `since_record` covers everything after the trough, which is
    what makes "clawed back" or "still down there" sayable.
    """

    peak: int
    peak_at: int  # epoch seconds of the first game reaching the peak
    trough: int
    trough_at: int
    record: Record  # how they scored through the fall
    since_record: Record  # every game after the trough
    recovered: bool  # the rating has since reached the peak again

    @property
    def depth(self) -> int:
        """Negative: the fall in rating points."""
        return self.trough - self.peak


class RatingTrajectory(BaseModel):
    """Where the student is heading, over the **full** archive
    (docs/06-coach.md, "Trajectory") — never the profile window, whose
    whole job is to hold the level roughly constant.

    A coach's first question is the direction, and averages cannot
    answer it: on a student who has gone 185 to 1479 the archive mean
    describes nobody, while "up 443 points in a year" describes them
    exactly.
    """

    rating_now: int
    deltas: list[RatingDelta] = []  # ascending by days
    rating_max: int
    rating_max_at: int
    rating_min: int
    rating_min_at: int
    games: int  # the whole archive in this control
    window_start: int | None = None  # the full archive's own span
    window_end: int | None = None
    drawdown: Drawdown | None = None  # None when the curve only rises

    def delta(self, days: int) -> RatingDelta | None:
        return next((d for d in self.deltas if d.days == days), None)

    @property
    def improving(self) -> bool:
        """True unless both the 90- and 365-day deltas are <= 0.

        The peak gap is only a headline for a student who is not
        improving (docs/06-coach.md, "Trajectory"): "95 below peak" on
        someone up 443 on the year is a misread, and it is the one the
        first live narrative made. A student with neither delta yet
        measured counts as improving, so the gap stays suppressed
        rather than being asserted on no evidence.
        """
        long_deltas = [d.delta for d in self.deltas if d.days in (90, 365)]
        return not long_deltas or any(d > 0 for d in long_deltas)


class ComparisonGroup(BaseModel):
    """A group of games named for a comparison (docs/06-coach.md,
    "Reading a comparison").

    Every field is a property fixed **before** the game was played.
    There is deliberately no `result` and no rating band: selecting
    games on the thing being measured is what makes a +/-100 rating
    window delete drawdowns and a win-rate-conditioned bucket
    manufacture tilt. `find_games` keeps its `result` filter, because
    it answers "show me games" rather than "is this a tendency".

    All-None means "every game in scope", which is what `within`
    defaults to.
    """

    color: Color | None = None
    opening: str | None = None  # case-insensitive name substring
    time_class: TimeClass | None = None
    since: int | None = None  # epoch seconds, inclusive
    until: int | None = None  # epoch seconds, exclusive

    def label(self) -> str:
        """How the group reads in a rendered comparison row."""
        parts: list[str] = []
        if self.color is not None:
            parts.append(f"as {self.color.capitalize()}")
        if self.opening:
            parts.append(f"in the {self.opening}")
        if self.time_class is not None:
            parts.append(f"in {self.time_class}")
        return " ".join(parts) if parts else "in every game"


class ComparisonInput(BaseModel):
    """One matched pair of buckets, before the family is judged."""

    label: str  # "Tilt", "By color", ...
    left_label: str  # "after a loss"
    left: Record
    right_label: str  # "not after a loss"
    right: Record
    # The gap expected under the null, in percentage points. Zero for
    # almost everything -- but not for colour, where White scores better
    # than Black for every player alive, so a difference of zero would be
    # the anomaly (docs/06-coach.md, "Reading a comparison").
    baseline: float = 0.0


class Comparison(BaseModel):
    """Two disjoint buckets of the same games, with a verdict
    (docs/06-coach.md, "Reading a comparison").

    `significant` is decided over the profile's whole comparison family
    by Benjamini-Hochberg, never per row, which is the entire point: a
    profile makes up to eight of these before a narrative run asks
    anything, and judging each on its own
    manufactures roughly one spurious tendency every other student.
    """

    label: str
    left_label: str
    left: Record
    right_label: str
    right: Record
    gap: float  # percentage points, left score - right score
    baseline: float = 0.0  # the gap expected under the null
    resolution: float  # +/- points; 2 standard errors of the gap
    significant: bool  # survived the BH step-up

    @property
    def excess(self) -> float:
        """How far the gap runs past what the null already expects --
        the quantity actually tested."""
        return round(self.gap - self.baseline, 1)

    @property
    def measurable(self) -> bool:
        """False when either bucket is too small to have a variance at
        all, in which case there is nothing to report either way."""
        return self.left.games > 1 and self.right.games > 1


class ProfileOpening(BaseModel):
    """One repertoire family in the player profile.

    A row of the profile's compact repertoire: a family the player
    chooses (`faced=False`, `moves` is their own system) or a line they
    keep facing (`faced=True`, `moves` is the full line with both sides
    answering). Rolled up from `OpeningStats` under the family rules
    stated once in docs/06-coach.md — the profile restates the
    repertoire, it never re-derives its semantics.
    """

    color: Color
    name: str  # family label — the most-played member's name root
    moves: str  # chosen: the player's own system; faced: the full line
    games: int  # every stored game in the family — volume, not analysis
    score: float  # (wins + draws/2) / games, 0-1
    faced: bool
    # Centipawns per move over this family's opening plies, and over
    # every player move in its games. Quality layer, so both are None
    # when no game in the family has been analyzed. Defaulted for
    # snapshots stored before the columns existed -- without them the
    # profile could not say that a student's Pirc costs 0.32 pawns a
    # move out of the book against 0.23 in their London, which is the
    # sharpest repertoire signal there is.
    opening_acpl: float | None = None
    avg_cp_loss: float | None = None


class PlayerProfile(BaseModel):
    """The durable who-is-this-student artifact (docs/06-coach.md).

    Two layers. Deterministic facts, distilled from a `PlayerReport` by
    `build_profile` — free, recomputed on demand. And an LLM
    `narrative`, generated only on explicit user action and cached by
    storage (docs/03-storage.md, `player_profiles`), where the facts
    persist beside it as the snapshot the narrative described. Kept
    compact on purpose: `render_profile_context` turns it into the
    ~250-token block other coach prompts embed at the top, so every
    list here is capped by the builder.

    Inherits `PlayerReport`'s volume/quality split and both of its
    denominators — see that docstring. `games_covered` is the analyzed
    sample behind the quality figures; `games_in_scope` is every stored
    game behind the ratings, records and repertoire counts.
    """

    username: str
    # The scope these facts describe. `time_class` is the control they
    # cover (None = all controls mixed) and doubles as storage's key for
    # the narrative: a 2100 bullet player and their 1500 rapid self are
    # different students, and one profile averaging both describes
    # neither. An embedded profile states this — without it the block
    # would present one control's numbers as the whole player.
    time_class: TimeClass | None = None
    games_covered: int  # analyzed games — the quality figures' sample
    games_in_scope: int = 0  # every stored game behind the volume figures
    window_start: int | None  # covered span, as in PlayerReport
    window_end: int | None
    player_moves: int  # denominator for judgment_counts
    overall_acpl: float  # centipawns per player move
    judgment_counts: dict[Judgment, int]
    phases: dict[Phase, PhaseStats]
    time_classes: list[TimeClassStats]
    months: list[MonthStats]  # most recent months only, oldest first
    periods: list[PeriodStats] = []  # trailing windows, narrowest first
    # Volume-layer figures, copied from the report (docs/06-coach.md,
    # "Milestones"): the overall record, what a student remembers, and
    # how they win and lose — none of which any ACPL average expresses.
    # `terminations` rides uncapped, unlike every other list here: it is
    # bounded by chess.com's own result-code vocabulary, and a capped
    # list would make the totals its renderers state disagree with the
    # records above them. All default to empty, so a snapshot stored
    # before they existed still parses — the fields simply read as
    # absent until the next regeneration.
    record: Record = Record()  # every stored game in scope
    color_records: dict[Color, Record] = {}
    best_win: BestWin | None = None
    streaks: StreakStats | None = None
    opponents: OpponentStats | None = None
    terminations: list[TerminationStats] = []
    openings: list[ProfileOpening]  # chosen + faced, capped per color
    error_patterns: list[ErrorPattern]
    # The span the *analyzed* games cover, which is not the span above:
    # `window_start`/`window_end` bound every stored game in scope,
    # these bound the subset an engine has reached. Stating only a count
    # ("1,158 of 1,925 analyzed") let the first live narrative call a
    # seven-month quality figure the student's "whole span", because
    # nothing said the analyzed games were all recent.
    analyzed_window_start: int | None = None
    analyzed_window_end: int | None = None
    # Full-archive direction, deliberately outside the profile window
    # (docs/06-coach.md, "Trajectory"). None on an archive too short to
    # measure one.
    trajectory: RatingTrajectory | None = None
    # True when the window had to keep extending past the drift bound to
    # reach a usable sample, so its outcome rates necessarily span a
    # change in the student's level. Both renderers say so; a student
    # mid-climb gets a caveat rather than a 40-game window.
    window_spans_level_change: bool = False
    # Matched bucket comparisons with BH verdicts (docs/06-coach.md,
    # "Reading a comparison"). Empty on a profile with no sample to
    # compare, and on snapshots stored before the family existed.
    comparisons: list[Comparison] = []
    narrative: str | None = None  # stored LLM layer; None until generated


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


class RepertoireGame(BaseModel):
    """One game as the repertoire tree consumes it — moves and, when
    analyzed, per-ply evals, both sliced to the caller's ply cap.

    Produced by storage (docs/03-storage.md, `list_repertoire_games`),
    consumed by openings (`build_repertoire`), which is what makes it a
    domain type. Slicing happens inside storage: rows stay bounded and
    `pgn` never rides along, so the type can cross the boundary without
    re-opening the uncapped-archive problem `GameSummary` exists to
    avoid.
    """

    id: str
    color: Color
    result: Result
    san_moves: list[str]  # sliced to the requested cap
    evals: list[MoveEval] | None  # same slice; None if unanalyzed


class GameDetail(Game):
    opening: Opening | None = None
    analysis: GameAnalysis | None = None


class AnalyzedGame(Game):
    analysis: GameAnalysis
    opening: Opening | None = None


# --- Coach chat: the game-scan tool (docs/06-coach.md, "Chat") ---

ScanEventName = Literal[
    "sacrifice", "eval_swing", "comeback", "delivered_mate", "castled"
]


class ScanEventSpec(BaseModel):
    """One step of a `scan_games` match sequence (docs/06-coach.md,
    "Chat").

    Which extra fields apply depends on `event`; the rest are
    ignored. `within_plies` bounds the gap to the previous step's
    match ply, so it means nothing on the first step. Caps and
    validation live at the tool boundary (the JSON schema the model
    sees, plus the API toolkit's clamps), not here — mirroring how
    `GameFilters.limit` is clamped by its caller.
    """

    event: ScanEventName
    piece: Literal["queen", "rook", "minor"] = "minor"  # sacrifice
    sound_only: bool = False  # sacrifice
    min_swing_pawns: float = 3.0  # eval_swing
    direction: Literal["gained", "lost"] = "gained"  # eval_swing
    side: Literal["short", "long", "any"] = "any"  # castled
    within_plies: int | None = None  # steps 2+: max gap to previous


class ScanSpec(BaseModel):
    """An ordered event sequence to find within single games."""

    match: list[ScanEventSpec]


class ScanHit(BaseModel):
    """One matched step: where it happened and what the annotations
    say.

    `detail` is the rendered annotation line (net points, realizes or
    declined, soundness, balance, evals) — prose owned by coach, so
    its wording stays beside the rest of the prompt text.
    `fen_before` is the position before the move, ready to hand to
    `analyze_position`.
    """

    ply: int  # 1-based, matches MoveEval.ply
    san: str
    fen_before: str
    detail: str


class ScanMatch(BaseModel):
    """One game matching the whole sequence: one hit per step, in
    order."""

    game: GameSummary
    hits: list[ScanHit]


class ScanOutcome(BaseModel):
    """A scan's matches plus the denominators that make coverage a
    fact the model reads rather than estimates (docs/06-coach.md,
    "Chat").

    `eligible` counts every stored game matching the metadata
    filters; `scanned` those actually inspected (the candidate cap
    bounds it); `unverified_scanned` the scanned games with no
    stored analysis — moves-only events still match there, with
    eval-backed annotations rendered as unverified;
    `skipped_unanalyzed` the games an eval-reading event could not
    inspect at all.
    """

    eligible: int
    scanned: int
    unverified_scanned: int
    skipped_unanalyzed: int
    truncated: bool
    matches: list[ScanMatch]


class ScanCandidate(BaseModel):
    """One game as the scan consumes it — summary identity, full
    moves, and evals when analyzed.

    Produced by storage (docs/03-storage.md, `scan_candidates`),
    consumed by coach's event detectors, which is what makes it a
    domain type. Like `RepertoireGame`, `pgn` never rides along;
    unlike it, the moves are unsliced — events live anywhere in the
    game.
    """

    summary: GameSummary
    san_moves: list[str]
    evals: list[MoveEval] | None  # None if unanalyzed


class GameSearchPage(BaseModel):
    """One page of `find_games` results with the totals that make
    coverage honest (docs/06-coach.md, "Chat"): `total` counts every
    match, the page shows `games` starting at `offset`, newest
    first."""

    games: list[GameSummary]
    total: int
    offset: int
