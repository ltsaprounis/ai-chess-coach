import { afterEach, describe, expect, it, vi } from "vitest";
import {
  api,
  explainUrl,
  HttpError,
  type OpeningStats,
  queryString,
  score,
  sortWorstFirst,
} from "./api";

describe("queryString", () => {
  it("returns an empty string for no params", () => {
    expect(queryString({})).toBe("");
  });

  it("drops undefined and empty values", () => {
    expect(queryString({ result: undefined, time_class: "" })).toBe("");
  });

  it("stringifies booleans and numbers", () => {
    expect(queryString({ analyzed: true, limit: 25 })).toBe(
      "?analyzed=true&limit=25",
    );
  });

  it("url-encodes values", () => {
    expect(queryString({ opening: "Ruy Lopez" })).toBe("?opening=Ruy+Lopez");
  });
});

function stats(partial: Partial<OpeningStats> & { eco: string }): OpeningStats {
  return {
    name: partial.eco,
    color: "white",
    system: "1.e4",
    first_moves: "1.e4 e5",
    faced: false,
    games: 0,
    wins: 0,
    losses: 0,
    draws: 0,
    analyzed_games: 0,
    avg_cp_loss: null,
    opening_acpl: null,
    opening_moves: 0,
    player_moves: 0,
    ...partial,
  };
}

describe("explainUrl", () => {
  it("omits refresh by default", () => {
    expect(explainUrl("demo-059", 28)).toBe(
      "/api/games/demo-059/explain?ply=28",
    );
  });

  it("omits refresh when explicitly false", () => {
    expect(explainUrl("demo-059", 28, "coach-a", false)).toBe(
      "/api/games/demo-059/explain?ply=28&agent_id=coach-a",
    );
  });

  it("adds refresh=true only when regenerating", () => {
    expect(explainUrl("demo-059", 28, "coach-a", true)).toBe(
      "/api/games/demo-059/explain?ply=28&agent_id=coach-a&refresh=true",
    );
  });
});

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

/** A page of `size` dummy rows — `allGames` only cares about length. */
function page(size: number): unknown[] {
  return Array.from({ length: size }, (_, i) => ({ id: `g${i}` }));
}

describe("api.sync", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("omits the full param by default, keeping the plain incremental URL", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ games_synced: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.sync("alice");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/players/alice/sync",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("omits the full param when explicitly false", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ games_synced: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.sync("alice", { full: false });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/players/alice/sync",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("adds full=true only when requesting a full re-sync", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ games_synced: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.sync("alice", { full: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/players/alice/sync?full=true",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

describe("api.allGames", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("stops after a single short page without paging further", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(page(3)));
    vi.stubGlobal("fetch", fetchMock);

    const games = await api.allGames("alice");

    expect(games).toHaveLength(3);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps paging while pages come back full, stopping on the first short one", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(page(1000)))
      .mockResolvedValueOnce(jsonResponse(page(1000)))
      .mockResolvedValueOnce(jsonResponse(page(500)));
    vi.stubGlobal("fetch", fetchMock);

    const games = await api.allGames("alice");

    expect(games).toHaveLength(2500);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("stops and warns at the pathological-loop guard if pages never come back short", async () => {
    // A fresh Response per call — a Response body can only be read once,
    // and this mock is invoked far more than the other cases here.
    const fetchMock = vi
      .fn()
      .mockImplementation(() => jsonResponse(page(1000)));
    vi.stubGlobal("fetch", fetchMock);
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    const games = await api.allGames("alice");

    // 50,000 is the guard, not a cap anyone should meet — this only
    // fires when every page keeps coming back full forever.
    expect(games).toHaveLength(50_000);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    warnSpy.mockRestore();
  });
});

describe("api.analyze", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts the page's current window/time-class filters for a scoped request", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ queued: 5, remaining: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.analyze("alice", { since: 1_700_000_000, time_class: "rapid" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/players/alice/analyze",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ since: 1_700_000_000, time_class: "rapid" }),
      }),
    );
  });

  it("throws an HttpError carrying the response status (e.g. 409, a run already active)", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ error: { message: "a run is already active" } }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    try {
      await api.analyze("alice", { limit: 10 });
      expect.unreachable("expected api.analyze to throw");
    } catch (error) {
      expect(error).toBeInstanceOf(HttpError);
      expect((error as HttpError).status).toBe(409);
      expect((error as HttpError).message).toBe("a run is already active");
    }
  });
});

describe("score and sortWorstFirst", () => {
  it("counts draws as half a point", () => {
    expect(
      score(stats({ eco: "X", games: 4, wins: 1, losses: 2, draws: 1 })),
    ).toBe(0.375);
  });

  it("sorts worst score first, more games breaking ties", () => {
    const bad = stats({ eco: "BAD", games: 4, wins: 0, losses: 4 });
    const mid = stats({ eco: "MID", games: 2, wins: 1, losses: 1 });
    const midBig = stats({ eco: "MIDBIG", games: 10, wins: 5, losses: 5 });
    const good = stats({ eco: "GOOD", games: 3, wins: 3, losses: 0 });

    const sorted = sortWorstFirst([good, mid, midBig, bad]).map((s) => s.eco);
    expect(sorted).toEqual(["BAD", "MIDBIG", "MID", "GOOD"]);
  });
});
