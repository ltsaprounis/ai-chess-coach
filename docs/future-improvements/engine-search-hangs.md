# Engine workers hang: fixed-depth search has no cost bound

Status: analysed and measured 2026-07-28, fix not scheduled. The
defect lives in [04-engine.md](../04-engine.md)'s component; the
reasons nobody noticed for forty minutes at a time are spread across
[07-api.md](../07-api.md) and `backend/scripts/backfill.py`.

Two consequences beyond the hangs themselves, both established by
measurement below: **every stored analysis is affected** and needs
re-running once a fix lands, and **every engine entry point is
exposed**, not only the backfill — the live eval board and the
coach's engine tool included.

## What happened

The 2026-07-28 rapid backfill (`--since 2026-01-01 --time-class
rapid`, 677 games) hung three times. Each hang pinned one of the two
pool workers at 100% CPU indefinitely and only a manual `kill` of the
Stockfish process cleared it. The job did finish complete — all 1,130
rapid-2026 games are analysed, every game dropped by a hang was
retried and succeeded — but it took roughly 2h40m against the ~70
minutes its own healthy rate implied.

| Batch | Wall time | Outcome                                     |
|-------|-----------|---------------------------------------------|
| 1     | 10m23s    | 100/100 at 6.2 s/game, both workers healthy |
| 2     | 43m20s    | 99/100 — worker hung, killed manually       |
| 3     | ~24m      | 99/100 — hung, killed manually              |
| 4     | ~30m      | 99/100 — a slow stall *and* a hang + kill   |
| 5-7   | ~45m      | clean                                       |

Batch 1 and 2 timings are exact (the CLI printed them); 3-7 are read
off the CLI's batch lines and the process table. Each hang cost
exactly one game and 20-35 minutes.

## What it is not

Ruled out during the run, each with evidence, because all three were
plausible enough to chase:

- **Not memory.** `vm.swapusage` showed `total = 0.00M, used =
  0.00M` — the machine never swapped — with 76% free and the hung
  worker's RSS pinned at exactly 372,448 KB across samples.
- **Not a poison game.** Both culprit games analysed fine when
  retried in a later batch. There is no permanently bad game.
- **Not "the end of the batch", though it always looked like it.**
  With two workers, the healthy one drains the rest of the batch
  while the hung one holds its slot, so the symptom is always
  `99/100` no matter when the hang started. In batch 2 the CPU
  accounting showed the hang began early: the healthy worker had 23
  minutes of CPU across 199 games (~7 s/game, the normal rate) while
  the hung one had 51 minutes at ~98% duty for its entire life.
- **Not an intrinsically expensive position.** This one is the
  interesting one — see below.

## Root cause: identical search, 0.03 s or 25 s, depending on history

The FEN the server logged when the batch-2 worker was killed is
`8/8/3B4/1KP5/6k1/5p2/8/3q4 b - - 1 60` (ply 119 of game
`77601cfd…`, a queen against bishop and pawn ending). Analysed at
`depth=16`, the same search on the same binary:

| Engine state before the search                | Time    | Nodes      | Result    |
|-----------------------------------------------|---------|------------|-----------|
| cold process, position in isolation           | 0.03 s  | 83,627     | Mate(+8)  |
| after its own game's plies 1-118              | 25.00 s | 48,620,323 | Cp(+2617) |
| after 12 unrelated games, then its own plies  | 0.04 s  | 86,773     | Cp(+2895) |
| the 25 s engine, after one `ucinewgame`       | 0.03 s  | 83,627     | Mate(+8)  |

A 581× swing in nodes searched, from nothing but what the process
analysed beforehand. A depth sweep on a cold engine confirms the
position itself is cheap at every depth (10 → 18 all land between
0.00 s and 0.04 s), and an ordinary middlegame position costs 0.12 s.
So the warm search is 833× this position's own cold cost and 200×
an ordinary position's — and the hangs observed in the run lasted
20-35 minutes, two orders of magnitude beyond even that.

Two things follow, and the second is the one that makes this
awkward:

- **Clearing the accumulated state fixes it.** One `ucinewgame`
  returned the engine to a bit-identical cold result (83,627 nodes).
  The state is never cleared today: python-chess only sends
  `ucinewgame` when the `game` argument *changes*
  (`chess/engine.py:1624`), and `Engine.evaluate`
  ([uci.py:56](../../backend/src/chess_coach/engine/uci.py:56))
  never passes one — so a worker's transposition table and history
  heuristics persist for the life of the process, across hundreds of
  unrelated games.
