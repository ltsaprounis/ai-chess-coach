import { describe, expect, it } from "vitest";
import type { PlayerProfile } from "./api.ts";
import {
  blunderShare,
  errorExampleHref,
  errorExampleLabel,
  hasPartialCoverage,
  isProfileStale,
  openingsFor,
  peakGap,
  peakLabel,
  scopeLabel,
  scorePercent,
  streakLabel,
  terminationShares,
} from "./playerProfile.ts";

describe("isProfileStale", () => {
  it("is false with no narrative at all — nothing to compare", () => {
    expect(isProfileStale({ games_covered: 10 }, null)).toBe(false);
  });

  it("is false when the narrative's own snapshot matches the fresh count", () => {
    expect(isProfileStale({ games_covered: 10 }, { games_covered: 10 })).toBe(
      false,
    );
  });

  it("is true once more games have been analyzed since generation", () => {
    expect(isProfileStale({ games_covered: 15 }, { games_covered: 10 })).toBe(
      true,
    );
  });

  it("is true even when the fresh count is lower (re-sync edge case), not just higher", () => {
    expect(isProfileStale({ games_covered: 5 }, { games_covered: 10 })).toBe(
      true,
    );
  });

  // The narrative covers its time control's full history while the
  // facts honour the window filter too, so under a window the two
  // counts describe different scopes and must not be compared.
  it("compares against the narrative's own scope, not the windowed facts", () => {
    expect(
      isProfileStale({ games_covered: 12 }, { games_covered: 40 }, 40),
    ).toBe(false);
  });

  it("is stale when the narrative's own scope has grown, whatever the window shows", () => {
    expect(
      isProfileStale({ games_covered: 12 }, { games_covered: 40 }, 55),
    ).toBe(true);
  });
});

describe("scopeLabel", () => {
  it("names the time control the profile covers", () => {
    expect(scopeLabel({ time_class: "rapid" })).toBe("rapid");
  });

  it("says all time controls when the facts mix them", () => {
    expect(scopeLabel({ time_class: null })).toBe("all time controls");
  });
});

describe("hasPartialCoverage", () => {
  it("is true when quality figures rest on a subset of the games", () => {
    expect(hasPartialCoverage({ games_covered: 40, games_in_scope: 120 })).toBe(
      true,
    );
  });

  it("is false once every game in scope is analyzed", () => {
    expect(hasPartialCoverage({ games_covered: 40, games_in_scope: 40 })).toBe(
      false,
    );
  });
});

describe("blunderShare", () => {
  it("divides blunders by player moves", () => {
    expect(
      blunderShare({ judgment_counts: { blunder: 10 }, player_moves: 100 }),
    ).toBe(0.1);
  });

  it("is 0 with no recorded moves rather than dividing by zero", () => {
    expect(blunderShare({ judgment_counts: {}, player_moves: 0 })).toBe(0);
  });

  it("is 0 when the judgment map carries no blunder key", () => {
    expect(
      blunderShare({ judgment_counts: { best: 100 }, player_moves: 100 }),
    ).toBe(0);
  });
});

function opening(
  partial: Partial<PlayerProfile["openings"][number]> = {},
): PlayerProfile["openings"][number] {
  return {
    color: "white",
    name: "Italian Game",
    moves: "1.e4",
    games: 8,
    score: 0.6,
    faced: false,
    ...partial,
  };
}

describe("openingsFor", () => {
  const rows = [
    opening({ color: "white", faced: false, name: "chosen-white" }),
    opening({ color: "white", faced: true, name: "faced-white" }),
    opening({ color: "black", faced: false, name: "chosen-black" }),
    opening({ color: "black", faced: true, name: "faced-black" }),
  ];

  it("filters to exactly one color-and-partition", () => {
    expect(openingsFor(rows, "white", false).map((r) => r.name)).toEqual([
      "chosen-white",
    ]);
    expect(openingsFor(rows, "black", true).map((r) => r.name)).toEqual([
      "faced-black",
    ]);
  });

  it("returns an empty list when nothing matches, not an error", () => {
    expect(openingsFor([], "white", false)).toEqual([]);
  });
});

function pattern(
  partial: Partial<PlayerProfile["error_patterns"][number]> = {},
): PlayerProfile["error_patterns"][number] {
  return {
    pattern: "hangs_piece",
    label: "Hangs a piece",
    count: 5,
    share_of_blunders: 0.2,
    example_game_id: null,
    example_ply: null,
    example_end_time: null,
    example_move_number: null,
    example_opponent: null,
    ...partial,
  };
}

