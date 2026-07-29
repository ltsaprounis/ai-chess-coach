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
export type RepertoireTree =
  paths["/api/players/{username}/openings/tree"]["get"]["responses"]["200"]["content"]["application/json"];
export type RepertoireNode = RepertoireTree["root"];
export type BookMove = RepertoireNode["book_moves"][number];
export type AnalyzeRequest = components["schemas"]["AnalyzeRequest"];
export type AnalyzeResult =
  paths["/api/players/{username}/analyze"]["post"]["responses"]["202"]["content"]["application/json"];
export type GameAnalysis = NonNullable<GameDetail["analysis"]>;
export type MoveEval = GameAnalysis["evals"][number];
export type PlayerReport =
  paths["/api/players/{username}/report"]["get"]["responses"]["200"]["content"]["application/json"];
export type PlayerHighlights =
  paths["/api/players/{username}/highlights"]["get"]["responses"]["200"]["content"]["application/json"];
export type HighlightMove = PlayerHighlights["blunders"][number];
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

/**
 * Query for the repertoire move tree (Openings explorer page): the
 * same window/time-control scope `report`/`openings` take, plus the
 * color the tree is built for and an optional `min_games` prune
 * override. The server defaults `min_games` to 2 (clamped 1-10); the
 * page's "Show one-off lines" toggle sends 1 to include one-off
 * deviations, and omits the field otherwise to keep the default.
 */
export type OpeningsTreeQuery = StatsQuery & {
  color: Color;
  min_games?: number;
};

/** Page size for the dashboard's fetch-everything helper. */
const ALL_GAMES_PAGE = 1000;
/**
 * Pathological-loop guard, not a cap: `allGames` stops naturally on a
 * short page, so no real archive should ever reach this many games in
 * one fetch. If it does, something's wrong (an API bug returning full
 * pages forever) — warn instead of looping forever.
 */
const ALL_GAMES_GUARD = 50_000;

export function queryString(
  params: Record<string, string | number | boolean | undefined>,
): string {
  const entries = Object.entries(params)
    .filter(([, value]) => value !== undefined && value !== "")
    .map(([key, value]) => [key, String(value)]);
  return entries.length === 0 ? "" : `?${new URLSearchParams(entries)}`;
}

/**
 * Thrown by `json()` for any non-2xx response. `status` lets callers
 * branch on a specific code — e.g. the Coach page treats a 409 from
 * `POST /analyze` ("a run is already active for this player",
 * docs/07-api.md) as "attach to progress", not a failure.
 */
export class HttpError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "HttpError";
    this.status = status;
  }
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      error?: { message?: string };
    } | null;
    throw new HttpError(
      response.status,
      body?.error?.message ?? `HTTP ${response.status}`,
    );
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
  /**
   * `full: true` re-fetches the entire archive instead of just the
   * games since the last sync, to backfill columns (currently
   * `termination`) added after the games were stored — a normal sync
   * never re-fetches a stored game. Omitting `full` keeps the plain
   * incremental URL unchanged.
   */
  sync: async (
    username: string,
    options: { full?: boolean } = {},
  ): Promise<SyncResult> =>
    json(
      await fetch(
        `/api/players/${encodeURIComponent(username)}/sync${queryString({
          full: options.full || undefined,
        })}`,
        { method: "POST" },
      ),
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
  /** Every stored game, paged; stops on a short page. */
  allGames: async (username: string): Promise<GameSummary[]> => {
    const all: GameSummary[] = [];
    for (let offset = 0; offset < ALL_GAMES_GUARD; offset += ALL_GAMES_PAGE) {
      const page = await api.games(username, {
        limit: ALL_GAMES_PAGE,
        offset,
      });
      all.push(...page);
      if (page.length < ALL_GAMES_PAGE) {
        return all;
      }
    }
    console.warn(
      `allGames: stopped after ${all.length} games — the pathological-loop guard was hit (${ALL_GAMES_GUARD}). This should never happen for a real archive; the API may be returning full pages endlessly.`,
    );
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
  /** Per-color repertoire move tree for the Openings explorer page —
   *  one fetch per (color, filters); the page drills client-side with
   *  no further requests (docs/future-improvements/
   *  openings-explorer.md). */
  openingsTree: async (
    username: string,
    query: OpeningsTreeQuery,
  ): Promise<RepertoireTree> =>
    json(
      await fetch(
        `/api/players/${encodeURIComponent(username)}/openings/tree${queryString(query)}`,
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
  /** Dashboard's blunders + brilliancies lists — same `since`/`time_class`
   *  scoping as `report`/`openings`. */
  highlights: async (
    username: string,
    query: StatsQuery = {},
  ): Promise<PlayerHighlights> =>
    json(
      await fetch(
        `/api/players/${encodeURIComponent(username)}/highlights${queryString(query)}`,
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
  /**
   * `since`/`until`/`time_class` scope the bulk (non-`gameIds`) path —
   * both the enqueue and `remaining` — so "analyze this window" is
   * expressible; the Coach page's "Analyze the rest" action uses them
   * with the same filters `/report` and `/coach` were sent.
   */
  analyze: async (
    username: string,
    options: {
      gameIds?: string[];
      limit?: number;
      since?: number;
      until?: number;
      time_class?: TimeClass;
    } = {},
  ): Promise<AnalyzeResult> =>
    json(
      await fetch(`/api/players/${encodeURIComponent(username)}/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          game_ids: options.gameIds,
          limit: options.limit,
          since: options.since,
          until: options.until,
          time_class: options.time_class,
        } satisfies AnalyzeRequest),
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
