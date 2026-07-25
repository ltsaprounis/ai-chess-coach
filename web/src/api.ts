// Typed API client — the only place URLs appear (docs/08-frontend.md).
// Types come from the generated OpenAPI schema; run `make gen-api`
// after changing backend routes or models.

import type { components, paths } from "./api/schema";

export type GameSummary =
  paths["/api/players/{username}/games"]["get"]["responses"]["200"]["content"]["application/json"][number];
export type TimeClass = GameSummary["time_class"];
export type GameDetail =
  paths["/api/games/{game_id}"]["get"]["responses"]["200"]["content"]["application/json"];
export type SyncResult =
  paths["/api/players/{username}/sync"]["post"]["responses"]["200"]["content"]["application/json"];
export type OpeningStats =
  paths["/api/players/{username}/openings"]["get"]["responses"]["200"]["content"]["application/json"][number];
export type Color = OpeningStats["color"];
export type AnalyzeResult =
  paths["/api/players/{username}/analyze"]["post"]["responses"]["202"]["content"]["application/json"];
export type GameAnalysis = NonNullable<GameDetail["analysis"]>;
export type MoveEval = GameAnalysis["evals"][number];
export type PlayerReport =
  paths["/api/players/{username}/report"]["get"]["responses"]["200"]["content"]["application/json"];
export type PlayerSummary =
  paths["/api/players"]["get"]["responses"]["200"]["content"]["application/json"][number];
export type CoachResponse =
  paths["/api/players/{username}/coach"]["post"]["responses"]["200"]["content"]["application/json"];
export type CoachRequest = components["schemas"]["CoachRequest"];
export type CoachAgentsResponse =
  paths["/api/coach/agents"]["get"]["responses"]["200"]["content"]["application/json"];
export type CoachAgent = CoachAgentsResponse["agents"][number];

export type GameFilters = {
  result?: string;
  time_class?: string;
  analyzed?: boolean;
  limit?: number;
  offset?: number;
};

/**
 * Optional scoping for report/openings: an epoch-second window (since
 * inclusive, until exclusive) and/or a single time control.
 */
export type StatsQuery = {
  since?: number;
  until?: number;
  time_class?: TimeClass;
};

/**
 * Scoping + cache control for `POST /coach` — the same window and
 * time-control filters `/report` takes, so the coach reasons over the
 * period the student is looking at rather than every game ever
 * played. `refresh` skips the server cache and overwrites the cached
 * report, mirroring `explainUrl`'s `refresh` — used by the page's
 * Regenerate action.
 */
export type CoachOptions = StatsQuery & {
  agentId?: string;
  refresh?: boolean;
};

/** Page size for the dashboard's fetch-everything helper. */
const ALL_GAMES_PAGE = 500;
/** Hard cap so a huge archive cannot hammer the API from one page load. */
const ALL_GAMES_CAP = 2000;

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
  /** Every stored game, paged; stops on a short page or at the cap. */
  allGames: async (username: string): Promise<GameSummary[]> => {
    const all: GameSummary[] = [];
    for (let offset = 0; offset < ALL_GAMES_CAP; offset += ALL_GAMES_PAGE) {
      const page = await api.games(username, {
        limit: ALL_GAMES_PAGE,
        offset,
      });
      all.push(...page);
      if (page.length < ALL_GAMES_PAGE) {
        break;
      }
    }
    return all;
  },
  openings: async (
    username: string,
    query: StatsQuery = {},
  ): Promise<OpeningStats[]> =>
    json(
      await fetch(
        `/api/players/${encodeURIComponent(username)}/openings${queryString(query)}`,
      ),
    ),
  game: async (gameId: string): Promise<GameDetail> =>
    json(await fetch(`/api/games/${encodeURIComponent(gameId)}`)),
  report: async (
    username: string,
    query: StatsQuery = {},
  ): Promise<PlayerReport> =>
    json(
      await fetch(
        `/api/players/${encodeURIComponent(username)}/report${queryString(query)}`,
      ),
    ),
  coach: async (
    username: string,
    options: CoachOptions = {},
  ): Promise<CoachResponse> =>
    json(
      await fetch(`/api/players/${encodeURIComponent(username)}/coach`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: options.agentId ?? null,
          since: options.since ?? null,
          until: options.until ?? null,
          time_class: options.time_class ?? null,
          refresh: options.refresh ?? false,
        } satisfies CoachRequest),
      }),
    ),
  coachAgents: async (): Promise<CoachAgentsResponse> =>
    json(await fetch("/api/coach/agents")),
  players: async (): Promise<PlayerSummary[]> =>
    json(await fetch("/api/players")),
  analyze: async (
    username: string,
    options: { gameIds?: string[]; limit?: number } = {},
  ): Promise<AnalyzeResult> =>
    json(
      await fetch(`/api/players/${encodeURIComponent(username)}/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          game_ids: options.gameIds,
          limit: options.limit,
        }),
      }),
    ),
};

export function progressUrl(username: string): string {
  return `/api/players/${encodeURIComponent(username)}/analyze/progress`;
}

/** SSE live-eval stream for one position; depth/multipv default server-side. */
export function evalUrl(fen: string): string {
  return `/api/eval${queryString({ fen })}`;
}

/**
 * SSE move-explanation stream; agent defaults server-side when
 * omitted. `refresh` skips the server cache and regenerates,
 * overwriting the cached row — used by the panel's Regenerate action.
 */
export function explainUrl(
  gameId: string,
  ply: number,
  agentId?: string,
  refresh?: boolean,
): string {
  return `/api/games/${encodeURIComponent(gameId)}/explain${queryString({
    ply,
    agent_id: agentId,
    refresh: refresh || undefined,
  })}`;
}