describe("errorExampleLabel", () => {
  it("joins the date and move number when both are known", () => {
    const p = pattern({
      example_end_time: 1_700_000_000,
      example_move_number: 14,
    });
    expect(errorExampleLabel(p)).toBe(
      `${new Date(1_700_000_000 * 1000).toLocaleDateString()} · move 14`,
    );
  });

  it("falls back to a plain label with neither date nor move number", () => {
    expect(errorExampleLabel(pattern())).toBe("view game");
  });
});

// The example link must deep-link to the mistake's position via the
// Game page's `?ply=` param when the data carries one
// (docs/08-frontend.md "Dashboard"), and fall back to the bare game
// link when it doesn't — shared by the Dashboard's Recurring-mistakes
// table and the profile card.
describe("errorExampleHref", () => {
  it("is null with no example game to link to", () => {
    expect(errorExampleHref(pattern())).toBeNull();
  });

  it("links to the game alone without a known ply", () => {
    expect(errorExampleHref(pattern({ example_game_id: "g1" }))).toBe(
      "/games/g1",
    );
  });

  it("links the game alone when the ply is absent, not just null", () => {
    expect(
      errorExampleHref(
        pattern({
          example_game_id: "abc-123-uuid:leo",
          example_ply: undefined,
        }),
      ),
    ).toBe("/games/abc-123-uuid:leo");
  });

  it("links to the game's ply deep link when a ply is known", () => {
    expect(
      errorExampleHref(pattern({ example_game_id: "g1", example_ply: 27 })),
    ).toBe("/games/g1?ply=27");
  });

  it("keeps ply 0 — the initial position is a valid deep link", () => {
    expect(
      errorExampleHref(pattern({ example_game_id: "g1", example_ply: 0 })),
    ).toBe("/games/g1?ply=0");
  });
});

describe("scorePercent", () => {
  it("counts draws as half a point", () => {
    expect(scorePercent({ games: 4, wins: 1, losses: 1, draws: 2 })).toBe(
      "50%",
    );
  });

  it("is null with no games — an absent sample, never a 0% score", () => {
    expect(scorePercent({ games: 0, wins: 0, losses: 0, draws: 0 })).toBeNull();
  });
});

describe("peakLabel / peakGap", () => {
  const entry = {
    rating_end: 1523,
    rating_min: 1380,
    rating_max: 1540,
    rating_max_at: 1_700_000_000,
    rating_min_at: 1_690_000_000,
  };

  it("dates the peak — a peak with no when is trivia", () => {
    expect(peakLabel(entry)).toBe(
      `1540 · ${new Date(1_700_000_000 * 1000).toLocaleDateString()}`,
    );
  });

  it("falls back to the bare rating on a snapshot stored before the date existed", () => {
    expect(peakLabel({ ...entry, rating_max_at: null })).toBe("1540");
  });

  it("reports how far below the peak the current rating sits", () => {
    expect(peakGap(entry)).toBe(-17);
  });

  it("is zero at the peak rather than a positive shortfall", () => {
    expect(peakGap({ ...entry, rating_end: 1540 })).toBe(0);
  });
});

describe("streakLabel", () => {
  const streaks = {
    current_result: "loss" as const,
    current_length: 3,
    longest_win: 6,
    longest_loss: 4,
    after_loss: { games: 10, wins: 3, losses: 6, draws: 1 },
  };

  it("names the run and its length", () => {
    expect(streakLabel(streaks)).toBe("3-game losing run");
  });

  // A run of one is not a run: "1-game winning run" reads as momentum
  // that does not exist.
  it("calls a run of one what it is — the last game's result", () => {
    expect(
      streakLabel({ ...streaks, current_result: "win", current_length: 1 }),
    ).toBe("Last game won");
  });
});

describe("terminationShares", () => {
  const terminations = [
    { result: "win" as const, termination: "win", games: 30 },
    { result: "loss" as const, termination: "timeout", games: 12 },
    { result: "loss" as const, termination: "resigned", games: 8 },
  ];

  it("shares each cause against that result's own total, not every game", () => {
    expect(terminationShares(terminations, "loss")).toEqual([
      { termination: "timeout", games: 12, share: 0.6 },
      { termination: "resigned", games: 8, share: 0.4 },
    ]);
  });

  it("is empty for a result that never happened", () => {
    expect(terminationShares(terminations, "draw")).toEqual([]);
  });
});
