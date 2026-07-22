# AI Chess Coach

Web app that pulls a user's chess.com games, analyzes them with
Stockfish, classifies openings, and generates LLM coaching advice.

## Current state

Planning is complete; no code exists yet. Build follows the
milestones in [docs/PROPOSAL.md](../docs/PROPOSAL.md), starting with
milestone 1 (uv scaffold, submodules, Stockfish build).

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

## Hard rules

- Only `chess_coach.api` composes components; components import
  `chess_coach.domain` and their own code, never each other.
- Config alone reads env/config files; everything else gets values
  injected as plain arguments.
- Domain type or interface change → update the affected component
  doc in the same commit.
- Docs use 80-column lines (tables and URLs may exceed).
- Commit only when asked; messages explain the why, not just what.
