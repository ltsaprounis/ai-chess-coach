# Automatic prompt versions — content fingerprints

Status: planned (2026-07-27), not yet scheduled. Bundles scan finding
12 ([codebase-scan-2026-07.md](../archive/codebase-scan-2026-07.md),
"Explanation cache ignores `PROMPT_VERSION`") because both items are
the same question — *what identifies the prompt a cached LLM answer
was produced from* — and fixing one without the other means touching
the same three components twice.

## The ask

`coach.PROMPT_VERSION` is a hand-written string
(`"2026-07-fen-coverage"`, `coach/prompt.py:27`) that a human must
remember to bump whenever the template changes. Replace it with a
version derived automatically from the prompt's own content, so the
cache key cannot drift from the template it names.

At the same time, give the explanation cache a version of its own:
`explanations` keys on `(game_id, ply, agent_id)` only
(`storage/explanations.py`), so a reworked explain prompt keeps
serving explanations generated from a template that no longer exists.

## Why the manual bump is the wrong shape

- **It is a promise, not a mechanism.** Nothing fails when the
  template changes and the string does not; the failure is silent and
  arrives later as stale advice attributed to the current prompt.
- **It has already needed choreography.** `archive/fixes-2026-07/README.md`
  sequences waves 2, 3 and 5 partly so `PROMPT_VERSION` "churns once
  per wave, not per keystroke" — a scheduling constraint that exists
  only because the bump is manual.
- **Its asymmetry is the bug in finding 12.** The report cache keys on
  the version; the explanation cache has no version to key on. One
  automatic mechanism removes the asymmetry instead of documenting it.

## What a prompt version has to mean

The version identifies **the model-visible input a cached answer was
produced from**. Two runs may share a cache row only if they would
have sent the same template. That gives three properties, in priority
order:

1. **No false negatives.** If the template changed, the version must
   change. A miss costs one extra LLM call; a false hit serves wrong
   advice indefinitely. Correctness first — and house policy already
   bounds the damage: every LLM call is user-triggered *and* cached
   ([03-storage.md](../03-storage.md)), so an extra miss costs one
   call behind an explicit user action, not a background bill.
2. **Determinism.** Same source, same version, on any machine, on any
   supported Python — the version is stored in SQLite and compared
   across processes and upgrades.
3. **Few false positives.** A comment fix should ideally not re-bill
   the most expensive call the app makes. Ideally — not at any cost to
   property 1.

Non-goal: engine settings. `render_explain_prompt` embeds MultiPV
lines produced at `engine.depth`, so changing that config changes the
model's input without changing the template. That is a config-keyed
cache question, deliberately out of scope here; today's behaviour
(depth changes do not invalidate) is unchanged.

## Recommended design

### 1. One fingerprint helper

`chess_coach/coach/fingerprint.py`, ~15 lines, no reflection:

```python
"""Content fingerprints for the prompt templates (docs/06-coach.md)."""

import hashlib
from pathlib import Path

# Scheme tag: stored rows carry it, so a future change of what is
# hashed is self-describing rather than silently colliding.
_SCHEME = "h1"
_DIGEST_CHARS = 12


def source_fingerprint(*paths: str | None) -> str:
    """Hash the source of the modules that define one prompt.

    Order matters and is fixed by the call site. `None` means the
    module has no source file (a frozen or namespace import) — the
    version would be unknowable, so fail loudly instead of emitting a
    fingerprint that silently collides with every other build.
    """
    digest = hashlib.sha256()
    for path in paths:
        if path is None:
            raise RuntimeError("prompt module has no source file")
        digest.update(Path(path).read_bytes())
    return f"{_SCHEME}-{digest.hexdigest()[:_DIGEST_CHARS]}"
```

Source text, not bytecode or AST: it is interpreter-independent
(property 2 — a bytecode hash would change on a Python upgrade and
invalidate every cached report for nothing) and it cannot miss a
change (property 1).

### 2. Split the prompt module so each version is precise

`coach/prompt.py` currently holds both templates, so a single
file-level fingerprint would make an explain-instruction edit
invalidate every cached *report* — the expensive direction. Split it
(pure refactor, no behaviour change):