- **Per-game clearing would not have prevented this one.** The 25 s
  case was poisoned by the game's *own* preceding 118 plies, which a
  `ucinewgame` between games leaves untouched. Adjacent plies share
  most of their search tree, so they are exactly the entries most
  likely to be probed — and, when they carry mate-ish bounds from a
  neighbouring position, most likely to send the search into
  repeated re-searches.

The effect is not monotonic in "amount of state": priming with 12
extra games made it fast again, presumably because 80M nodes of
unrelated traffic evicted or aged out the poisonous entries. That
unpredictability is the real finding. **The cost of `go depth 16` is
not a function of the position; it is a function of the position and
an unbounded amount of process history, and it has no upper bound.**

## The same bug corrupts results, not just throughput

The table above shows the warm search returning `Cp(+2617)` where the
cold search finds `Mate(+8)`. `MATE_SCORE` folds mate to ±10,000
(`domain.py:11`), so those two evaluations of one position sit 7,383
cp apart — thirty-six times the 200 cp blunder threshold. Since
`_cp_loss` is the difference between consecutive positions' clamped
scores
([analysis.py:96](../../backend/src/chess_coach/engine/analysis.py:96)),
a move evaluated across that discrepancy can be judged a blunder or
sound depending on nothing but what the worker analysed an hour
earlier.

Repeating the same analysis five times inside one process gave
`Mate(+8)` then `Mate(+7)` four times, with node counts falling from
83,627 to 7,974. So stored analyses are not reproducible: re-running
the identical backfill can produce different `cp_loss`, different
judgment counts, and a different ACPL for the same game. `analyses`
rows record `depth = 16` and imply a fixed standard that the engine
does not actually deliver.

## How wrong is the stored data? Measured, not estimated

Twelve stored rapid games (six of the longest, six ordinary) were
re-analysed with a cleared engine per position and diffed against
what the backfill saved. The re-run is a valid reference: two
independent cleared runs of the same game are **bit-identical** —
204/204 and 170/170 moves, ACPL equal to the decimal — so every
difference below is error in the stored row, not measurement noise.

| Game       | Plies | Stored ACPL | Deterministic ACPL |
|------------|-------|-------------|--------------------|
| `29c44e31` | 204   | 326.1       | **114.4**          |
| `4decdfd1` | 170   | 258.6       | **84.6**           |
| `7ad9b1f4` | 62    | 421.9       | 397.9              |
| `613e6bd9` | 211   | 122.2       | 125.2              |
| `d3d1ba78` | 73    | 616.3       | 619.4              |
| (7 others) |       | within ~6%  |                    |

Across 1,565 moves: 89.6% of evals differ from the deterministic
reference, 24% of judgments change, and 63 moves (4%) move by 100 cp
or more of loss. Most judgment changes are small — `_judge` assigns
"best" only on an exact move match
([analysis.py:110](../../backend/src/chess_coach/engine/analysis.py:110)),
so a different-but-equal best move flips best↔good without meaning
anything — but the ACPL column is the coach report's headline number
and two of twelve games have it wrong by a factor of three, always in
the same direction: mate-score flicker inflates the stored value.

The error concentrates in long games, where mating sequences live.
Of 1,202 analysed games today, 27 run to 150+ plies and 248 to
100-149.

## Every engine entry point is exposed, not just the backfill

Nothing about this is specific to `make backfill`; that job was
simply the first thing to run the engine for hours. All three
front-end paths reach the same unbounded, never-cleared search:

| Trigger                                   | Path                              | Exposed |
|-------------------------------------------|-----------------------------------|---------|
| "Analyze the rest" / games-list analyze    | `pool.analyze_game` → `evaluate`  | identical code to the backfill |
| Live eval board (`GET /api/eval`)          | `stream_eval` → `stream_infos`    | same missing `game` argument |
| Coach `analyze_position` tool, explain SSE | `eval_lines` → `stream_infos`     | same |

