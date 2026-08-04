# Engineering Guidelines

How we build this codebase: clean component boundaries, readable
code, and a modern, fast toolchain. The backend is Python; TypeScript
exists only in `web/`. These rules apply across every component doc;
where a component doc and this file conflict, this file wins.

## Toolchain

Fast, strict, and few in number.

Dependency policy, both stacks: direct dependencies are pinned
exactly in the manifests (`backend/pyproject.toml`,
`web/package.json`); lockfiles are per-machine and gitignored, so
each install resolves transitives fresh. Do not commit a lockfile.

### Backend (Python 3.12+, everything outside `web/`)

| Concern         | Tool             | Notes                         |
|-----------------|------------------|-------------------------------|
| Env + packages  | uv               | exact pins; lockfile ignored  |
| Lint + format   | ruff             | lint and format, no Black     |
| Type checking   | pyright (strict) | CI gate, not editor-only      |
| Tests           | pytest           | fixtures over mocks           |
| Import rules    | import-linter    | boundaries enforced in CI     |
| Schemas         | pydantic v2      | domain types, config, API I/O |
| Git hooks       | pre-commit       | ruff + pyright fast path      |

Python version pinned in `.python-version`. pyright runs in strict
mode; `Any` in a public signature is a review blocker — type
everything that crosses a component boundary.

### Frontend (`web/` only)

| Concern       | Tool            | Notes                          |
|---------------|-----------------|--------------------------------|
| Runtime       | Node 22 LTS     | pinned: `.nvmrc` + engines     |
| Packages      | pnpm            | exact pins; lockfile ignored   |
| Lint + format | Biome           | one tool, no ESLint/Prettier   |
| Types         | `tsc --strict`  | plus `noUncheckedIndexedAccess`|
| Tests         | Vitest          |                                |
| API types     | openapi-typescript | generated from the backend |

Frontend API types are generated from FastAPI's OpenAPI schema
(`pnpm gen:api`); hand-writing a response type is a review blocker.
Sole exemption: SSE event payloads never appear in the OpenAPI
schema, so their types are hand-declared, each with a comment naming
the backend model it mirrors.

## Architectural boundaries

The dependency rules from [README.md](README.md), enforced
mechanically rather than by convention:

1. Components are subpackages of `chess_coach`; each exposes its
   public surface through its `__init__.py` only. Importing another
   component's internals (`chess_coach.storage.db`) is forbidden.
2. Only the composition root (`chess_coach.api`,
   [07-api.md](07-api.md)) may import other components.
   Components import `chess_coach.domain` and stdlib/their own deps —
   never each other.
3. `web/` never imports or shares code with the backend; it compiles
   against generated OpenAPI types only.
4. import-linter encodes rules 1-2 (layers + independence contracts);
   CI fails on violations. Rule 3 holds structurally — there is no
   shared package to import.

Boundary discipline:

- Dependencies are injected as plain arguments. Configuration and
  secrets (`os.environ`, the config file) are read only by
  [config](01-config.md); other components may read data files
  (TSVs, the DB, the engine binary) but only at injected paths.
- Importing a module has no side effects; work starts when a factory
  function is called. No module-level state or singletons — factories
  return instances so tests can create as many as they need.
- Data crossing a boundary is a `domain` type or a pydantic-validated
  shape, never an unchecked dict.

## Code style

- Small, single-purpose functions; early returns over nesting.
- Plain functions and modules over classes; a class only where state
  and lifecycle genuinely belong together (engine process, DB
  connection, opening book).
- Full type hints on every public function; builtin generics
  (`list[str]`, `X | None`), `Literal` over stringly-typed values.
- Name by domain: `cp_loss`, `judgment`, `book_exit_ply` — never
  `data`, `result2`, `helper`.
- Comments state constraints the code cannot express ("mate maps to
  ±10000 cp for loss arithmetic"), never narrate the next line.
- No speculative abstraction: three concrete call sites before a
  helper exists. No metaclasses, no decorator magic.
- Formatting is whatever `ruff format` emits; never hand-format.

## Errors and async

- Each component defines typed exceptions (`UnknownUserError`,
  `EngineCrashError`); the API layer maps them to HTTP codes.
  Raising bare `Exception` across a boundary blocks review.
- Async where I/O concurrency pays (engine pool, ingestion, LLM
  calls); plain sync elsewhere (storage). No fire-and-forget tasks:
  every `asyncio.Task` is awaited or tracked and cancelled on
  shutdown.
- Long-running work is cancellable and shuts down cleanly.

## Testing

- Unit tests per component under `backend/tests/`, mirroring the
  package layout; fixtures in `tests/testdata/`. CI runs no live
  network, no real LLM, no Stockfish — recorded fixtures and stubs
  instead. One opt-in local integration test builds and exercises
  the real engine binary.
- API-level integration tests use a temp DB plus stub engine and
  provider ([07-api.md](07-api.md)).
- Tests target each component's public `__init__` surface, not
  internals.

## CI gates (GitHub Actions, all required)

CI runs on GitHub Actions (`.github/workflows/ci.yml`), one job per
surface:
Backend: `uv sync` → `ruff check` + `ruff format --check` →
`pyright` → `lint-imports` → `pytest`.
Frontend: `pnpm install` → Biome check → `tsc --noEmit` → Vitest →
build. pre-commit runs the fast subset locally before each commit.

## Definition of done

A change is done when the gates pass, any new boundary or domain
type is reflected in the component docs in the same commit, and the
diff reads without the author having to explain it.