| New module          | Holds                                          |
|---------------------|------------------------------------------------|
| `prompt_format.py`  | `SYSTEM_PROMPT`, `format_eval`, `format_cp_loss`, `_plural`, `_pawns_or_na`, `_format_date`, `_MATE_SCALE` |
| `prompt_report.py`  | `render_prompt` + its sections, `_INSTRUCTIONS`, `PROMPT_VERSION` |
| `prompt_explain.py` | `render_explain_prompt`, `_EXPLAIN_INSTRUCTIONS`, `EXPLAIN_PROMPT_VERSION` |

Each version hashes its own module plus the shared one:

```python
# coach/prompt_report.py
PROMPT_VERSION = source_fingerprint(__file__, prompt_format.__file__)

# coach/prompt_explain.py
EXPLAIN_PROMPT_VERSION = source_fingerprint(__file__, prompt_format.__file__)
```

`SYSTEM_PROMPT` sits in the shared module on purpose: it is sent with
both the report and the explain run (`providers.py:530-532`), so it
belongs to both fingerprints.

The public surface is unchanged — `coach/__init__.py` re-exports the
same names (plus `EXPLAIN_PROMPT_VERSION`), and `coach.prompt`
disappears as an internal path. Two importers need the new path:
`coach/__init__.py` and `coach/providers.py:31`.

### 3. What the fingerprint covers, and one gray area

In scope: everything in the two prompt modules and the shared
formatter — templates, instruction blocks, the system prompt, and the
section builders that assemble them.

Out of scope: provider plumbing (SDK options, `max_turns`, retry
logic). Those change the run, not the template, and dragging
`providers.py` into the hash would invalidate on every unrelated
provider edit.

The gray area is that `providers.py` holds two model-visible strings:
`_ANALYZE_TOOL_DESCRIPTION` (the tool's description, which the model
reads) and `_render_lines` (`providers.py:511-519`, how engine results
are rendered back into the conversation). Both shape the model's input
during agentic runs but live outside the fingerprint. **Recommended:
move both into `prompt_format.py`** as part of the split — they are
prompt text wearing provider clothes, and the move brings them under
both fingerprints for free.

### 4. Finding 12: the explanation cache gains the version

Storage's explanation functions take the version as part of the key:

```python
def get_explanation(db: Db, game_id: str, ply: int, agent_id: str,
                    *, prompt_version: str) -> str | None
def save_explanation(db: Db, game_id: str, ply: int, agent_id: str,
                     text: str, *, prompt_version: str) -> None
```

Keyword-only, matching how `ReportKey` makes every keyed field
explicit without introducing a second key model for four fields.

Migration `007_explanation_prompt_version.sql`: SQLite cannot alter a
primary key, so the table is rebuilt (the same shape as `006`, which
already rewrites child tables under `PRAGMA foreign_keys=OFF`):

```sql
PRAGMA foreign_keys=OFF;
BEGIN;
CREATE TABLE explanations_new (
    game_id TEXT NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    ply INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,   -- coach.EXPLAIN_PROMPT_VERSION
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (game_id, ply, agent_id, prompt_version)
);
DROP TABLE explanations;
ALTER TABLE explanations_new RENAME TO explanations;
COMMIT;
PRAGMA foreign_keys=ON;
```

Pre-versioning rows are **not** carried over. Storage never imports
coach, so a static SQL file cannot know which template produced them —
and serving them under the current version is exactly the bug being
fixed. The cost is one paid regeneration per explanation the user
re-opens; explanations are per-move and generated on demand, so the
corpus is small. (Alternative, if that cost turns out to matter:
copy rows with `prompt_version = ''`, which never matches a real
fingerprint, and have the API layer — the one place that sees both
components — backfill them at startup. More moving parts for a cache
of on-demand text; not recommended.)

The API layer passes `EXPLAIN_PROMPT_VERSION` at both call sites
(`routes.py:453` and `routes.py:509`). No HTTP surface change, so no
`pnpm gen:api` and no frontend work.

Existing `reports` rows keyed `"2026-07-fen-coverage"` stop matching
once `PROMPT_VERSION` becomes a fingerprint — the same one-time
invalidation any manual bump caused, needing no migration. They can be
left to age out or deleted in the same migration; leaving them costs a
few KB.

## Rejected alternatives

- **Hash the rendered output of a canonical fixture.** Semantically
  perfect (the version moves exactly when the output moves) and it
  reuses the existing snapshot idea. Rejected on property 1: a
  template branch the fixture does not exercise changes nothing, and
  keeping the fixture branch-complete is the same manual promise the
  bump already is — only now silent when broken.
