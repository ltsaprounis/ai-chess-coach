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

| # | Component | Doc                              | Depends on            |
|---|-----------|----------------------------------|-----------------------|
| 1 | Config    | [01-config.md](01-config.md)     | nothing               |
| 2 | Ingestion | [02-ingestion.md](02-ingestion.md) | domain types        |
| 3 | Storage   | [03-storage.md](03-storage.md)   | domain types          |
| 4 | Engine    | [04-engine.md](04-engine.md)     | domain types          |
| 5 | Openings  | [05-openings.md](05-openings.md) | domain types          |
| 6 | Coach     | [06-coach.md](06-coach.md)       | domain types, LLM SDK |
| 7 | API       | [07-server.md](07-server.md)     | components 1-6        |
| 8 | Frontend  | [08-frontend.md](08-frontend.md) | backend HTTP API only |

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
   types below). Nothing else is shared.
3. Config values are injected as plain arguments. No component other
   than config reads files or environment variables.
4. The frontend knows only the HTTP API defined in
   [07-server.md](07-server.md). Its request/response types are
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

Types may grow, but changes to them are contract changes — update the
affected component docs in the same commit.
