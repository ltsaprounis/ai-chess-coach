# 03 — Chosen vs faced: the "What you face" split

**Status: shipped 2026-07-25 in `d2795a1` (wave 2); drill-through
verified 229/229 families on the real DB. The revisit clause is
live: on real data, faced-family labels are coarse (anti-Pirc
systems label as "Pirc Defense") — revisit if it confuses.**

## Symptom

The rework split the repertoire by color and added the `system` /
`first_moves` columns, which makes chosen-vs-faced *legible* — but
the table still interleaves lines the player chose with lines their
opponents chose against them, and the boundary review called the
missing split the headline remaining gap
(coach-report-improvements.md §1: "systems you chose" vs "replies
you face"). If coaching advice still misattributes an opening, this
is the missing structure.

## Decision: derive `faced` from `Opening.ply` parity, by majority

Per game, the classification `Opening` carries `ply` — the 1-based
ply of the book move that fixed the name. White moves are odd
plies, Black moves even. If the naming ply belongs to the
*opponent*, the name describes their choice:

- player is white → opponent-named iff `opening.ply` is even
- player is black → opponent-named iff `opening.ply` is odd

Example: the Englund is named by 1...e5 (ply 2); a White player's
Englund rows are therefore faced. The Pirc is named by 1...d6
(ply 2); a Black player's Pirc rows are chosen.

Per row (one `(color, eco, name)` group), transpositions can reach
the same name at different plies, so the flag is a strict majority
over the group's games:

> `faced` is true iff opponent-named games × 2 > total games in the
> group. Ties are chosen.

This rule is implemented twice — storage's SQL over classified
games and the coach's Python over analyzed games — against this one
statement, exactly like the rest of the repertoire semantics. The
agreement test keeps them honest.

## Contract change (main session, before delegating)

- `OpeningStats` (`domain.py`) gains `faced: bool` with a docstring
  stating the parity-majority rule above.
- `docs/06-coach.md` "Repertoire" section gains the rule, plus the
  rendering and rollup rules below. `docs/03-storage.md` points at
  it. Update `docs/08-frontend.md` for the Dashboard split.
- Rollup rule (pinned here, stated in 06): partition rows by
  `faced` *before* rolling up. The chosen partition rolls up by
  (color, system) exactly as today. The faced partition rolls up by
  (color, name root) — the name up to the first colon — because for
  faced lines the name *is* the opponent's choice, while the
  player's own system varies with their replies and would split one
  gambit across families. Records sum and both ACPL columns stay
  move-weighted in both partitions. The 5+ sample floor applies per
  partition; below-floor rows from both partitions fold into the
  color section's single existing long-tail line.

## Slices

### storage-dev

- `opening_stats` (`storage/games.py`) computes `faced` in SQL over
  the group: the parity predicate per game
  (`(g.color = 'white') = (g.opening_ply % 2 = 0)`), summed and
  compared 2×sum > count.
- Extend `tests/test_storage.py` with an Englund-as-White case
  (faced) and a mixed-parity group exercising the majority rule.

### coach-dev (after storage-dev lands, so the agreement test can
run against both)

- `_opening_stats` (`coach/report.py`) computes the same flag from
  `AnalyzedGame.opening.ply`.
- `coach/prompt.py`: within each color section, two sub-tables —
  "Systems you chose" (chosen partition, keyed as today) and "What
  you face as White/Black" (faced partition, keyed by name root,
  rendered with `first_moves` so the reply is visible). Apply the
  rollup rule pinned above. Bump `PROMPT_VERSION`.
- Regenerate the snapshot
  (`UPDATE_SNAPSHOTS=1 uv run pytest tests/test_coach.py -k snapshot`)
  and read the diff: `coach_scenario.py` already contains an
  Englund faced as White above the sample floor, so the faced table
  must appear populated, and the Englund must vanish from the
  chosen table. That diff is the acceptance artifact.
- Extend `tests/test_repertoire_agreement.py` to cover `faced`,
  including the transposition/majority case. Do not weaken the
  scenario fixture.

### Fold-in: the drill-through undercount (pre-existing)

A family row can report 8 games while clicking through shows 4. The
family's `games` sums all member (color, eco, name) rows,
transpositions included, but `Games.tsx` matched
`playerSystem(game.first_plies, game.color) === system` — an exact
match against the family's *representative* line, which transposed
games fail. Documented semantics (06-coach.md: a row's `system` is
the commonest way there, not the only one), but the UI disagreeing
with its own numbers is a defect, and this slice reworks exactly
that drill-through — so it is fixed here.

Pinned mechanism: **drill through by the family's member (eco, name)
list.** Per color there is exactly one `OpeningStats` row per
(eco, name) and each row rolls into exactly one family, so filtering
the Games page by "the game's classified opening is in the member
list, same color" reproduces the family's count by construction, in
both partitions. The list is frozen into the link at click time, so
the drill-through shows exactly the games the row counted (rather
than recomputing membership under a possibly different scope).

### frontend-dev (after `pnpm gen:api`)

- `web/src/openings.ts`: `groupByFamily` partitions by `faced`
  first, applying the pinned rollup keys (chosen: color+system;
  faced: color+name root), and each `OpeningFamily` carries its
  member `(eco, name)` pairs. `openings.test.ts` gains cases keeping
  the two partitions apart and covering the member lists.
- `web/src/components/RepertoireTable.tsx` (and the Dashboard
  section that hosts it): render the two groups under headings
  mirroring the prompt — repertoire first, "What you face" second.
- Drill-through, both partitions: the link carries `family` (chip
  label), `color`, and one `opening=ECO|name` param per member row
  (`|` is safe — ECO codes never contain it). Faced rows also carry
  a display-only `faced=true` so the filter chip can read "faced as
  white" rather than "as white". `Games.tsx` matches
  the game's classified opening against the pairs, same color.
  Fallbacks for older or hand-typed links, in order: a `system`
  param keeps the exact (color, system) match (`playerSystem` and
  `GameSummary.first_plies` stay live for this); a bare `family`
  falls back to the name-root match, which must now respect `color`
  when present — today it ignores it.

Acceptance: both gates green; agreement test covers `faced`; the
snapshot diff shows the Englund under "What you face as White"; a
family row's game count equals the row count its drill-through
shows (transpositions included).

## Out of scope

An opponent-system column (grouping faced lines by the opponent's
own three moves). The name-root key plus `first_moves` covers the
coaching need; revisit only if faced families prove too coarse.
