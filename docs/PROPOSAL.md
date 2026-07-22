# AI Chess Coach — Proposal

## Goal
Browser app that pulls a user's chess.com games, analyzes them with
Stockfish, classifies the opening of each game, and emits a structured
"coach me" prompt for an LLM chess coach.

## Stack
- Frontend: Vite + React + TypeScript — board UI, eval graphs, reports.
- Backend: Python (FastAPI); python-chess drives native Stockfish (UCI).
- Storage: SQLite (stdlib sqlite3) caching games and analysis results.

## Engine
- Stockfish as a git submodule (official repo), built from source once
  with `make -j build` in `engines/stockfish/src`. Pinned, reproducible.
- Fallback: `brew install stockfish` if the local build fails.
- Backend spawns a small UCI worker pool; per-move eval at fixed depth.

## Openings
- lichess-org/chess-openings as a second submodule (TSV: ECO code,
  name, moves): https://github.com/lichess-org/chess-openings
- Build a position->opening map at startup; deepest book match wins.

## Data flow
1. User enters a chess.com username — the public API needs no auth:
   GET https://api.chess.com/pub/player/{user}/games/archives
2. Fetch monthly PGN archives, parse with python-chess, store in SQLite.
3. Queue games for analysis: eval every position, flag inaccuracies,
   mistakes, and blunders via centipawn-loss thresholds; ACPL per phase.
4. Classify each game's opening; aggregate repertoire win/loss stats.
5. Generate the coach prompt: profile summary, recurring weakness
   themes, worst openings, critical positions as FEN — then send it
   to the coaching LLM; keep a copy button for manual use.

## Milestones
1. Repo scaffold; submodules added; Stockfish builds and answers UCI.
2. Fetch and store games for a username; games list in the UI.
3. Analysis pipeline with progress UI; per-game report view.
4. Opening classification and aggregate stats dashboard.
5. Coach prompt generator and export.

## Coaching LLM
- v1 calls the Claude API (Anthropic SDK, model claude-opus-4-8).
- A thin provider interface (`coach(prompt) -> text`) keeps other APIs
  swappable via config — e.g. Azure AI Foundry for a future demo.

## Config
- `coach.config.yaml`: engine depth (default 16), worker count,
  mistake thresholds, LLM provider and model. Nothing hardcoded.