`stream_eval` and `eval_lines` call `engine.analysis(...)`
([uci.py:78](../../backend/src/chess_coach/engine/uci.py:78)) with no
`game` argument either, so the live eval board and the coach's tool
share the never-cleared state and can disagree with the stored
analysis of the very same position.

Two consequences make the front-end paths worse than the batch one,
not better:

- **The pool is shared and small.** `engine.workers` defaults to 2, so
  a search wedged by a live-eval request removes half the capacity
  for everything else, and two wedges leave the app unable to analyse
  anything until a restart. The backfill at least fails one game and
  moves on.
- **A wedge is user-facing there.** An eval bar that never resolves,
  or a coach report whose engine tool never returns, with no batch
  boundary to make the stall visible.

## Why nothing caught it for forty minutes

Five independent gaps, each individually reasonable:

1. **No timeout exists anywhere on an engine call.** Neither
   `Engine.evaluate` nor `AnalysisPool.analyze_game` bounds how long
   a position may take.
2. **Retirement only triggers on `EngineError`**
   ([pool.py:125](../../backend/src/chess_coach/engine/pool.py:125)).
   A hang raises nothing, so the retire-and-respawn machinery — which
   worked perfectly all three times, once a human supplied the
   `kill` — never fires on its own.
3. **Run events are batch-grained.** `run_done` / `run_failed` only
   publish when the whole `asyncio.gather` finishes, so a run with
   one stuck game emits nothing at all.
4. **The per-position signal exists and nobody consumes it.** The
   progress SSE carries a `progress` event per evaluated position
   with `game_id` and `ply`. Absence of those events is a perfect
   hang detector — it is what diagnosed all three hangs in about 60
   seconds — but no consumer treats silence as a fault.
5. **The backfill CLI's stall guard is too coarse.** It compares
   games saved between batches, so 99 of 100 reads as healthy
   progress. Its live line shows `games x/100` and never the ply
   inside the current game, which is why a genuinely slow game (batch
   4, ~7 s/ply) and a dead worker looked identical from the terminal.

## Options

Measured on this machine (14 cores, Stockfish dev-20260720, Threads=1,
Hash=16 MB, depth 16), analysing whole games position by position the
way `analyze_game` does:

| Approach                          | The 128-ply culprit | 3 ordinary games (216 plies) |
|-----------------------------------|---------------------|------------------------------|
| carried state (today)             | 111.5 s             | 23.8 s                       |
| `ucinewgame` before every position | **11.7 s**          | 24.7 s (+3.8%)               |
| `Limit(depth=16, time=2.0)`       | 18.2 s              | 23.9 s (+0.4%)               |

The headline is that clearing state per position is **9.5× faster on
the pathological game and costs 3.8% on ordinary ones**. The
transposition-table reuse that the current code preserves across
plies is, measurably, worth almost nothing — each position is a
fresh search regardless — while the state it accumulates cost 100
extra seconds on this game, and three hangs across the 677 of the
run.

The time cap needs a footnote. No position hit the 2 s cap during
that whole-game run: `movetime` changes Stockfish's time management
enough that the explosive trajectory never developed, and 18.2 s is
just 128 positions at the normal rate. Forced into the pathological
state deliberately — prime the engine with plies 1-118, then cap —
the cap does bind, and returns `depth=15`, `Cp(+1872)`: a *third*
answer for the same position, alongside the cold `Mate(+8)` and the
warm `Cp(+2617)`.

## Recommendation

**Clear the search state per position, and bound the call anyway.**

1. **`ucinewgame` before every position** — the primary fix. Pass a
   changing `game` argument to `analyse` so python-chess emits it
   (`game=` is already in the signature; nothing else changes). It
   removes the cause rather than the symptom, costs 3.8% on ordinary
   games, and buys the property the schema already claims: an eval
   becomes a pure function of (position, depth, binary), so a
   re-analysis reproduces its judgments instead of inventing new ones.
   Apply it at **both** call sites — `analyse` in `evaluate` and
   `analysis` in `stream_infos` — or the live board and the coach
   tool keep the defect.
