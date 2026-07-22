# Component 4 — Engine (Stockfish analysis)

Runs Stockfish over every position of a game and produces a
`GameAnalysis`: per-move evals, centipawn loss, judgments, and ACPL by
game phase. Owns the engine binary, the UCI sessions, and the worker
pool. Knows nothing about storage, HTTP, or where games come from.

## Getting Stockfish

Official repo as a git submodule at `engines/stockfish`, built once:

```sh
git submodule update --init engines/stockfish
cd engines/stockfish/src && make -j build
```

`make build` auto-detects the architecture on current Stockfish. The
resulting binary path is passed in by the API layer; `brew install
stockfish` (and passing that path) is the documented fallback if the
local build fails. The build step ships as `make engine` in the root
Makefile.

## Interface

python-chess provides the UCI driver (`chess.engine`) — we do not
hand-roll the protocol. The pool owns N engine processes and a queue.

```python
class EngineOptions(BaseModel):
    depth: int
    thresholds: Thresholds    # domain type; values come from config

async def create_pool(bin_path: Path, workers: int) -> AnalysisPool

class AnalysisPool:
    async def analyze_game(
        self, game: Game, opts: EngineOptions,
        on_progress: Callable[[Progress], None] | None = None,
    ) -> GameAnalysis
    async def close(self) -> None    # quits engines cleanly

class Progress(BaseModel):
    game_id: str; ply: int; total_plies: int
```

Internally each worker holds one engine from
`chess.engine.popen_uci(bin_path)` (async API) and calls
`engine.analyse(board, chess.engine.Limit(depth=...))` per position.

## Analysis logic

- Replay `san_moves` on a `chess.Board` to get the position
  before/after each move; eval each position once (positions are
  shared between consecutive moves).
- `cp_loss` = eval drop from the mover's perspective, clamped at 0.
  Mate scores map to ±10000 cp for loss arithmetic
  (`score.score(mate_score=10000)`).
- Judgment from injected thresholds (see [config](01-config.md)):
  loss < inaccuracy → best/good, then inaccuracy/mistake/blunder.
- `evals` covers both sides (the eval graph needs them); ACPL
  (overall + per phase) and judgment counts cover the player's moves
  only.
- Phases: opening = first 10 full moves (refined later by the actual
  book-exit ply), endgame = both sides ≤ 13 points of material,
  middlegame = the rest. ACPL = mean cp_loss of the player's moves in
  that phase.

## Dependencies

- `chess_coach.domain` and python-chess. Depth, thresholds, workers,
  and the binary path are injected by the [API layer](07-api.md)
  from [config](01-config.md) — no imports.
- Results are persisted by the API layer via [storage](03-storage.md).

## Build plan

1. Single-engine wrapper: open, eval position (cp/mate/terminal
   positions), quit.
2. Single-game analyzer implementing the logic above.
3. Worker pool + asyncio queue with progress callbacks and clean
   shutdown (`AnalysisPool.close`).
4. Tests: analysis logic against a stub engine returning canned
   scores; one opt-in integration test on a short fixture game
   (mate-in-1 blunder) against the real binary.
