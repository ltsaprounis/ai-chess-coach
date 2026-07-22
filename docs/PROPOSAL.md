# AI Chess Coach — Proposal

## Goal
Browser app that pulls a user's chess.com games, analyzes them with
Stockfish, classifies openings, and coaches the player via an LLM.
Build plans: [README.md](README.md) and the per-component docs.

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
2. Fetch monthly PGN archives, parse with python-chess, store in
   SQLite; classify each game's opening as it is ingested.
3. Queue games for analysis: eval every position, flag inaccuracies,
   mistakes, and blunders via centipawn-loss thresholds; ACPL per phase.
4. Aggregate repertoire win/loss stats and weakness themes per player.
5. Build the coach prompt (profile, weaknesses, worst openings,
   critical FENs) and send it to the LLM; copy button for manual use.

## Milestones
1. Repo scaffold; submodules added; Stockfish builds and answers UCI.
2. Fetch and store games for a username; games list in the UI.
3. Opening classification; repertoire win/loss dashboard.
4. Analysis pipeline with progress UI; per-game view; full dashboard.
5. Coach prompt generator and export.

## Coaching LLM
- v1: Claude API (Anthropic SDK, claude-opus-4-8) behind a provider
  interface (`complete(prompt) -> text`); other APIs — e.g. an
  Azure AI Foundry demo — swap in via config.

## Config
- `coach.config.yaml`: engine depth (default 16), worker count,
  mistake thresholds, LLM provider and model. Nothing hardcoded.
