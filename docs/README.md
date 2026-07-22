# AI Chess Coach — Documentation Index

[PROPOSAL.md](PROPOSAL.md) is the high-level pitch. Each component below
has its own build plan. Components are decoupled: the API layer is the
only module that composes them, and everything else communicates through
the shared domain types and plain function interfaces.
[GUIDELINES.md](GUIDELINES.md) holds the cross-cutting engineering
rules — toolchain, boundary enforcement, style, testing, CI.

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
| 6 | Coach     | [06-coach.md](06-coach.md)         | domain; anthropic SDK   |
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
Color = Literal["white", "black"]
Result = Literal["win", "loss", "draw"]
TimeClass = Literal["bullet", "blitz", "rapid", "daily"]
Judgment = Literal["best", "good", "inaccuracy", "mistake", "blunder"]
Phase = Literal["opening", "middlegame", "endgame"]

class Thresholds(BaseModel):    # centipawn-loss judgment cutoffs
    inaccuracy: int; mistake: int; blunder: int

class Game(BaseModel):
    id: str; username: str; color: Color
    pgn: str; san_moves: list[str]; time_control: str
    time_class: TimeClass; result: Result; end_time: int
    opponent: str; player_rating: int; opponent_rating: int
    accuracy: float | None   # chess.com's own, when provided

class MoveEval(BaseModel):
    ply: int; san: str; eval_cp: int | None
    eval_mate: int | None; best_move: str
    cp_loss: int; judgment: Judgment

class GameAnalysis(BaseModel):
    game_id: str; depth: int; evals: list[MoveEval]
    acpl_by_phase: dict[Phase, float]     # opening/middlegame/endgame
    judgment_counts: dict[Judgment, int]

class Opening(BaseModel):
    eco: str; name: str; ply: int

class PlayerReport(BaseModel):
    username: str; games_analyzed: int; overall_acpl: float
    acpl_by_phase: dict[Phase, float]
    judgment_counts: dict[Judgment, int]
    openings: list[OpeningStats]          # games/W/L/D/avg_cp_loss
    critical_positions: list[CriticalPosition]  # fen/played/best/...
```

Composites elided above for brevity (`OpeningStats`,
`CriticalPosition`, `GameSummary`, `GameDetail`, `AnalyzedGame`,
`LlmConfig`) also live in `domain.py` — the component docs state
their shapes where they are used. Types may grow, but changes to
them are contract changes — update the affected component docs in
the same commit.
