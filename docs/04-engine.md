# Component 4 — Engine (Stockfish analysis)

Runs Stockfish over every position of a game and produces a
`GameAnalysis`: per-move evals, centipawn loss, judgments, and ACPL by
game phase. Owns the engine binary, the UCI protocol, and the worker
pool. Knows nothing about storage, HTTP, or where games come from.

## Getting Stockfish

Official repo as a git submodule at `engines/stockfish`, built once:

```sh
git submodule update --init engines/stockfish
cd engines/stockfish/src && make -j build
```

`make build` auto-detects the architecture on current Stockfish. The
resulting binary path is passed in by the server; `brew install
stockfish` (and passing that path) is the documented fallback if the
local build fails. The build step lives in the root `package.json` as
`setup:engine`.

## Interface

```ts
type EngineOptions = { depth: number; thresholds: Thresholds };

// One UCI engine process (spawn, handshake, eval, quit).
function createEngine(binPath: string): Engine;
Engine.evalPosition(fen: string, depth: number):
  Promise<{ cp: number | null; mate: number | null; best: string }>;

// Pool of N engines consuming a game queue.
function createAnalysisPool(binPath: string, workers: number): Pool;
Pool.analyzeGame(game: Game, opts: EngineOptions):
  Promise<GameAnalysis>;
Pool.on('progress', (e: { gameId: string; ply: number;
                          totalPlies: number }) => void);
```

## Analysis logic

- Replay `sanMoves` with `chess.js` to get the FEN before/after each
  move; eval each position once (positions are shared between moves).
- `cpLoss` = eval drop from the mover's perspective, clamped at 0.
  Mate scores map to ±10000 cp for loss arithmetic.
- Judgment from injected thresholds (see [config](01-config.md)):
  loss < inaccuracy → best/good, then inaccuracy/mistake/blunder.
- Phases: opening = first 10 full moves (refined later by the actual
  book-exit ply), endgame = both sides ≤ 13 points of material,
  middlegame = the rest. ACPL = mean cpLoss of the player's moves in
  that phase.

## Dependencies

- `shared/types.ts` and `chess.js`. Depth/thresholds/workers/binPath
  are injected by the [server](07-server.md) from
  [config](01-config.md) — no imports.
- Results are persisted by the server via [storage](03-storage.md).

## Build plan

1. UCI wrapper over `child_process` with a line-based reply parser.
2. `evalPosition` (handles `cp`, `mate`, and stalemate/checkmate).
3. Single-game analyzer implementing the logic above.
4. Worker pool + queue with progress events and graceful shutdown.
5. Tests: parser unit tests on canned UCI output; integration test on
   a short fixture game (mate-in-1 blunder) against the real binary.
