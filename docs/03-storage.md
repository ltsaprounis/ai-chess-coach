# Component 3 — Storage (SQLite)

Persists games, analyses, and opening classifications so nothing is
fetched or analyzed twice. Plain repository functions over the
stdlib `sqlite3` module (WAL mode, synchronous) — no ORM. Storage is
deliberately sync; FastAPI runs sync code in its threadpool, so the
shared connection is opened with `check_same_thread=False` — safe
only because CPython's sqlite3 is serialized (`threadsafety == 3`,
asserted at open).

## Schema

```sql
games (
  id TEXT PRIMARY KEY, username TEXT NOT NULL, color TEXT,
  pgn TEXT, san_moves TEXT,          -- JSON array
  time_control TEXT, time_class TEXT, result TEXT, end_time INTEGER,
  opponent TEXT, player_rating INTEGER, opponent_rating INTEGER,
  accuracy REAL,             -- chess.com's own, nullable
  termination TEXT,          -- raw per-player result code, nullable
  opening_eco TEXT, opening_name TEXT, opening_ply INTEGER
);
analyses (
  game_id TEXT PRIMARY KEY REFERENCES games(id),
  depth INTEGER, evals TEXT,         -- JSON list[MoveEval]
  overall_acpl REAL,                 -- the game's own mean cp loss
  acpl_by_phase TEXT, judgment_counts TEXT
);
explanations (                       -- cached coach move explanations
  game_id TEXT NOT NULL REFERENCES games(id),
  ply INTEGER NOT NULL,              -- 1-based, matches MoveEval.ply
  agent_id TEXT NOT NULL,            -- coach agent that produced it
  text TEXT NOT NULL,                -- the explanation, markdown
  created_at INTEGER NOT NULL,      -- unix seconds
  PRIMARY KEY (game_id, ply, agent_id)
);
reports (                            -- cached whole-report coaching
  username TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  since INTEGER NOT NULL,            -- window start, 0 = open
  until INTEGER NOT NULL,            -- window end, 0 = open
  time_class TEXT NOT NULL,          -- '' = all controls
  prompt_version TEXT NOT NULL,      -- coach.PROMPT_VERSION
  prompt TEXT NOT NULL,              -- what was sent, markdown
  advice TEXT NOT NULL,              -- what came back, markdown
  games_analyzed INTEGER NOT NULL,   -- coverage, for the staleness hint
  created_at INTEGER NOT NULL,       -- unix seconds
  PRIMARY KEY (username, agent_id, since, until, time_class,
               prompt_version)
);
```

The window columns are NOT NULL with sentinels on purpose: SQLite
treats NULLs as distinct in a primary key, so nullable window columns
would make every all-time report a cache miss that silently inserts a
new row and re-bills the model.

Indexes on `games(username, end_time)`. Migrations are numbered SQL
files applied at open; the current version lives in `user_version`.

## Interface

