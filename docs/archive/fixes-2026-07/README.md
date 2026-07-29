# Fixes iteration — July 2026

Status: closed 2026-07-27, archived 2026-07-29. Items 1-5 and 7 all
shipped (waves 1-7, `f90f97e` through the Coach-page warning); item 6
was never started and moved on to
[player-profile.md](../../future-improvements/player-profile.md).
Kept for its decisions and measurements. Original text follows, with
only the item-6 row and cross-doc links updated for the move.

The follow-up iteration to the coach report rework
([coach-report-improvements.md](../coach-report-improvements.md)).
That rework shipped findings 1-8, 10 and 12; this iteration closes
what it left open, plus the games-list cap bug that predates it.

This README is the orchestrator's map: the overall picture, the
decisions already made, and which doc each sub-agent reads. A
sub-agent should need only its feature doc here plus its own
component doc (docs/0N-\*.md) — everything else is pinned in the
feature doc so implementers don't guess.

## Work items

| # | Doc | What | Components (agents) | Pri | Status |
|---|-----|------|---------------------|-----|--------|
| 1 | [01-games-list-uncap.md](01-games-list-uncap.md) | Slim `GameSummary`, remove the 2000-game cap | domain (main), storage-dev, api-dev, frontend-dev | P0 | shipped `f90f97e` |
| 2 | [02-termination-backfill.md](02-termination-backfill.md) | Full re-sync path so `Game.termination` backfills | api-dev, frontend-dev | P1 | shipped `f90f97e`; backfill run and verified (8,149/8,149 non-NULL) |
| 3 | [03-faced-openings.md](03-faced-openings.md) | Chosen-vs-faced split in the repertoire | domain (main), storage-dev, coach-dev, frontend-dev | P1 | shipped `d2795a1`; label-coarseness revisit clause open |
| 4 | [04-report-engine-tool.md](04-report-engine-tool.md) | Engine tool for the report path (finding 9) | coach-dev, api-dev | P2 | shipped `8c340d4`; live run 2026-07-27 exposed the FEN gap → wave-5 follow-up |
| 5 | [05-drop-max-tokens.md](05-drop-max-tokens.md) | Remove the inert `LlmConfig.max_tokens` | main session only | P2 | shipped `f90f97e` |
| 6 | [player-profile.md](../../future-improvements/player-profile.md) | `PlayerProfile` + narrative (finding 11) | domain (main), coach-dev, storage-dev, api-dev, frontend-dev | P3 | never started; moved to `future-improvements/` on archival |
| 7 | [07-analysis-coverage.md](07-analysis-coverage.md) | State analysis coverage; make backfill aimable | domain (main), storage-dev, coach-dev, api-dev, frontend-dev | P1 | complete: waves 5-7 (`c2f9f2c`, `0c836f0`, Coach-page warning 2026-07-27) |

## Decisions already made

Do not re-litigate these in sub-agent sessions; the rationale lives
in each feature doc.

- **The games-list cap is removed, not raised or made per-class.**
  A per-time-class cap still truncates this player's own blitz
  archive (4,324 games lifetime), so the Dashboard would still
  disagree with `/report` — the exact reported bug. Removal is paid
  for by slimming the list row (~2.4 MB uncapped vs ~5.9 MB capped
  today). See doc 01.
- **`GameSummary` stops extending `Game`.** It becomes a standalone
  list-row model without `pgn`/`san_moves`, carrying `first_plies`
  (the first 6 SAN plies) for the repertoire drill-through.
- **Termination backfill needs a full re-sync, not a normal sync.**
  Sync is incremental and never re-fetches stored games; the
  handover's "one sync per player" was wrong. Doc 02 adds
  `?full=true`.
- **Chosen-vs-faced is derived from `Opening.ply` parity**, decided
  by majority across the row's games. Doc 03 pins the exact rule
  once; both producers implement it against that statement.
- **`CoachProvider.complete` gains an optional analyst.** The report
  becomes agentic with a small turn budget; single-turn remains the
  fallback when no engine pool exists. Doc 04.
- **`max_tokens` is dropped, not wired.** Neither shipped provider
  can honor it; it returns as a provider-specific setting if the
  `anthropic` provider lands. Doc 05.

## Sequencing

Contract changes (domain types, component surfaces, HTTP API) are
made in the main session first — `domain.py` and the affected
component docs in the same commit — then component slices are
delegated. That is the standing rule from `.claude/CLAUDE.md`; last
iteration every serious defect appeared in the seams, not the
slices.

- **Wave 1 (parallel; shipped `f90f97e`):** 01, 02, 05. Disjoint
  backend surfaces. The frontend slices of 01 and 02 both touch
  `Games.tsx`, so run them sequentially through frontend-dev (01
  first).
- **Wave 2 (shipped `d2795a1`):** 03. Touches `storage/games.py`,
  `coach/report.py`, `coach/prompt.py`, `openings.ts` — keep it off
  Wave 1's files.
- **Wave 3 (shipped `8c340d4`):** 04. Edits `coach/prompt.py` and
  the snapshot again; after 03 so `PROMPT_VERSION` and the snapshot
  churn once per wave, not per keystroke.
- **Wave 4 (never run):** 06. Reuses 03's aggregation and 04's
  provider path; both settled on 2026-07-25, so the work is
  unblocked for whichever iteration picks it up — it just isn't this
  one. The design moved to `future-improvements/` on archival.
- **Wave 5 (shipped `c2f9f2c`):** 04's live-run follow-up
  (turning-point FENs, affirmative verification rule) + 07's
  report/prompt slice (coverage statement). One wave because both
  edit `coach/prompt.py` and the snapshot: one `PROMPT_VERSION`
  bump ("2026-07-fen-coverage"), one readable diff.
- **Wave 6 (shipped `0c836f0`):** 07's analyze-endpoint window
  filters (storage-dev → api-dev) + the backfill CLI (main
  session — an HTTP-only client, owned by no component).
- **Wave 7 (shipped 2026-07-27):** 07's Coach-page coverage warning
  + "Analyze the rest" chaining (frontend-dev). Closes item 7 and,
  with 06 handed on, the iteration.

`PROMPT_VERSION` bumps in waves 2, 3 and 5; each bump invalidates
the report cache by design. That is expected, not a regression.

## Standing guard rails (do not weaken)

- `backend/tests/test_repertoire_agreement.py` — storage's SQL
  producer and the coach's Python producer must emit identical
  `OpeningStats`. Doc 03 extends it; nothing shrinks it.
- `backend/tests/coach_scenario.py` — the 19-game fixture is built
  so each past failure mode stays visible. Extend it; never
  simplify it.
- `backend/tests/testdata/coach_prompt.md` — the prompt snapshot is
  the human review artifact. Regenerate with
  `UPDATE_SNAPSHOTS=1 uv run pytest tests/test_coach.py -k snapshot`
  and *read the diff* before accepting it.
- `test_engine_analysis.py::test_phase_rule_matches_coach` — keeps
  the engine and coach phase rules identical.

## Verification gates

Every wave ends green on both gates before the next starts:

- `make check` — ruff, pyright strict, import-linter, pytest.
- `cd web && pnpm biome check && pnpm typecheck && pnpm test &&
  pnpm build`.

After the last wave, run `boundary-reviewer` over the whole diff.
Last iteration it caught every cross-component defect the gates
missed; treat its findings as blocking.
