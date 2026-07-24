# AI Chess Coach

A web app that pulls your [chess.com](https://www.chess.com) games,
analyzes them with [Stockfish](https://stockfishchess.org), classifies
your openings, and turns the results into LLM coaching advice.

The backend is Python end to end (FastAPI + python-chess); TypeScript
lives only in the `web/` frontend.

## Features

- **Sync** — fetch and cache a chess.com player's games (no auth
  needed) and classify each opening against the lichess ECO database.
- **Analyze** — run Stockfish over every position, flagging
  inaccuracies, mistakes, and blunders and computing ACPL by game
  phase, with a live progress stream.
- **Explain a move** — ask the coach why a specific move was a mistake
  and what to play instead; explanations stream in and are cached.
- **Live eval** — toggle a Stockfish candidate-lines panel for any
  position on the board.
- **Report & repertoire** — a weakness report and a worst-first
  opening table, plus full LLM coaching advice on request.

LLM calls only ever fire when you ask for them, and every result is
cached so repeats don't re-bill.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python 3.12+)
- Node 22 LTS and [pnpm](https://pnpm.io)
- A C++ toolchain to build Stockfish (or `brew install stockfish` and
  set `engine.bin_path` in your config)

## Quickstart

```bash
git clone --recurse-submodules git@github.com:ltsaprounis/ai-chess-coach.git
cd ai-chess-coach

make engine                              # build Stockfish from the submodule
cd backend && uv sync && cd ..           # backend deps
cd web && pnpm install && pnpm build && cd ..   # frontend deps + build
```

Then run the app and open **http://localhost:8000**:

```bash
make dev-api    # serves the API and the built frontend on one port
```

Working on the frontend? Run `cd web && pnpm dev` alongside `make
dev-api` and use **http://localhost:5173** for hot reload; otherwise
re-run `pnpm build` to pick up UI changes.

## Coaching (LLM providers)

Two providers work today, both chosen because they ride a login you
likely already have and need **no API key or separate billing**:

- **`claude-agent-sdk`** (default) — uses your local Claude Code login.
- **`github-copilot`** — uses your GitHub Copilot CLI login (premium
  requests against your Copilot seat).

Keyed providers (**`anthropic`** via `ANTHROPIC_API_KEY`, and
**`azure-foundry`**) are planned — each slots in behind the same
provider seam. Configure the selectable coach roster under `coach:` in
your config; see [docs/06-coach.md](docs/06-coach.md).

## Configuration

Optional. Copy [`coach.config.example.yaml`](coach.config.example.yaml)
to `coach.config.yaml` and edit; every key has a default (engine depth,
thresholds, coach roster, port, DB path). Secrets go in environment
variables, never the file (e.g. `ANTHROPIC_API_KEY`).

## Development

```bash
make check     # backend: ruff + pyright + import-linter + pytest
make gen-api   # regenerate the frontend's OpenAPI types after API changes
```

Only `chess_coach.api` composes the other components; they talk through
shared domain types with boundaries enforced by import-linter in CI.
Read [docs/GUIDELINES.md](docs/GUIDELINES.md) before contributing, and
[docs/README.md](docs/README.md) for the architecture. FastAPI's own
API docs are at `http://localhost:8000/docs`.

## License

This project is licensed under the [MIT License](LICENSE).

Bundled dependencies keep their own licenses: Stockfish (GPLv3, run as a
separate process) and the lichess openings database (CC0) are git
submodules, and `python-chess` (GPLv3+) is a backend dependency — its
copyleft only applies if you distribute the backend, not when hosting
it as a service.