```python
def open_db(db_path: Path) -> Db   # connection + migrations applied

# Game repo
def upsert_games(db: Db, games: list[Game]) -> None
def list_games(db: Db, username: str,
               filters: GameFilters) -> list[GameSummary]
def get_game(db: Db, game_id: str) -> GameDetail | None
def list_players(db: Db) -> list[PlayerSummary]  # saved-players picker
def latest_game_time(db: Db, username: str) -> int | None  # sync cut
def games_needing_analysis(db, username: str, depth: int,
                           limit: int | None = None, *,
                           since: int | None = None,
                           until: int | None = None,
                           time_class: TimeClass | None = None
                           ) -> list[Game]
def count_games_needing_analysis(db, username: str, depth: int, *,
                                 since: int | None = None,
                                 until: int | None = None,
                                 time_class: TimeClass | None = None
                                 ) -> int
#   The optional window/time-class kwargs scope both functions the
#   same way as list_analyzed_games/count_games (since inclusive,
#   until exclusive), so an "analyze this window" run and its
#   remaining count describe the same games. Newest-first order and
#   the depth semantics are unchanged.
def games_missing_opening(db: Db, username: str) -> list[Game]
def list_analyzed_games(db, username: str, *, since: int | None = None,
                        until: int | None = None,
                        time_class: TimeClass | None = None
                        ) -> list[AnalyzedGame]
def count_games(db, username: str, *, since: int | None = None,
                until: int | None = None,
                time_class: TimeClass | None = None) -> int
#   Total stored games matching the filters, analyzed or not — the
#   "of 1,010" denominator behind the report's coverage statement
#   (PlayerReport.games_in_scope). Window semantics are identical to
#   list_analyzed_games (since inclusive, until exclusive), so the
#   count and the analyzed list describe the same scope.
def opening_stats(db, username: str, *, since: int | None = None,
                  until: int | None = None,
                  time_class: TimeClass | None = None) -> list[OpeningStats]
#   since/until: epoch-second window (since inclusive, until exclusive)
#   time_class: restrict to one time control
#   Rows are keyed by (color, eco, name) and carry both move strings,
#   both ACPL columns, and `faced` (opponent-named by `opening_ply`
#   parity, strict majority per row) — the semantics are defined once
#   in 06-coach.md; this is storage's implementation of that
#   definition, over classified games rather than analyzed ones (a SQL
#   fetch, aggregated in Python). Returned
#   most-played first (the coach's implementation sorts by impact
#   instead; both are re-sorted by their consumers).
#   ACPL comes from the per-move `analyses.evals`, not from
#   `overall_acpl`, so the columns stay move-weighted.
def set_opening(db: Db, game_id: str, opening: Opening) -> None

# Analysis repo
def save_analysis(db: Db, analysis: GameAnalysis) -> None

# Explanation cache (coach move explanations are expensive; the API
# layer reads before generating and writes after — one per
# game/ply/agent, upsert overwrites)
def get_explanation(db: Db, game_id: str, ply: int,
                    agent_id: str) -> str | None
def save_explanation(db: Db, game_id: str, ply: int,
                     agent_id: str, text: str) -> None

# Report cache. A whole-report coaching run is the most expensive
# call the app makes, so house policy — user-triggered AND cached —
# applies to it exactly as it does to explanations. `CachedReport`
# is storage's own result type (the cached text plus the coverage
# the UI needs to flag staleness).
class ReportKey(BaseModel):        # every field is part of the key
    username: str; agent_id: str; prompt_version: str
    since: int = 0; until: int = 0     # 0 = open-ended
    time_class: str = ""               # '' = all controls

class CachedReport(BaseModel):
    prompt: str; advice: str
    games_analyzed: int; created_at: int

def get_report(db: Db, key: ReportKey) -> CachedReport | None
def save_report(db: Db, key: ReportKey, prompt: str, advice: str,
                games_analyzed: int) -> int
#   Returns the created_at (unix seconds) it persisted. Storage reads
#   that clock exactly once per call; callers use the return value
#   instead of taking a second, possibly-disagreeing reading.
```

Pydantic handles the JSON columns (`model_dump_json` /
`model_validate_json`).

`GameSummary` is the slim list row — the fields the list views
render (id, color, time_class, result, end_time, opponent, ratings,
accuracy, termination, opening, analyzed) plus `first_plies`, the
first 6 SAN plies, which the repertoire drill-through needs to
derive the player's system client-side. Deliberately **not** a
`Game`: `pgn` is never selected for list rows and the full
`san_moves` never crosses the boundary (it is read and sliced to
`first_plies` inside storage), which is what lets the frontend fetch
the whole archive uncapped
(docs/fixes-2026-07/01-games-list-uncap.md). `GameDetail`
(Game + optional analysis + opening) stays the full record;
`GameFilters` (opening/result/time_class/analyzed/paging) is
storage's own public parameter type.

## Dependencies

- `chess_coach.domain` and the stdlib. Nothing else.
- Consumed only by the [API layer](07-api.md). It stores what
  [ingestion](02-ingestion.md), [engine](04-engine.md), and
  [openings](05-openings.md) produce but never imports them.

## Build plan

1. `open_db` + migration runner + migration 001 (schema above).
2. Game repo functions with JSON (de)serialization helpers.
3. Analysis repo functions.
4. Tests on a temp-file database covering each function and re-open.
