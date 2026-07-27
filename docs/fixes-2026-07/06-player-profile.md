# 06 — Player profile (finding 11) — parked for a later iteration

**Status: not started, by decision. Its prerequisites (docs 03 and
04) both settled on 2026-07-25, so it is unblocked for whichever
iteration picks it up.**

## What and why

The durable artifact other features embed:
COACH-REPORT-IMPROVEMENTS.md §11 is the spec and stays canonical;
this doc adds only the sequencing and slice boundaries. Two layers:

- **Deterministic facts** — `PlayerProfile` in `domain`, computed
  in `coach` from stored games: repertoire by color with move
  sequences (chosen vs faced, from doc 03), rating trajectory,
  phase and time-class error rates, tagged error-pattern counts.
  Free, recomputed on demand.
- **LLM narrative** — a few sentences of tendencies plus an
  evidence-linked weakness list. Expensive, therefore stored in a
  `player_profiles` table and regenerated only on explicit user
  action, mirroring the `reports` cache shape (keyed by username,
  with `generated_at`, window bounds, `games_covered`, `agent_id`,
  `prompt_version`, the facts JSON, the narrative text).

The payoff is `render_profile_context(profile) -> str`: a compact
(~250 token) block other prompts embed at the top — explain stops
being "explain this move" and becomes "explain this move to a
player who hangs pieces to back-rank checks and plays the London".

## Hard prerequisite

Do not start before docs 03 and 04 have landed and settled. The
deterministic facts are the same aggregation the report produces —
build the profile earlier and it gets built twice — and the
narrative generation should go through the post-04 provider path.

## Contract change (main session, before delegating)

- `PlayerProfile` added to `domain.py`. Placement per the spec:
  aggregation in `coach`, persistence in `storage`, composition in
  `api` — inside component 6, not a new docs/09 component.
- Docs in the same commit: `docs/06-coach.md` (profile functions,
  `render_profile_context`, its own `PROFILE_PROMPT_VERSION` or
  reuse — decide there), `docs/03-storage.md` (`player_profiles`
  table + accessors), `docs/07-api.md` (endpoints), `docs/README.md`
  (domain table).

## Slices, in order

1. **coach-dev** — `build_profile(games) -> PlayerProfile` (facts
   layer, pure), `render_profile_prompt`, `render_profile_context`;
   unit tests over `coach_scenario.py`.
2. **storage-dev** — migration 006 `player_profiles` + get/put,
   mirroring `storage/reports.py`.
3. **api-dev** — `GET /players/{u}/profile` (cached; facts always
   fresh, narrative from the table) and a POST/refresh path
   mirroring the coach endpoint's cache semantics; wire the profile
   context block into the explain prompt path.
4. **frontend-dev** — after `pnpm gen:api`: a modest surface first
   (a profile card on the Coach page with a Regenerate action);
   the Dashboard "Recurring mistakes" section can follow later.

Acceptance: both gates green; two profile fetches, one provider
invocation; explain prompts carry the context block (snapshot
updated and reviewed).
