# Component 2 — Ingestion (chess.com client)

Fetches a user's games from the chess.com public API and normalizes
them into `Game` domain objects. Pure fetch-and-parse: it does not
store anything and does not know the database exists.

## chess.com API

No authentication required. Two endpoints:

- `GET https://api.chess.com/pub/player/{user}/games/archives`
  returns a list of monthly archive URLs (the URLs follow
  `/pub/player/{user}/games/{YYYY}/{MM}`, so specific months can
  also be addressed directly).
- `GET {archiveUrl}` returns that month's games. Each game carries
  `uuid`, `url`, `pgn`, `time_control`, `time_class`, `rules`,
  `end_time`, `white`/`black` (`username`, `rating`, per-player
  `result` code), and sometimes `accuracies` (chess.com's own
  analysis numbers).

Politeness: requests are serial (the API dislikes parallel bursts),
with retry + backoff on 429 and a `User-Agent` header (requests
without one get throttled). A 404 on archives means unknown username
and raises a typed `UnknownUserError`; other transport failures
surface as the base `IngestionError`, also exported.

Prior art: `~/repos/chess-guess` (`src/etl/ingest.py`, `process.py`)
uses the same API and informed the normalization rules below.

## Normalization rules

- `id` = the game's `uuid`; usernames are lowercased everywhere
  (chess.com treats them case-insensitively).
- Keep only `rules == "chess"` — variants (bughouse, threecheck,
  kingofthehill, chess960 for v1) are skipped.
- Keep only games starting from the standard position. A `rules ==
  "chess"` game can still carry a `SetUp`/`FEN` PGN header (chess.com
  custom-position games, e.g. some daily challenges); any such game is
  dropped. `SetUp "1"` paired with `FEN` is the conventional pairing,
  but a bare `FEN` header is treated the same way. **Invariant:**
  every stored `Game` starts from the standard position — downstream
  replay (engine analysis, coach context/report) always rebuilds the
  board from `chess.Board()` rather than honoring a PGN's `FEN`
  header, and relies on ingestion having already dropped anything
  that would make that replay wrong.
- The per-player `result` code maps to `win | draw | loss`:
  - win: `win`
  - draw: `agreed`, `repetition`, `stalemate`, `insufficient`,
    `50move`, `timevsinsufficient`
  - loss: `checkmated`, `timeout`, `resigned`, `lose`, `abandoned`
  - unknown codes log a warning and skip the game.
- The raw code is **also kept verbatim** as `Game.termination`. The
  win/draw/loss collapse discards the difference between losing on
  time, resigning and being mated, which is among the most actionable
  signals a coach has; keeping the code costs one column and makes
  "38% of your losses are on the clock" answerable.
- `accuracies.{white,black}`, when present, is kept on the `Game` as
  `accuracy` — a free sanity check against our own engine numbers.

## Interface

```python
async def get_archives(username: str) -> list[str]

# Yields one batch per monthly archive, newest month last.
# `since` (epoch seconds) skips months older than the last sync and
# drops individual games at or before it — a normal sync never
# re-fetches a stored game. `since=None` therefore doubles as the
# full re-sync path: it re-fetches the entire archive, and storage's
# idempotent upsert backfills columns added after games were stored
# (the API layer's `sync?full=true`).
async def sync_games(
    username: str, since: int | None = None
) -> AsyncIterator[list[Game]]
```

PGN parsing uses python-chess (`chess.pgn.read_game`) to extract the
SAN move list and headers. Games that fail to parse (malformed or
aborted) are skipped with a warning, never raised.

## Dependencies

- `chess_coach.domain` (`Game`), httpx, python-chess. Nothing else.
- Consumed by the [API layer](07-api.md), which pipes yielded
  batches into [storage](03-storage.md). The `since` value comes from
  storage via the API layer — ingestion itself is stateless.

## Build plan

1. Thin httpx wrapper with retry/backoff and a User-Agent header.
2. `get_archives` + archive fetcher with pydantic response models.
3. Raw game → `Game` normalizer implementing the rules above.
4. `sync_games` async generator combining the above.
5. Tests against recorded JSON fixtures (no live network in CI).
