# Component 3 — Storage (SQLite)

Persists games, analyses, and opening classifications so nothing is
fetched or analyzed twice. Plain repository functions over the
stdlib `sqlite3` module (WAL mode, synchronous) — no ORM. Storage is
deliberately sync; FastAPI runs sync code in its threadpool, so the
shared connection is opened with `check_same_thread=False` — safe
only because CPython's sqlite3 is serialized (`threadsafety == 3`,
asserted at open). Serialized mode covers single C calls, not usage
patterns — concurrent transactions interleave BEGIN/COMMIT, and reads
racing another thread's statements corrupt pysqlite's statement cache
(observed as segfaults at close) — so `Db` is a thin wrapper whose
re-entrant lock serializes every statement: each runs and is fully
fetched under the lock (`execute` returns materialized rows), and
`with db:` holds it across the whole transaction, commit included.

## Schema

```sql
games (
  -- one row per (game, perspective): id is ingestion's perspective id
  -- "{uuid}:{username}" (docs/02-ingestion.md), so a game between two
  -- tracked players stores once per side instead of colliding
  id TEXT PRIMARY KEY, username TEXT NOT NULL, color TEXT,
  pgn TEXT, san_moves TEXT,          -- JSON array
  time_control TEXT, time_class TEXT, result TEXT, end_time INTEGER,
  opponent TEXT, player_rating INTEGER, opponent_rating INTEGER,
  accuracy REAL,             -- chess.com's own, nullable
  termination TEXT,          -- raw per-player result code, nullable
  opening_eco TEXT, opening_name TEXT, opening_ply INTEGER,
  chesscom_uuid TEXT         -- the raw uuid; groups perspective rows
);
analyses (
  game_id TEXT PRIMARY KEY REFERENCES games(id),
  depth INTEGER, evals TEXT,         -- JSON list[MoveEval]
  -- engine.ANALYSIS_VERSION at save time (injected by the API layer;
  -- storage never imports engine). The DEFAULT grandfathers rows
  -- saved before versioning existed as version 1 — the carried-state
  -- semantic — so a version bump marks them all stale at once
  analysis_version INTEGER NOT NULL DEFAULT 1,
  overall_acpl REAL,                 -- the game's own mean cp loss
  acpl_by_phase TEXT, judgment_counts TEXT,
  -- per-perspective aggregates, derived from evals by save_analysis:
  -- player-ply counts and summed losses (whole game and opening
  -- phase), so opening_stats sums four integers per analyzed game
  -- instead of re-parsing every evals blob per request
  player_moves INTEGER, player_loss INTEGER,
  opening_moves INTEGER, opening_loss INTEGER
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
chat_threads (                       -- coach chat conversations
  -- (docs/future-improvements/coach-chat.md). The stored transcript
  -- is the conversation's single source of truth; provider-side
  -- sessions are a cache of it, keyed by the opaque provider_state
  id TEXT PRIMARY KEY,               -- uuid, minted by the API layer
  username TEXT NOT NULL,
  agent_id TEXT NOT NULL,            -- pinned for the thread's life
  scope TEXT NOT NULL,               -- 'report' | 'game'
  game_id TEXT,                      -- scope='game' only; no FK on
  ply INTEGER,                       --   purpose (games never delete)
  since INTEGER NOT NULL,            -- 0 = open, as in reports
  until INTEGER NOT NULL,
  time_class TEXT NOT NULL,          -- '' = all controls
  provider_state TEXT,               -- opaque resume token, nullable
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
chat_messages (
  thread_id TEXT NOT NULL REFERENCES chat_threads(id),
  seq INTEGER NOT NULL,              -- per-thread, 1-based
  role TEXT NOT NULL,                -- 'user' | 'assistant'
  content TEXT NOT NULL,             -- markdown
  created_at INTEGER NOT NULL,
  PRIMARY KEY (thread_id, seq)
);
```

The window columns are NOT NULL with sentinels on purpose: SQLite
treats NULLs as distinct in a primary key, so nullable window columns
would make every all-time report a cache miss that silently inserts a
new row and re-bills the model. The chat tables reuse the same
sentinel convention so the thread's scope compares exactly like a
report key. `chat_threads(username, updated_at)` is indexed for the
thread list.

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
                           version: int,
                           limit: int | None = None, *,
                           since: int | None = None,
                           until: int | None = None,
                           time_class: TimeClass | None = None
                           ) -> list[Game]
def count_games_needing_analysis(db, username: str, depth: int,
                                 version: int, *,
                                 since: int | None = None,
                                 until: int | None = None,
                                 time_class: TimeClass | None = None
                                 ) -> int
