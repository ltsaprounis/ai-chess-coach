# Player profile — the durable artifact other features embed

Status: planned (2026-07), not yet built. Raised as finding 11 of the
coach report review
([coach-report-improvements.md](../archive/coach-report-improvements.md))
and carried as item 06 of the July fixes iteration
([fixes-2026-07/README.md](../archive/fixes-2026-07/README.md)), which
closed without starting it. Its two prerequisites settled on
2026-07-25, so it is unblocked for whichever iteration picks it up.
The spec below is self-contained; the archived docs add only the
original analysis and the numbers behind it.

## What it is and why

Every feature that talks to the student re-derives who the student is
from scratch. The profile computes that once, in two layers.

**Deterministic facts** — `PlayerProfile` in `domain`, computed in
`coach` from stored games: repertoire by color with move sequences
(chosen vs faced), rating trajectory, phase and time-class error
rates, tagged error patterns with counts. Free, always fresh,
recomputed on demand. This is the same aggregation the report already
produces, which is why the profile comes last: build it earlier and
you build it twice.

**An LLM narrative** — three to five sentences of tendencies plus a
short evidence-linked weakness list. Expensive, therefore stored and
regenerated only on explicit user action, in a `player_profiles`
table keyed by username with `generated_at`, window bounds,
`games_covered`, `agent_id`, `prompt_version`, the facts JSON and the
narrative text. That mirrors the `reports` cache shape, including its
staleness signal.

The payoff is `render_profile_context(profile) -> str`: a compact
(~250 token) block other prompts embed at the top. The explain prompt
stops being "explain this move" and becomes "explain this move *to a
player who hangs pieces to back-rank checks and plays the London*".
One block, every future feature, one place to improve it.

Placement: aggregation in `coach` (it is report-shaped), persistence
in `storage`, composition in `api`; `domain` gains `PlayerProfile`.
Keep it inside component 6 rather than opening docs/09 — it is the
report's own output, not a new concern.

## Prerequisites — both met

Do not start before these have landed and settled; both did, on
2026-07-25.

- **The chosen-vs-faced repertoire split** (archived fixes doc 03).
  The facts layer reuses that aggregation; without it the profile
  would restate the misattribution the review's finding 1 fixed.
- **The agentic provider path** (archived fixes doc 04). Narrative
  generation goes through `CoachProvider.complete` with an optional
  analyst, not a second bespoke call path.

## Contract change (main session, before delegating)

- `PlayerProfile` added to `domain.py`, per the placement above.
- Docs in the same commit: [06-coach.md](../06-coach.md) (profile
  functions, `render_profile_context`, and whether the narrative gets
  its own `PROFILE_PROMPT_VERSION` or reuses `PROMPT_VERSION` —
  decide there), [03-storage.md](../03-storage.md)
  (`player_profiles` table + accessors), [07-api.md](../07-api.md)
  (endpoints), [README.md](../README.md) (domain table).

If [prompt-version-fingerprint.md](prompt-version-fingerprint.md)
lands first, the profile's `prompt_version` is a fingerprint from the
start and the question above answers itself.

## Slices, in order

1. **coach-dev** — `build_profile(games) -> PlayerProfile` (facts
   layer, pure), `render_profile_prompt`, `render_profile_context`;
   unit tests over `coach_scenario.py`.
2. **storage-dev** — migration 009 `player_profiles` + get/put,
   mirroring `storage/reports.py`. (001-008 are taken; check the
   directory before writing the file.)
3. **api-dev** — `GET /players/{u}/profile` (cached; facts always
   fresh, narrative from the table) and a POST/refresh path mirroring
   the coach endpoint's cache semantics; wire the profile context
   block into the explain prompt path.
4. **frontend-dev** — after `pnpm gen:api`: a modest surface first
   (a profile card on the Coach page with a Regenerate action); the
   Dashboard "Recurring mistakes" section can follow later.

Acceptance: both gates green; two profile fetches, one provider
invocation; explain prompts carry the context block (snapshot updated
and reviewed).
