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
};
