# Archive

Closed-out reviews, scans and fix iterations, plus the design records
of features that have since shipped. They are kept for their
measurements and their reasoning — why a thing was built the way it
was, and what the numbers were at the time — not as descriptions of
current behaviour. For that, read the component docs (`docs/0N-*.md`);
for what is still planned, read `docs/future-improvements/`.

Nothing arrives here with loose ends attached: an item still worth
doing is moved to `future-improvements/`, folded into the owning
component doc, or listed on
[NEW-FEATURE-PROPOSAL.md](../NEW-FEATURE-PROPOSAL.md) *before* its
report is archived.

## Reviews, scans and fix iterations

| Report | Subject | Closed |
|--------|---------|--------|
| [prompt-hygiene.md](prompt-hygiene.md) | Audit of the five coach templates as they render: seven defects, all fixed, plus the units pass it triggered | 2026-07-31 |
| [engine-search-hangs.md](engine-search-hangs.md) | Fixed-depth searches with no cost bound hung the engine workers; fix plus the full re-analysis it forced | 2026-07-28 |
| [coach-report-improvements.md](coach-report-improvements.md) | Review of the whole-report coaching output and the Dashboard views on the same data, and the rework it planned | 2026-07-28 |
| [fixes-2026-07/](fixes-2026-07/README.md) | The follow-up fix iteration to that rework — seven items, waves 1-7 | 2026-07-27 |
| [codebase-scan-2026-07.md](codebase-scan-2026-07.md) | Whole-codebase scan: 14 findings, 12 fixed one commit at a time | 2026-07-27 |

## Design records of shipped features

Each of these was a `future-improvements/` design that got built. The
contracts migrated into the component docs, which are authoritative.
What stays here is the reasoning: the alternatives weighed, the
premises checked, and (for the profile) what a real archive broke on
first contact.

| Design | Shipped | Contracts now live in |
|--------|---------|-----------------------|
| [coach-game-search.md](coach-game-search.md) | 2026-08-03, designed and built the same day | 03, 06, 07 + `domain` |
| [player-profile.md](player-profile.md) | 2026-07-30, reworked the same day | 03, 06, 07, 08 + `domain` |
| [coach-chat.md](coach-chat.md) | 2026-07-30 | 03, 06, 07, 08 + `domain` |
| [openings-explorer.md](openings-explorer.md) | 2026-07-29 | 03, 05, 07, 08 + `domain` |

## What each one left behind

- **Prompt hygiene** — nothing. All seven findings were fixed in one
  pass, and the two rules it left optional were taken with them. The
  standing statements it produced live in
  [06-coach.md](../06-coach.md): "Units" (one scale, one name — the
  finding-1 defect generalized across the whole system), "One register
  per document", "One persona, three artifacts", and the seed carve-out
  under "Chat". The guards are
  `test_no_template_says_acpl_anywhere`,
  `test_render_prompt_data_describes_the_student_in_one_register` and
  `test_system_prompt_names_no_one_artifact`. Its own method — read the
  rendered snapshots in `backend/tests/testdata/`, not the templates —
  is the part worth repeating: two of the seven were invisible in the
  source.
- **Engine search hangs** — `analysis_version` on stored analyses, so
  a future engine-behaviour change can re-queue affected rows the
  same way. The addendum records the re-analysis results and the one
  default it corrected.
- **Coach report improvements** — the repertoire semantics that
  finding 1 turns on now live in [06-coach.md](../06-coach.md) and are
  implemented twice against that statement, with
  `tests/test_repertoire_agreement.py` keeping the two producers
  honest. Its finding 11 became
  [player-profile.md](player-profile.md).
- **Fixes 2026-07** — the standing guard rails its README lists
  (repertoire agreement, the 19-game coach scenario, the prompt
  snapshot, the shared phase rule) are all still live tests. Do not
  weaken them because the iteration is closed.
- **Codebase scan** — two findings outlived it. Finding 12 became
  [prompt-version-fingerprint.md](../future-improvements/prompt-version-fingerprint.md);
  finding 14 (default engine/book paths assume a source checkout) is
  parked behind the GPL distribution question that `pyproject.toml`
  already notes. Finding 1's rejected alternative is recorded in
  [normalized-game-model.md](../future-improvements/normalized-game-model.md).
- **Coach game search**: three follow-ups, all carried as one
  backlog entry under "Housekeeping worth scheduling" in
  [NEW-FEATURE-PROPOSAL.md](../NEW-FEATURE-PROPOSAL.md): the
  bounded "analyze this one game from chat" affordance for verifying
  finds in unanalyzed games, the scan-result cache whose go/no-go is
  fed by the wall-time log the API layer emits per scan, and a watch
  on `_CHAT_MAX_TURNS` (8) now that a search turn spends 3-4 calls.
  Its [spike report](../spike-reports/coach-game-search-events.md)
  stays in spike-reports as live evidence for the cache decision.
- **Player profile**: nothing. Every surface it specified is built
  and re-stated in 03/06/07/08, including the same-day rework that
  re-keyed the profile per time control and split volume from
  quality. Its "Prerequisites" section is the part worth re-reading
  before the next report-shaped feature: the profile came last on
  purpose, because building it before the report's aggregation
  existed would have built that aggregation twice.
- **Coach chat**: its "Open questions and risks" section, which the
  2026-07-30 boundary review added to. Resume fidelity was never
  measured against a live provider, so how often the replay path
  actually runs is still unknown; the ~40-message cap is a guess; and
  three small gaps were accepted knowingly (report-seed latency, the
  mid-stream scope switch, the never-started stream leak). All of it
  is now carried as one backlog entry under "Housekeeping worth
  scheduling" in
  [NEW-FEATURE-PROPOSAL.md](../NEW-FEATURE-PROPOSAL.md).
- **Openings explorer**: its four "Deferred (recorded, not built)"
  items, namely the punish metric, transposition annotation, per-node
  drill to the Games page, and an LLM "explain this line". They are
  on the backlog as tier-2 item 10. Its "Synergy" note still stands:
  the tree computes the true book-exit position that backlog item 5
  needs.

## Paths that moved

In-repo references are rewritten with each move, but older sessions
and anything linking in from outside will still name the original
locations. Archived 2026-07-29 unless noted:

| Was | Is |
|-----|-----|
| `docs/COACH-REPORT-IMPROVEMENTS.md` | `docs/archive/coach-report-improvements.md` |
| `docs/CODEBASE-SCAN-2026-07.md` | `docs/archive/codebase-scan-2026-07.md` |
| `docs/fixes-2026-07/` | `docs/archive/fixes-2026-07/` |
| `docs/fixes-2026-07/06-player-profile.md` | `docs/archive/player-profile.md` |
| `docs/future-improvements/prompt-hygiene.md` | `docs/archive/prompt-hygiene.md` |
| `docs/future-improvements/player-profile.md` | `docs/archive/player-profile.md` (2026-08-03) |
| `docs/future-improvements/coach-chat.md` | `docs/archive/coach-chat.md` (2026-08-03) |
| `docs/future-improvements/openings-explorer.md` | `docs/archive/openings-explorer.md` (2026-08-03) |
