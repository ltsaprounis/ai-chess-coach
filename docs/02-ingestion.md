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
and surfaces as a typed `UnknownUserError`.

Prior art: `~/repos/chess-guess` (`src/etl/ingest.py`, `process.py`)
uses the same API and informed the normalization rules below.

## Normalization rules

- `id` = the game's `uuid`; usernames are lowercased everywhere
  (chess.com treats them case-insensitively).
- Keep only `rules == "chess"` — variants (bughouse, threecheck,
  kingofthehill, chess960 for v1) are skipped.
- The per-player `result` code maps to `win | draw | loss`:
  - win: `win`
  - draw: `agreed`, `repetition`, `stalemate`, `insufficient`,
    `50move`, `timevsinsufficient`
  - loss: `checkmated`, `timeout`, `resigned`, `lose`, `abandoned`
  - unknown codes log a warning and skip the game.
- `accuracies.{white,black}`, when present, is kept on the `Game` as
  `accuracy` — a free sanity check against our own engine numbers.

## Interface

```ts
function getArchives(username: string): Promise<string[]>;

// Yields one batch per monthly archive, newest month last.
// `since` (epoch seconds) skips months older than the last sync.
function syncGames(username: string, since?: number):
  AsyncGenerator<Game[]>;
```

PGN parsing uses `chess.js`: extract SAN move list, result, and
headers. Games that fail to parse (variants, aborted games) are
skipped with a warning, never thrown.

## Dependencies

- `shared/types.ts` (`Game`) and `chess.js`. Nothing else.
- Consumed by the [server](07-server.md), which pipes yielded batches
  into [storage](03-storage.md). The `since` value comes from storage
  via the server — ingestion itself is stateless.

## Build plan

1. Thin fetch wrapper with retry/backoff and a User-Agent header.
2. `getArchives` + archive fetcher with response typing.
3. PGN → `Game` normalizer (id = chess.com UUID from the game URL).
4. `syncGames` generator combining the above.
5. Tests against recorded JSON fixtures (no live network in CI).
