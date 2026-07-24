import { describe, expect, it } from "vitest";
import type { OpeningStats } from "./api";
import { groupByFamily, openingFamily } from "./openings";

function opening(
  partial: Partial<OpeningStats> & { name: string },
): OpeningStats {
  return {
    eco: "X00",
    games: 0,
    wins: 0,
    losses: 0,
    draws: 0,
    analyzed_games: 0,
    avg_cp_loss: null,
    ...partial,
  };
}

describe("openingFamily", () => {
  it("takes the name up to the first colon", () => {
    expect(openingFamily("French Defense: Knight Variation")).toBe(
      "French Defense",
    );
    expect(openingFamily("Polish Opening")).toBe("Polish Opening");
  });
});

describe("groupByFamily", () => {
  it("sums records across a family", () => {
    const families = groupByFamily([
      opening({
        name: "French Defense: Knight Variation",
        games: 5,
        losses: 5,
      }),
      opening({ name: "French Defense: Steinitz Attack", games: 2, losses: 2 }),
      opening({ name: "Italian Game: Two Knights", games: 3, wins: 3 }),
    ]);
    const french = families.find((f) => f.family === "French Defense");
    expect(french?.games).toBe(7);
    expect(french?.losses).toBe(7);
    expect(families).toHaveLength(2);
  });

  it("weights avg cp loss by analyzed games", () => {
    const [family] = groupByFamily([
      opening({ name: "X: a", games: 2, analyzed_games: 2, avg_cp_loss: 100 }),
      opening({ name: "X: b", games: 2, analyzed_games: 0, avg_cp_loss: null }),
      opening({ name: "X: c", games: 1, analyzed_games: 1, avg_cp_loss: 40 }),
    ]);
    expect(family?.analyzedGames).toBe(3);
    expect(family?.avgCpLoss).toBe(80); // (100*2 + 40*1) / 3
  });

  it("leaves cp loss null when nothing is analyzed", () => {
    const [family] = groupByFamily([
      opening({ name: "Y: a", games: 3, analyzed_games: 0 }),
    ]);
    expect(family?.avgCpLoss).toBeNull();
  });
});
