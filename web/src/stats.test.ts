import { describe, expect, it } from "vitest";
import type { PlayerReport } from "./api";
import {
  latestRatings,
  monthKey,
  monthlyActivity,
  mostPlayedClass,
  ratingSeries,
  type StatGame,
  splitPhases,
  tally,
  tallyByColor,
} from "./stats";

function game(partial: Partial<StatGame> = {}): StatGame {
  return {
    result: "win",
    color: "white",
    time_class: "blitz",
    end_time: Date.UTC(2026, 0, 15) / 1000,
    player_rating: 800,
    ...partial,
  };
}

describe("tally", () => {
  it("returns zeros for no games", () => {
    expect(tally([])).toEqual({ games: 0, wins: 0, losses: 0, draws: 0 });
  });

  it("counts a single game", () => {
    expect(tally([game({ result: "draw" })])).toEqual({
      games: 1,
      wins: 0,
      losses: 0,
      draws: 1,
    });
  });

  it("counts wins, losses, and draws", () => {
    const games = [
      game({ result: "win" }),
      game({ result: "win" }),
      game({ result: "loss" }),
      game({ result: "draw" }),
    ];
    expect(tally(games)).toEqual({ games: 4, wins: 2, losses: 1, draws: 1 });
  });
});

describe("tallyByColor", () => {
  it("returns zero tallies for no games", () => {
    const { white, black } = tallyByColor([]);
    expect(white.games).toBe(0);
    expect(black.games).toBe(0);
  });

  it("splits results by the player's color", () => {
    const games = [
      game({ color: "white", result: "win" }),
      game({ color: "black", result: "loss" }),
      game({ color: "black", result: "draw" }),
    ];
    const { white, black } = tallyByColor(games);
    expect(white).toEqual({ games: 1, wins: 1, losses: 0, draws: 0 });
    expect(black).toEqual({ games: 2, wins: 0, losses: 1, draws: 1 });
  });
});

describe("latestRatings and mostPlayedClass", () => {
  it("returns nothing for no games", () => {
    expect(latestRatings([])).toEqual([]);
    expect(mostPlayedClass([])).toBeNull();
  });

  it("uses the most recent game's rating regardless of order", () => {
    const games = [
      game({ end_time: 200, player_rating: 850 }),
      game({ end_time: 100, player_rating: 700 }),
    ];
    expect(latestRatings(games)).toEqual([
      { timeClass: "blitz", rating: 850, games: 2 },
    ]);
  });

  it("orders classes by game count, most played first", () => {
    const games = [
      game({ time_class: "rapid", end_time: 1, player_rating: 900 }),
      game({ time_class: "blitz", end_time: 2, player_rating: 810 }),
      game({ time_class: "rapid", end_time: 3, player_rating: 920 }),
      game({ time_class: "bullet", end_time: 4, player_rating: 600 }),
    ];
    expect(latestRatings(games)).toEqual([
      { timeClass: "rapid", rating: 920, games: 2 },
      { timeClass: "blitz", rating: 810, games: 1 },
      { timeClass: "bullet", rating: 600, games: 1 },
    ]);
    expect(mostPlayedClass(games)).toBe("rapid");
  });
});

describe("ratingSeries", () => {
  it("returns nothing for no games", () => {
    expect(ratingSeries([], "blitz")).toEqual([]);
  });

  it("filters to the class and sorts oldest first", () => {
    const games = [
      game({ time_class: "blitz", end_time: 300, player_rating: 820 }),
      game({ time_class: "rapid", end_time: 200, player_rating: 950 }),
      game({ time_class: "blitz", end_time: 100, player_rating: 790 }),
    ];
    expect(ratingSeries(games, "blitz")).toEqual([
      { endTime: 100, rating: 790 },
      { endTime: 300, rating: 820 },
    ]);
  });
});

describe("monthKey", () => {
  it("splits games on either side of a UTC month boundary", () => {
    const endOfMarch = Date.UTC(2026, 2, 31, 23, 59, 59) / 1000;
    const startOfApril = Date.UTC(2026, 3, 1, 0, 0, 0) / 1000;
    expect(monthKey(endOfMarch)).toBe("2026-03");
    expect(monthKey(startOfApril)).toBe("2026-04");
  });
});

describe("monthlyActivity", () => {
  it("returns nothing for no games", () => {
    expect(monthlyActivity([])).toEqual([]);
  });

  it("buckets a single game into its month", () => {
    const games = [
      game({ end_time: Date.UTC(2026, 4, 10) / 1000, result: "loss" }),
    ];
    expect(monthlyActivity(games)).toEqual([
      { month: "2026-05", games: 1, wins: 0, losses: 1, draws: 0 },
    ]);
  });

  it("fills empty months between the first and last", () => {
    const games = [
      game({ end_time: Date.UTC(2026, 0, 5) / 1000, result: "win" }),
      game({ end_time: Date.UTC(2026, 2, 5) / 1000, result: "draw" }),
    ];
    expect(monthlyActivity(games)).toEqual([
      { month: "2026-01", games: 1, wins: 1, losses: 0, draws: 0 },
      { month: "2026-02", games: 0, wins: 0, losses: 0, draws: 0 },
      { month: "2026-03", games: 1, wins: 0, losses: 0, draws: 1 },
    ]);
  });

  it("rolls over a year boundary", () => {
    const games = [
      game({ end_time: Date.UTC(2025, 11, 31, 23, 0) / 1000, result: "win" }),
      game({ end_time: Date.UTC(2026, 0, 1, 1, 0) / 1000, result: "loss" }),
    ];
    expect(monthlyActivity(games).map((month) => month.month)).toEqual([
      "2025-12",
      "2026-01",
    ]);
  });
});

function phaseStat(
  partial: Partial<PlayerReport["phases"][string]> = {},
): NonNullable<PlayerReport["phases"][string]> {
  return { moves: 0, acpl: null, judgment_counts: {}, ...partial };
}

describe("splitPhases", () => {
  it("renders a phase with zero moves as 'no moves', never a zero bar", () => {
    const { phaseData, emptyPhases } = splitPhases({
      opening: phaseStat({ moves: 40, acpl: 22.5 }),
      middlegame: phaseStat({ moves: 30, acpl: 61.2 }),
      endgame: phaseStat({ moves: 0, acpl: null }),
    });
    expect(emptyPhases).toEqual(["endgame"]);
    expect(phaseData.map((bar) => bar.label)).toEqual([
      "opening",
      "middlegame",
    ]);
  });

  it("still renders a real zero ACPL phase as a bar — null and 0 are not the same", () => {
    const { phaseData, emptyPhases } = splitPhases({
      opening: phaseStat({ moves: 12, acpl: 0 }),
    });
    expect(emptyPhases).toEqual(["middlegame", "endgame"]);
    expect(phaseData).toEqual([
      { label: "opening", value: 0, note: "12 moves" },
    ]);
  });

  it("treats a missing phase key the same as zero moves", () => {
    const { phaseData, emptyPhases } = splitPhases({});
    expect(phaseData).toEqual([]);
    expect(emptyPhases).toEqual(["opening", "middlegame", "endgame"]);
  });

  it("singularizes the move-count note for exactly one move", () => {
    const { phaseData } = splitPhases({
      opening: phaseStat({ moves: 1, acpl: 5 }),
    });
    expect(phaseData[0]?.note).toBe("1 move");
  });
});