#   Needing analysis = no analyses row, or one shallower than
#   `depth`, or one saved under an `analysis_version` older than
#   `version` (the caller passes engine.ANALYSIS_VERSION) — so an
#   engine version bump re-queues every stored game with no endpoint
#   change. The optional window/time-class kwargs scope both
#   functions the same way as list_analyzed_games/count_games (since
#   inclusive, until exclusive), so an "analyze this window" run and
#   its remaining count describe the same games. Newest-first order
#   is unchanged.
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
#   ACPL sums the per-perspective aggregate columns save_analysis
#   derives from the per-move `analyses.evals` — never `overall_acpl`
#   (a per-game mean), so the columns stay move-weighted.
def set_opening(db: Db, game_id: str, opening: Opening) -> None

def list_repertoire_games(
    db: Db, username: str, *, max_plies: int,
    since: int | None = None, until: int | None = None,
    time_class: TimeClass | None = None) -> list[RepertoireGame]
#   The repertoire-tree input (docs/future-improvements/
#   openings-explorer.md): every stored game in scope, analyzed or
#   not — LEFT JOIN on analyses, so an unanalyzed game still comes
#   back with `evals=None`. Window semantics are identical to
#   list_analyzed_games (since inclusive, until exclusive; time_class
#   optional; all default to the full history). Newest-first order.
#   `san_moves` and `evals` are both sliced to `max_plies` inside
#   storage, and `pgn` is never selected — the documented exception to
#   "san_moves never crosses the boundary" (see `GameSummary` above):
#   rows stay bounded by the cap and the consumer is server-side (the
#   openings component), so the uncapped-archive-to-browser problem
#   that rule exists to prevent does not apply here. Evals are parsed
#   from JSON before slicing (SQLite cannot slice a JSON array).

# Analysis repo
def save_analysis(db: Db, analysis: GameAnalysis,
                  version: int) -> None
#   `version` is engine.ANALYSIS_VERSION, injected by the API layer
#   like depth/thresholds are — storage records it, never defines it.

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

# Chat threads (docs/future-improvements/coach-chat.md). Transcripts
# are `domain.ChatMessage` rows; thread row types are storage's own
# surface, like `CachedReport`.
ChatScope = Literal["report", "game"]

class ChatThread(BaseModel):
    id: str; username: str; agent_id: str
    scope: ChatScope
    game_id: str | None = None; ply: int | None = None
    since: int = 0; until: int = 0     # 0 = open, as in ReportKey
    time_class: str = ""               # '' = all controls
    provider_state: str | None = None
    created_at: int; updated_at: int

class ChatThreadSummary(BaseModel):    # one thread-list row
    id: str; scope: ChatScope
    game_id: str | None; ply: int | None
    since: int; until: int; time_class: str
    agent_id: str
    title: str          # first user message, sliced to ~80 chars
    messages: int       # total rows, the cap check's input
    updated_at: int

def create_chat_thread(db: Db, *, thread_id: str, username: str,
                       agent_id: str, scope: ChatScope,
                       game_id: str | None = None,
                       ply: int | None = None,
                       since: int = 0, until: int = 0,
                       time_class: str = "") -> ChatThread
#   Reads the clock once; created_at == updated_at on the returned
#   row, same one-reading rule as save_report.
def get_chat_thread(db: Db, thread_id: str) -> ChatThread | None
def list_chat_threads(db: Db,
                      username: str) -> list[ChatThreadSummary]
#   Most recently updated first.
def delete_chat_thread(db: Db, thread_id: str) -> bool
#   Deletes the thread and its messages in one transaction (explicit
#   delete, never FK cascade); False when the thread doesn't exist.
def list_chat_messages(db: Db,
                       thread_id: str) -> list[ChatMessage]  # seq asc
def append_chat_exchange(db: Db, thread_id: str, user: ChatMessage,
                         assistant: ChatMessage,
                         provider_state: str | None) -> None
#   One transaction: both rows at the next two seqs, provider_state
#   overwritten (None clears it), updated_at set to the assistant
#   message's created_at. A turn is atomic — a user message with no
#   reply is never persisted (docs/07-api.md: aborts persist nothing).
def clear_chat_provider_state(db: Db, thread_id: str) -> None
#   The abort/error path: the discarded turn may have reached the
#   provider's warm session, so the next turn must replay from the
#   stored transcript rather than resume a diverged session.
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
(docs/archive/fixes-2026-07/01-games-list-uncap.md). `GameDetail`
(Game + optional analysis + opening) stays the full record;
`GameFilters` is storage's own public parameter type:
opening_eco (exact), opening_name_like (case-insensitive substring
on the classified name), opponent (case-insensitive exact),
result, time_class, analyzed, since/until (epoch-second window,
since inclusive, until exclusive — the same semantics every other
windowed query here uses), and limit/offset paging. The name,
opponent and window filters exist for the coach chat toolkit's
`find_games` tool (docs/future-improvements/coach-chat.md), which
queries by what a student says — an opponent's name, an opening's
name — rather than by ECO code.

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