- **Walk `__code__.co_consts` from each entry point.** Precise
  (comments excluded, helpers discovered automatically) but it is
  exactly the "decorator magic"/reflection GUIDELINES.md bans, and
  bytecode-derived hashes move with the interpreter.
- **`git rev-parse` the prompt files.** Requires git at runtime and
  produces no version for a working-tree edit.
- **Keep the manual string, add a test that fails when the file
  changes without a bump.** Pins the version to a source hash anyway,
  but leaves a human in the loop for no benefit beyond a readable
  name — and the name is recoverable from git history regardless.

## Plan

Contract-first, per `.claude/CLAUDE.md`: the docs and interface
changes land in the main session, component slices are delegated.

1. **Docs and contract (main session).** Update
   [06-coach.md](../06-coach.md) (`PROMPT_VERSION` is derived, not
   bumped; add `EXPLAIN_PROMPT_VERSION`; note the module split),
   [03-storage.md](../03-storage.md) (explanations table + signatures)
   and [07-api.md](../07-api.md) (explain cache key). Same commit as
   the interface change per the hard rules.
2. **Split the prompt module (coach-dev).** Pure refactor. Acceptance:
   `testdata/coach_prompt.md` byte-identical, all gates green, no
   change to `coach/__init__.py`'s exported names.
3. **Fingerprints (coach-dev).** Add `fingerprint.py`, derive both
   versions, export `EXPLAIN_PROMPT_VERSION`. Acceptance: the pinned
   versions fixture below exists and the value is stable across two
   interpreter runs.
4. **Storage key (storage-dev) + API wiring (api-dev), one commit.**
   Migration 007, the two signatures, both call sites. These cannot
   land separately — the signature change breaks the API layer.
5. **Close out (main session).** Mark finding 12 fixed in
   [codebase-scan-2026-07.md](../archive/codebase-scan-2026-07.md); note in
   `archive/fixes-2026-07/README.md` that the bump-coordination constraint no
   longer applies.
6. **boundary-reviewer** before committing, as usual for multi-agent
   work.

Estimate: half a day, most of it in step 2 (a 718-line module split
three ways) and step 4's tests.

## Tests

- **Pinned versions fixture.** `testdata/prompt_versions.txt` holds
  both fingerprints, regenerated with `UPDATE_SNAPSHOTS=1` exactly
  like the prompt snapshot. This is the review artifact: a version
  moving shows up as a diff line next to the template diff, so an
  invalidation caused by a comment-only edit is visible before it is
  paid for.
- **Determinism.** Both versions match `h1-[0-9a-f]{12}` and are equal
  across two calls; the two versions differ from each other.
- **Shared-module coupling.** Editing `prompt_format.py` moves both
  versions — asserted by construction in the fixture, since both
  fingerprints include it.
- **Storage (storage-dev).** A saved explanation is invisible under a
  different `prompt_version`; two versions coexist for the same
  `(game_id, ply, agent_id)`; migration 007 applies to a `006`-era DB.
- **API (api-dev).** The explain endpoint regenerates rather than
  serving a cached row written under a different version; the existing
  cached-path and `refresh` tests still pass.

## Risks and trade-offs

- **Source must be readable at runtime.** `Path(__file__).read_bytes()`
  works for a source checkout and a normal wheel install, not for a
  zipimport/frozen build. Same class of packaging assumption as scan
  finding 14; the helper raises rather than silently degrading.
- **False positives are now automatic.** A comment fix or `ruff
  format` churn in a prompt module invalidates that prompt's cache.
  Accepted deliberately (property 1 over property 3); the pinned
  fixture makes each one visible in review, where it can be squashed
  into the same commit as a real template change.
- **Versions stop being human-readable.** `h1-9f2c…` names no
  iteration. Mitigated by the pinned fixture — `git log -S` on the
  version string finds the commit that introduced it — and by the
  scheme tag, which keeps old rows interpretable if the scheme
  changes.
- **One-time cache loss** on both tables, as described above.

## Decisions to confirm before starting

1. Split `prompt.py` three ways (recommended — keeps an explain edit
   from invalidating the expensive report cache), or accept one shared
   fingerprint for both prompts and skip step 2 entirely?
2. Drop pre-versioning explanation rows (recommended) or preserve them
   under `''` with an API-layer backfill?
3. Move `_ANALYZE_TOOL_DESCRIPTION` and `_render_lines` out of
   `providers.py` into `prompt_format.py` so model-visible text is
   covered by the fingerprints (recommended)?
