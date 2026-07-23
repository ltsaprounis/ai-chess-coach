# AI Chess Coach

Web app that pulls a user's chess.com games, analyzes them with
Stockfish, classifies openings, and generates LLM coaching advice.

## Current state

All eight components (01–08) are built and the app runs end to end;
work now iterates on features and polish. The milestones in
[docs/PROPOSAL.md](../docs/PROPOSAL.md) remain the roadmap.

## Documentation map

- [docs/README.md](../docs/README.md) — index: architecture,
  decoupling rules, shared domain types. Read this first.
- [docs/GUIDELINES.md](../docs/GUIDELINES.md) — toolchain, style,
  testing, CI. Binding for all code.
- docs/01, 02, … — one numbered build plan per component; the set
  is open-ended, currently 01–08. New component: take the next free
  number, add a doc with the same interface/dependencies structure,
  and register it in the README table and import-linter contracts.
  Follow the docs; update them when contracts change.

## Stack (see GUIDELINES.md for detail)

- Backend: Python 3.12+ — FastAPI, python-chess, pydantic v2, stdlib
  sqlite3. Tooling: uv, ruff, pyright (strict), pytest, import-linter.
- Frontend (`web/` only): Vite + React + TypeScript — pnpm, Biome,
  Vitest. API types generated from OpenAPI; never hand-written.
- Submodules: `engines/stockfish`, `vendor/chess-openings`.

## Component sub-agents

`.claude/agents/` defines one sub-agent per component, all pinned to
Sonnet 5 (`claude-sonnet-5`). Each knows its component's doc, scope,
boundary rules, and verification gates:

| Agent        | Owns                              | Doc     |
|--------------|-----------------------------------|---------|
| config-dev   | `chess_coach.config`              | docs/01 |
| ingestion-dev| `chess_coach.ingestion`           | docs/02 |
| storage-dev  | `chess_coach.storage`             | docs/03 |
| engine-dev   | `chess_coach.engine`              | docs/04 |
| openings-dev | `chess_coach.openings`            | docs/05 |
| coach-dev    | `chess_coach.coach`               | docs/06 |
| api-dev      | `chess_coach.api` (composition)   | docs/07 |
| frontend-dev | `web/`                            | docs/08 |

When to delegate:
- A task scoped to a single component goes to its agent. Give it a
  self-contained prompt: the goal, affected files, and acceptance
  criteria — agents start cold and read their docs themselves.
- Independent single-component tasks may run as parallel agents;
  they touch disjoint directories by construction.
- Work spanning components stays in the main session: decide the
  contract change (domain types, component surfaces, HTTP API)
  first, update docs, then hand each component its slice. Endpoint
  wiring goes to api-dev; a new API surface usually means api-dev
  first, then frontend-dev after `pnpm gen:api`.
- Agents must not edit outside their component; if one reports a
  needed contract change, resolve it in the main session rather
  than relaunching the agent with broader scope.
- New component (docs/09+): create the doc first, then add a
  matching agent in `.claude/agents/` following the existing
  structure, pinned to the same model.

## Hard rules

- Only `chess_coach.api` composes components; components import
  `chess_coach.domain` and their own code, never each other.
- Config alone reads env/config files; everything else gets values
  injected as plain arguments.
- Domain type or interface change → update the affected component
  doc in the same commit.
- Docs use 80-column lines (tables and URLs may exceed).
- Commit only when asked; messages explain the why, not just what.
