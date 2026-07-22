# Component 3 — Storage (SQLite)

Persists games, analyses, and opening classifications so nothing is
fetched or analyzed twice. Plain repository functions over
`better-sqlite3` (synchronous, in-process, WAL mode) — no ORM.

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
  depth INTEGER, evals TEXT,         -- JSON MoveEval[]
  acpl_by_phase TEXT, judgment_counts TEXT
);
```

Indexes on `games(username, end_time)`. Migrations are numbered SQL
files applied at open; the current version lives in `user_version`.

## Interface

```ts
function openDb(dbPath: string): Db;

// Game repo
upsertGames(games: Game[]): void;
listGames(username: string, filter?): GameRow[];
getGame(id: string): (Game & { analysis?: GameAnalysis;
                               opening?: Opening }) | null;
latestGameTime(username: string): number | null;   // for sync `since`
gamesNeedingAnalysis(username: string, depth: number): Game[];
setOpening(gameId: string, opening: Opening): void;

// Analysis repo
saveAnalysis(a: GameAnalysis): void;
listAnalyses(username: string): GameAnalysis[];
```

## Dependencies

- `shared/types.ts` and `better-sqlite3`. Nothing else.
- Consumed only by the [server](07-server.md). It stores what
  [ingestion](02-ingestion.md), [engine](04-engine.md), and
  [openings](05-openings.md) produce but never imports them.

## Build plan

1. `openDb` + migration runner + migration 001 (schema above).
2. Game repo functions with JSON (de)serialization helpers.
3. Analysis repo functions.
4. Tests on a temp-file database covering each function and re-open.
