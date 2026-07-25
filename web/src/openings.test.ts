import { describe, expect, it } from "vitest";
import type { OpeningStats } from "./api";
import { groupByFamily, openingFamily, playerSystem } from "./openings";

function opening(
  partial: Partial<OpeningStats> & {
    name: string;
    color: OpeningStats["color"];
    system: string;
  },
): OpeningStats {
  return {
    eco: "X00",
    first_moves: partial.system,
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

describe("openingFamily", () => {
  it("takes the name up to the first colon", () => {
    expect(openingFamily("French Defense: Knight Variation")).toBe(
      "French Defense",
    );
    expect(openingFamily("Polish Opening")).toBe("Polish Opening");
  });
});

describe("groupByFamily", () => {
  it("sums records within a (color, system) family", () => {
    const families = groupByFamily([
      opening({
        name: "French Defense: Knight Variation",
        color: "black",
        system: "1...e6",
        games: 5,
        losses: 5,
      }),
      opening({
        name: "French Defense: Steinitz Attack",
        color: "black",
        system: "1...e6",
        games: 2,
        losses: 2,
      }),
      opening({
        name: "Italian Game: Two Knights",
        color: "white",
        system: "1.e4 2.Nf3 3.Bc4",
        games: 3,
        wins: 3,
      }),
    ]);
    const french = families.find((f) => f.system === "1...e6");
    expect(french?.games).toBe(7);
    expect(french?.losses).toBe(7);
    expect(french?.color).toBe("black");
    expect(families).toHaveLength(2);
  });

  it("keeps two colors of the same family separate (the Englund regression)", () => {
    // A 1.d4 player who has only ever faced the Englund Gambit as
    // White must never see it rolled up with their own Black
    // repertoire, or any White system, just because the name-root
    // happens to collide.
    const families = groupByFamily([
      opening({
        name: "Englund Gambit",
        color: "white",
        system: "1.d4 2.dxe5 3.Nf3",
        first_moves: "1.d4 e5 2.dxe5 Nc6 3.Nf3 Qe7",
        games: 12,
        wins: 10,
        losses: 2,
      }),
      opening({
        name: "Englund Gambit",
        color: "black",
        system: "1...e5",
        first_moves: "1.d4 e5",
        games: 3,
        losses: 3,
      }),
    ]);
    expect(families).toHaveLength(2);
    const white = families.find((f) => f.color === "white");
    const black = families.find((f) => f.color === "black");
    expect(white?.games).toBe(12);
    expect(black?.games).toBe(3);
    // Confirms it isn't the same object/bucket under the hood.
    expect(white?.system).not.toBe(black?.system);
  });

  it("rolls up two lichess names sharing one system into one family", () => {
    // The London and the Torre Attack are both filed as "Queen's Pawn
    // Game" by lichess's ECO names, but they are different systems —
    // and two DIFFERENT names ("Queen's Pawn Game: London System" vs
    // "Trompowsky Attack", say) can equally share one system when the
    // opponent's reply is what varies the name. Rollup must key off
    // `system`, not `name`.
    const families = groupByFamily([
      opening({
        name: "Queen's Pawn Game: Accelerated London System",
        color: "white",
        system: "1.d4 2.Bf4 3.e3",
        games: 122,
        wins: 70,
        losses: 42,
        draws: 10,
      }),
      opening({
        name: "Indian Game: London System",
        color: "white",
        system: "1.d4 2.Bf4 3.e3",
        games: 33,
        wins: 20,
        losses: 10,
        draws: 3,
      }),
      opening({
        name: "Trompowsky Attack",
        color: "white",
        system: "1.d4 2.Bg5",
        games: 8,
        wins: 4,
        losses: 4,
      }),
    ]);
    const london = families.find((f) => f.system === "1.d4 2.Bf4 3.e3");
    expect(london?.games).toBe(155);
    expect(london?.wins).toBe(90);
    // Label comes from the most-played member.
    expect(london?.family).toBe("Queen's Pawn Game");
    expect(families).toHaveLength(2);
  });

  it("weights both ACPL columns by moves, not by games or analyzed games", () => {
    // Row "a" is a short game (5 player moves, 3 in the opening); row
    // "c" is a long one (45 player moves, 9 in the opening). A
    // game-weighted (or analyzed-games-weighted) rollup treats them as
    // equal-sized samples and rebuilds the mean-of-per-game-means the
    // backend's row-level move-weighting was meant to remove.
    const [family] = groupByFamily([
      opening({
        name: "X: a",
        color: "white",
        system: "1.e4",
        games: 2,
        analyzed_games: 2,
        avg_cp_loss: 100,
        player_moves: 5,
        opening_acpl: 20,
        opening_moves: 3,
      }),
      opening({
        name: "X: b",
        color: "white",
        system: "1.e4",
        games: 2,
        analyzed_games: 0,
        avg_cp_loss: null,
        opening_acpl: null,
      }),
      opening({
        name: "X: c",
        color: "white",
        system: "1.e4",
        games: 1,
        analyzed_games: 1,
        avg_cp_loss: 40,
        player_moves: 45,
        opening_acpl: 10,
        opening_moves: 9,
      }),
    ]);
    expect(family?.analyzedGames).toBe(3);
    // Move-weighted: (100*5 + 40*45) / (5+45) = 46 — not the
    // game-weighted (100*2 + 40*1) / 3 = 80 an analyzed-games rollup
    // would produce.
    expect(family?.avgCpLoss).toBe(46);
    expect(family?.avgCpLoss).not.toBe(80);
    // Move-weighted: (20*3 + 10*9) / (3+9) = 12.5 — not the
    // game-weighted (20*2 + 10*1) / 3 = 16.7 an analyzed-games rollup
    // would produce.
    expect(family?.openingAcpl).toBe(12.5);
    expect(family?.openingAcpl).not.toBe(16.7);
  });

  it("leaves both ACPL columns null when nothing is analyzed", () => {
    const [family] = groupByFamily([
      opening({ name: "Y: a", color: "black", system: "1...c5", games: 3 }),
    ]);
    expect(family?.avgCpLoss).toBeNull();
    expect(family?.openingAcpl).toBeNull();
  });
});

describe("playerSystem", () => {
  it("labels White's own first three moves with their move numbers", () => {
    expect(playerSystem(["d4", "d5", "Nf3", "Nf6", "Bg5", "e6"], "white")).toBe(
      "1.d4 2.Nf3 3.Bg5",
    );
  });

  it("labels Black's own first three moves with '...' notation", () => {
    expect(playerSystem(["d4", "d6", "Nf3", "Nf6", "g3", "g6"], "black")).toBe(
      "1...d6 2...Nf6 3...g6",
    );
  });

  it("stops early when the game is shorter than three of the player's moves", () => {
    expect(playerSystem(["e4", "e5"], "white")).toBe("1.e4");
    expect(playerSystem(["e4"], "black")).toBe("");
  });
});
