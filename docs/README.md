# AI Chess Coach — Documentation Index

[PROPOSAL.md](PROPOSAL.md) is the high-level pitch and
[NEW-FEATURE-PROPOSAL.md](NEW-FEATURE-PROPOSAL.md) holds the
prioritized candidates for what to build next.
[GUIDELINES.md](GUIDELINES.md) holds the cross-cutting engineering
rules — toolchain, boundary enforcement, style, testing, CI. Each
component below has its own build plan. Components are decoupled: the
API layer is the only module that composes them, and everything else
communicates through the shared domain types and plain function
interfaces.

Three directories hold work that is not a component:

- [future-improvements/](future-improvements/) — designs evaluated
  but not built, each with its reasoning: deliberately deferred, or
  planned and not yet scheduled. A doc leaves here by being built
  (its contracts migrate into the component docs) or by being
  rejected outright.
- [archive/](archive/README.md) — reviews, scans and fix iterations
  that are closed out, kept for their measurements and reasoning.
  Nothing here describes current plans; anything they left open was
  handed to `future-improvements/` or to a component doc first.
- [presentations/](presentations/) — self-contained HTML decks that
  explain the project to an audience. They restate what the docs
  below already say; the docs are the source of truth, so a deck is
  refreshed from them rather than cited as a contract.
  [architecture-overview.html](presentations/architecture-overview.html)
  covers the system map, the three data flows, the prompt flows, the
  coach's tool surface with a worked chat turn, and the design
  decisions behind them.

The backend is Python end to end; TypeScript appears only in the
`web/` frontend.

## Component docs

| # | Component | Doc                                | Depends on              |
|---|-----------|------------------------------------|-------------------------|
| 1 | Config    | [01-config.md](01-config.md)       | domain; pydantic, PyYAML|
| 2 | Ingestion | [02-ingestion.md](02-ingestion.md) | domain; httpx, python-chess |
| 3 | Storage   | [03-storage.md](03-storage.md)     | domain; stdlib sqlite3  |
| 4 | Engine    | [04-engine.md](04-engine.md)       | domain; python-chess    |
| 5 | Openings  | [05-openings.md](05-openings.md)   | domain; python-chess    |
| 6 | Coach     | [06-coach.md](06-coach.md)         | domain; claude-agent-sdk, github-copilot-sdk, python-chess |
| 7 | API       | [07-api.md](07-api.md)             | components 1-6; FastAPI |
| 8 | Frontend  | [08-frontend.md](08-frontend.md)   | backend HTTP API only   |

## Architecture

```
web (TS) ──HTTP/SSE──▶ api ──▶ ingestion  (chess.com public API)
                        ├────▶ storage    (SQLite)
                        ├────▶ engine     (Stockfish via UCI)
                        ├────▶ openings   (ECO position lookup)
                        ├────▶ coach      (report + LLM provider)
                        └────▶ config     (loaded once at startup)
```

## Decoupling rules

1. Only the API layer (`chess_coach.api`) imports other components.
   Components 1-6 never import each other.
2. Components share one contract module: `chess_coach.domain` (the
   types below). A component may also define parameter/result types
   on its own public surface (e.g. the engine's `Progress`, storage's
   `GameFilters`); `domain` holds types used by more than one
   component.
3. Config values are injected as plain arguments. Only config reads
   configuration and environment variables; components read data
   files (TSVs, the DB, the engine binary) only at injected paths.
4. The frontend knows only the HTTP API defined in
   [07-api.md](07-api.md). Its request/response types are
   generated from the FastAPI OpenAPI schema — never hand-written.

Rules 1-3 are enforced with import-linter (see
[GUIDELINES.md](GUIDELINES.md)).

## Repository layout

```
docs/                    this documentation
engines/stockfish/       git submodule (official Stockfish)
vendor/chess-openings/   git submodule (lichess ECO database)
backend/                 uv project — Python 3.12+
  pyproject.toml
  src/chess_coach/
    domain.py            shared domain types (pydantic)
    config/  ingestion/  storage/  engine/
    openings/  coach/  api/
web/                     Vite + React + TypeScript frontend
```

## Shared domain types (`chess_coach/domain.py`)

Pydantic models — validated at the edges, serializable everywhere.