2. **A per-position `asyncio.wait_for` in the pool** — containment,
   independent of cause. Routed into the existing retire/respawn
   path, it turns a 40-minute silent hang into a few seconds and one
   retried game, and it also covers hangs that have nothing to do
   with search state. The path is proven: it recovered cleanly all
   three times, once a human supplied the `kill`.

   This one does **not** generalise for free. A `wait_for` around a
   per-position `await` fits `analyze_game`; the streaming paths are
   long-lived generators by design and need their own deadline shape
   (time-to-first-info, or a total-stream budget), plus a check that
   `aclosing` can actually stop a search whose engine is unresponsive
   — today's cleanup assumes a healthy process. Design it before
   assuming the front-end paths are covered.
3. **A time cap is not needed on top of those two**, and should not
   be used as a substitute for (1). It leaves results dependent on machine speed
   and concurrent load, and when it binds it silently stores a
   shallower eval under a column that says `16`. If it is ever
   adopted, the achieved depth must be recorded per position rather
   than asserted per game.
4. **Observability, cheap and immediate.** A stall watchdog on the
   run (no progress event for N seconds → mark it stalled and publish
   that) plus the in-flight ply on the backfill CLI's live line would
   have made all three hangs self-evident in a minute rather than
   forty.

## Re-analysing the existing rows

The measurements above settle the "grandfather or re-analyse"
question: re-analyse. Two constraints on when and how.

**Not before the fix lands.** A re-run today reproduces the same
class of error with a different roll of the dice — hours of engine
time for data that is wrong in new places. The order is fix, then
re-analyse.

**There is no invalidation mechanism today.** `games_needing_analysis`
selects rows where the analysis is missing *or* shallower than the
configured depth
([games.py:211](../../backend/src/chess_coach/storage/games.py:211)),
so a game already stored at depth 16 will never be picked up again at
depth 16. Forcing a re-run means `DELETE FROM analyses`, or bumping
`engine.depth` (which changes the standard rather than repairing it).
A fix that changes what a stored eval means needs a deliberate way to
mark existing rows stale — an analysis version alongside `depth`, or
an explicit re-analyse flag on the endpoint. That work belongs with
this fix, not after it.

Sizing, for when it happens: 1,202 games are analysed today, and
clearing per position runs a typical game in about 6 s, so a full
re-analysis on two workers is roughly an hour — cheap enough that
targeting only long games is not worth the complexity.

## Contract changes any fix will need

- [04-engine.md](../04-engine.md) documents the exact call
  (`engine.analyse(board, chess.engine.Limit(depth=...))`, line 103)
  and the retirement rule (line 75). Both change.
- [03-storage.md](../03-storage.md): `analyses.depth` currently means
  "every position at this depth". Under a time cap it becomes "at
  most this depth"; under per-position clearing it becomes true as
  written for the first time.
- [01-config.md](../01-config.md): a timeout or per-position cap is a
  new `engine` config key with a default.
- Storage again, and this is new work rather than a wording change:
  there is no way to mark an analysis stale (see "Re-analysing the
  existing rows"). An analysis-version column beside `depth`, or an
  explicit re-analyse flag on `POST /analyze`, has to come with the
  fix or the repaired code has nothing to repair the data with.
- The fix spans engine, storage, config and api, so per the repo's
  delegation rules the contract decisions belong in a main session
  first; only then does each slice go to its component agent.

## What was not measured

Stated so the next person does not mistake absence for evidence:

- **MultiPV.** Live eval and the coach tool run `multipv=5` by
  default; every measurement here is single-PV. Whether a MultiPV
  search triggers the pathology more or less often is unknown.
- **The judgment split.** Of the 24% of judgments that change, the
  share that is harmless best↔good attribution versus a real
  threshold crossing was not separated. The 4% of moves shifting by
  ≥100 cp is the conservative proxy.
- **Hash size.** All runs used Stockfish's 16 MB default. A larger
  table might make the pathology rarer, commoner, or neither; it was
  not varied.
- **Generality of the 3.8%.** The cost of per-position clearing was
  measured on one machine, at depth 16, on four games.

## Diagnosis playbook, until a fix lands

- Both workers near 100% CPU **and** progress events flowing → a slow
  game, not a hang. Wait.
- One worker near 100%, its partner at exactly 0.0%, and no progress
  events for 60 s → hang. `kill` the busy PID; the pool respawns and
  the batch continues one game short.
- `ps -o pid,etime,time,%cpu,command -ax | grep '[s]tockfish'` for the
  first check; for the second, watch the stream:
  `curl -sN --max-time 60 localhost:8000/api/players/<user>/analyze/progress`
