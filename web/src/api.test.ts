import { describe, expect, it } from "vitest";
import {
  explainUrl,
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
    games: 0,
    wins: 0,
    losses: 0,
    draws: 0,
    avg_cp_loss: null,
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
