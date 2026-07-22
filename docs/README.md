# AI Chess Coach — Documentation Index

[PROPOSAL.md](PROPOSAL.md) is the high-level pitch. Each component below
has its own build plan. Components are decoupled: the server is the only
module that composes them, and everything else communicates through the
shared domain types and plain function interfaces.

## Component docs

| # | Component | Doc                              | Depends on            |
|---|-----------|----------------------------------|-----------------------|
| 1 | Config    | [01-config.md](01-config.md)     | nothing               |
| 2 | Ingestion | [02-ingestion.md](02-ingestion.md) | shared types        |
| 3 | Storage   | [03-storage.md](03-storage.md)   | shared types          |
| 4 | Engine    | [04-engine.md](04-engine.md)     | shared types          |
| 5 | Openings  | [05-openings.md](05-openings.md) | shared types          |
| 6 | Coach     | [06-coach.md](06-coach.md)       | shared types, LLM SDK |
| 7 | Server    | [07-server.md](07-server.md)     | components 1-6        |
| 8 | Frontend  | [08-frontend.md](08-frontend.md) | server HTTP API only  |

## Architecture

```
frontend ──HTTP/SSE──▶ server ──▶ ingestion  (chess.com public API)
                         ├──────▶ storage    (SQLite)
                         ├──────▶ engine     (Stockfish via UCI)
                         ├──────▶ openings   (ECO position lookup)
                         ├──────▶ coach      (report + LLM provider)
                         └──────▶ config     (loaded once at startup)
```

## Decoupling rules

1. Only the server imports other components. Components 1-6 never
   import each other.
2. Components share one contract module: `shared/types.ts` (domain
   types below). Nothing else is shared.
3. Config values are injected as plain arguments. No component other
   than config reads files or environment variables.
4. The frontend knows only the HTTP API defined in
   [07-server.md](07-server.md); it never imports server code.

## Repository layout

```
docs/                    this documentation
engines/stockfish/       git submodule (official Stockfish)
vendor/chess-openings/   git submodule (lichess ECO database)
shared/                  domain types package
server/                  Fastify app + components 1-6
web/                     Vite + React frontend
```

npm workspaces tie `shared`, `server`, and `web` together.

## Shared domain types (`shared/types.ts`)

```ts
type Game = {
  id: string; username: string; color: 'white' | 'black';
  pgn: string; sanMoves: string[]; timeControl: string;
  timeClass: 'bullet' | 'blitz' | 'rapid' | 'daily';
  result: 'win' | 'loss' | 'draw'; endTime: number;
  opponent: string; playerRating: number; opponentRating: number;
  accuracy: number | null;   // chess.com's own, when provided
};

type Judgment = 'best' | 'good' | 'inaccuracy' | 'mistake' | 'blunder';

type MoveEval = {
  ply: number; san: string; evalCp: number | null;
  evalMate: number | null; bestMove: string;
  cpLoss: number; judgment: Judgment;
};

type GameAnalysis = {
  gameId: string; depth: number; evals: MoveEval[];
  acplByPhase: { opening: number; middlegame: number; endgame: number };
  judgmentCounts: Record<Judgment, number>;
};

type Opening = { eco: string; name: string; ply: number };

type PlayerReport = {
  username: string; gamesAnalyzed: number; overallAcpl: number;
  acplByPhase: GameAnalysis['acplByPhase'];
  judgmentCounts: Record<Judgment, number>;
  openings: Array<Opening & { games: number; wins: number;
    losses: number; draws: number; avgCpLoss: number }>;
  criticalPositions: Array<{ fen: string; played: string;
    best: string; cpLoss: number; gameId: string }>;
};
```

Types may grow, but changes to them are contract changes — update the
affected component docs in the same commit.
