# Component 3 — Storage (SQLite)

Persists games, analyses, and opening classifications so nothing is
fetched or analyzed twice. Plain repository functions over the
stdlib `sqlite3` module (WAL mode, synchronous) — no ORM. Storage is
deliberately sync; FastAPI runs sync code in its threadpool.

## Schema

```sql
games (
  id TEXT PRIMARY KEY, username TEXT NOT NULL, color TEXT,
  pgn TEXT, san_moves TEXT,          -- JSON array
  time_control TEXT, time_class TEXT, result TEXT, end_time INTEGER,
  opponent TEXT, player_rating INTEGER, opponent_rating INTEGER,
  accuracy REAL,             -- chess.com's own, nullable
  opening_eco TEXT, opening_name TEXT, opening_ply INTEGER
);
analyses (
  game_id TEXT PRIMARY KEY REFERENCES games(id),
  depth INTEGER, evals TEXT,         -- JSON list[MoveEval]
  acpl_by_phase TEXT, judgment_counts TEXT
);
```

Indexes on `games(username, end_time)`. Migrations are numbered SQL
files applied at open; the current version lives in `user_version`.

## Interface

```python
def open_db(db_path: Path) -> Db   # connection + migrations applied

# Game repo
def upsert_games(db: Db, games: list[Game]) -> None
def list_games(db: Db, username: str, filters: GameFilters) -> ...
def get_game(db: Db, game_id: str) -> GameDetail | None
def latest_game_time(db: Db, username: str) -> int | None  # sync cut
def games_needing_analysis(db, username: str, depth: int) -> list[Game]
def set_opening(db: Db, game_id: str, opening: Opening) -> None

# Analysis repo
def save_analysis(db: Db, analysis: GameAnalysis) -> None
def list_analyses(db: Db, username: str) -> list[GameAnalysis]
```

Pydantic handles the JSON columns (`model_dump_json` /
`model_validate_json`).

## Dependencies

- `chess_coach.domain` and the stdlib. Nothing else.
- Consumed only by the [API layer](07-server.md). It stores what
  [ingestion](02-ingestion.md), [engine](04-engine.md), and
  [openings](05-openings.md) produce but never imports them.

## Build plan

1. `open_db` + migration runner + migration 001 (schema above).
2. Game repo functions with JSON (de)serialization helpers.
3. Analysis repo functions.
4. Tests on a temp-file database covering each function and re-open.
