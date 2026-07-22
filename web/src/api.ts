// Typed API client — the only place URLs appear (docs/08-frontend.md).
// Types come from the generated OpenAPI schema; run `make gen-api`
// after changing backend routes or models.

import type { paths } from "./api/schema";

export type GameSummary =
  paths["/api/players/{username}/games"]["get"]["responses"]["200"]["content"]["application/json"][number];
export type GameDetail =
  paths["/api/games/{game_id}"]["get"]["responses"]["200"]["content"]["application/json"];
export type SyncResult =
  paths["/api/players/{username}/sync"]["post"]["responses"]["200"]["content"]["application/json"];
export type OpeningStats =
  paths["/api/players/{username}/openings"]["get"]["responses"]["200"]["content"]["application/json"][number];
export type AnalyzeResult =
  paths["/api/players/{username}/analyze"]["post"]["responses"]["202"]["content"]["application/json"];
export type GameAnalysis = NonNullable<GameDetail["analysis"]>;
export type MoveEval = GameAnalysis["evals"][number];

export type GameFilters = {
  result?: string;
  time_class?: string;
  analyzed?: boolean;
};

export function queryString(
  params: Record<string, string | number | boolean | undefined>,
): string {
  const entries = Object.entries(params)
    .filter(([, value]) => value !== undefined && value !== "")
    .map(([key, value]) => [key, String(value)]);
  return entries.length === 0 ? "" : `?${new URLSearchParams(entries)}`;
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      error?: { message?: string };
    } | null;
    throw new Error(body?.error?.message ?? `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

/** Win rate counting draws as half a point, 0..1. */
export function score(stats: {
  wins: number;
  losses: number;
  draws: number;
  games: number;
}): number {
  return stats.games === 0 ? 0 : (stats.wins + stats.draws / 2) / stats.games;
}

/** Worst score first; more games breaks ties (more signal first). */
export function sortWorstFirst(openings: OpeningStats[]): OpeningStats[] {
  return [...openings].sort((a, b) => score(a) - score(b) || b.games - a.games);
}

export const api = {
  sync: async (username: string): Promise<SyncResult> =>
    json(
      await fetch(`/api/players/${encodeURIComponent(username)}/sync`, {
        method: "POST",
      }),
    ),
  games: async (
    username: string,
    filters: GameFilters = {},
  ): Promise<GameSummary[]> =>
    json(
      await fetch(
        `/api/players/${encodeURIComponent(username)}/games${queryString(filters)}`,
      ),
    ),
  openings: async (username: string): Promise<OpeningStats[]> =>
    json(await fetch(`/api/players/${encodeURIComponent(username)}/openings`)),
  game: async (gameId: string): Promise<GameDetail> =>
    json(await fetch(`/api/games/${encodeURIComponent(gameId)}`)),
  analyze: async (
    username: string,
    gameIds?: string[],
  ): Promise<AnalyzeResult> =>
    json(
      await fetch(`/api/players/${encodeURIComponent(username)}/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(gameIds ? { game_ids: gameIds } : {}),
      }),
    ),
};

export function progressUrl(username: string): string {
  return `/api/players/${encodeURIComponent(username)}/analyze/progress`;
}
