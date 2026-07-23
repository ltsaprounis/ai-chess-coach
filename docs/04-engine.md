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
        on_progress: ProgressCallback | None = None,
    ) -> GameAnalysis
    def stream_eval(
        self, fen: str, depth: int, multipv: int = 1,
    ) -> AsyncGenerator[LiveEval]    # live single-position eval
    async def eval_lines(
        self, fen: str, depth: int, multipv: int = 1,
    ) -> list[EvalLine]              # one-shot: final lines at depth
    async def close(self) -> None    # quits engines cleanly

class Progress(BaseModel):
    game_id: str; ply: int; total_plies: int

class LiveEval(BaseModel):           # snapshot of the candidate lines
    lines: list[EvalLine]            # sorted by multipv rank; domain
                                     # type — see README.md

async def analyze_game(              # pure logic; the pool drives it
    game: Game, opts: EngineOptions, evaluate: EvaluateFn
) -> GameAnalysis

EvaluateFn = Callable[[str], Awaitable[PositionEval]]   # FEN -> eval
ProgressCallback = Callable[[Progress], None]

class PositionEval(BaseModel):       # white-POV eval of one position
    cp: int | None; mate: int | None; best_uci: str | None
    clamped_cp: int                  # property; mate folds to ±MATE_SCORE

class EngineError(Exception): ...    # engine failed to start/misbehaved

MATE_SCORE = 10_000                  # domain constant, re-exported here
```

All engine start-up and protocol failures surface as `EngineError`,
the typed exception callers (the [API layer](07-api.md)) catch and
map; raw `chess.engine` errors never escape this component.

`stream_eval` powers the live analysis board: it parses the FEN
eagerly (raising `ValueError` on an invalid one, before any engine
work), borrows a pool worker (waiting if all are busy analyzing),
and runs one MultiPV search (`multipv` candidate lines; scores are
white-POV, and each line's score is the eval assuming its first move
is played). It maintains a lines-by-rank snapshot from the engine's
per-line infos and yields a `LiveEval` whenever the snapshot changes,
finishing at the target depth. Terminal positions yield nothing.
Closing the iterator early (client gone, position changed) must stop
the search and return the worker to the pool.

`eval_lines` is the non-streaming form — same search, returning only
the final snapshot. It exists for the coach's engine tool and for
seeding explain prompts ([06-coach.md](06-coach.md), wired by the
[API layer](07-api.md)). Terminal positions return `[]`. Both
`stream_eval` and `eval_lines` cap `multipv` at the number of legal
moves naturally (the engine reports fewer lines).

Internally each worker holds one engine from
`chess.engine.popen_uci(bin_path)` (async API) and calls
`engine.analyse(board, chess.engine.Limit(depth=...))` per position;
live searches pass `multipv=` through to `engine.analysis(...)`.

## Analysis logic

- Replay `san_moves` on a `chess.Board` to get the position
  before/after each move; eval each position once (positions are
  shared between consecutive moves).
- `cp_loss` = eval drop from the mover's perspective, clamped at 0.
  Mate scores map to `±MATE_SCORE` (10000 cp) for loss arithmetic
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
  multipv, and the binary path are injected by the
  [API layer](07-api.md) from [config](01-config.md) — no imports.
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