```python
MATE_SCORE = 10_000   # mate folded to ±cp for loss arithmetic
OPENING_PLIES = 20    # phase boundaries: shared by engine (which tags
ENDGAME_MATERIAL = 13 # moves) and coach (which re-derives the tags
PIECE_POINTS = {...}  # when aggregating), so the rule cannot drift

Color = Literal["white", "black"]
Result = Literal["win", "loss", "draw"]
TimeClass = Literal["bullet", "blitz", "rapid", "daily"]
Judgment = Literal["best", "good", "inaccuracy", "mistake", "blunder"]
Phase = Literal["opening", "middlegame", "endgame"]

class Thresholds(BaseModel):    # centipawn-loss judgment cutoffs
    inaccuracy: int; mistake: int; blunder: int

class BrilliantThresholds(BaseModel):   # sound-sacrifice cutoffs
    sac_points: int; best_tolerance_cp: int    # (docs/06-coach.md,
    winning_cap_cp: int; sound_floor_cp: int   #  "Highlights")

class Game(BaseModel):
    id: str; username: str; color: Color
    pgn: str; san_moves: list[str]; time_control: str
    time_class: TimeClass; result: Result; end_time: int
    opponent: str; player_rating: int; opponent_rating: int
    accuracy: float | None   # chess.com's own, when provided
    termination: str | None  # raw chess.com code behind `result`:
                             # timeout/resigned/checkmated/… — None
                             # until a game is re-synced

class MoveEval(BaseModel):
    ply: int; san: str; eval_cp: int | None
    eval_mate: int | None; best_move: str
    cp_loss: int; judgment: Judgment

class EvalLine(BaseModel):   # one MultiPV candidate line; the score
    multipv: int; depth: int # is the eval assuming the line is played
    eval_cp: int | None; eval_mate: int | None   # white's POV
    pv_san: list[str]        # SAN; first entry is the candidate move

class GameAnalysis(BaseModel):
    game_id: str; depth: int; evals: list[MoveEval]
    overall_acpl: float                   # player's moves only
    acpl_by_phase: dict[Phase, float]     # opening/middlegame/endgame
    judgment_counts: dict[Judgment, int]

class Opening(BaseModel):
    eco: str; name: str; ply: int

class Record(BaseModel):              # a W/L/D tally, reused widely
    games: int; wins: int; losses: int; draws: int

class OpeningStats(BaseModel):        # one opening from one side
    eco: str; name: str; color: Color # keyed by (color, eco, name):
    system: str                       #   the player's own first moves
    first_moves: str                  #   the line with both sides
    games: int; wins: int; losses: int; draws: int
    analyzed_games: int
    opening_acpl: float | None        # opening phase only
    avg_cp_loss: float | None         # whole game

class PhaseStats(BaseModel):          # acpl is None when moves == 0 —
    moves: int                        # a phase never reached must not
    acpl: float | None                # read as 0.0 cp loss
    judgment_counts: dict[Judgment, int]

class PlayerReport(BaseModel):
    # Two layers, two denominators (06-coach.md, "Volume and
    # quality"): volume fields describe every stored game in scope,
    # quality fields the analyzed subset. Mixing them made a
    # partly-analyzed archive report a rating from whichever game the
    # engine happened to reach last.
    username: str; games_analyzed: int; player_moves: int
    window_start: int | None; window_end: int | None
    time_class: TimeClass | None      # the filter applied; None = all
    requested_since: int | None; requested_until: int | None
    games_in_scope: int | None        # stored games matching the same
                                      # filters, analyzed or not — the
                                      # coverage denominator
    record: Record; overall_acpl: float
    phases: dict[Phase, PhaseStats]
    judgment_counts: dict[Judgment, int]
    time_classes: list[TimeClassStats]   # rating movement per control,
                                         # each extreme dated
    months: list[MonthStats]             # games/rating/ACPL/blunder %
    periods: list[PeriodStats]           # trailing recent-form windows
    terminations: list[TerminationStats] # how games actually ended
    opponents: OpponentStats | None      # score vs stronger/weaker
    color_records: dict[Color, Record]   # score as White / as Black
    best_win: BestWin | None             # strongest opponent beaten
    streaks: StreakStats | None          # runs + the after-a-loss score
    openings: list[OpeningStats]
    error_patterns: list[ErrorPattern]   # tagged deterministically
    critical_positions: list[CriticalPosition]  # turning points
```

Composites elided above for brevity (`CriticalPosition`,
`TimeClassStats`, `MonthStats`, `PeriodStats`, `OpponentStats`,
`TerminationStats`, `BestWin`, `StreakStats`,
`ErrorPattern`, `GameSummary`, `GameDetail`, `AnalyzedGame`,
`RepertoireGame`, `LlmConfig`, `CoachAgent`, `ChatMessage`,
`PlayerProfile`, `ProfileOpening`) also
live in `domain.py` — the component docs state their shapes where
they are used. `PlayerProfile` is the coach's two-layer distillation
of the report (docs/06-coach.md, "Player profile"), scoped to one
time control: component 6 defines and renders it, 3 stores it beside
its LLM narrative keyed by (player, control), and 7 serves it and
embeds its context block into the other coach prompts.
Types may grow, but changes to them are contract changes —
update the affected component docs in the same commit.

`PlayerReport` and `OpeningStats` are read by both the coaching prompt
and the Dashboard, and `OpeningStats` has two independent producers
(storage's SQL over classified games, coach's Python over analyzed
ones). Their aggregation semantics — move-weighted ACPL, the color
split, the family-rollup key, which ACPL each column means — are
defined once in [06-coach.md](06-coach.md); both implementations are
written against that definition rather than against each other.
