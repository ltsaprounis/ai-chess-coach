import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { PlayerProfile, ProfileNarrative } from "../api";
import ProfileCard from "./ProfileCard";

// Same "no DOM" constraint as ChatPanel.test.tsx/Coach.test.tsx (the
// Vitest environment is "node", per vite.config.ts): these assert on
// rendered markup for given props rather than simulating clicks.
// `ProfileCard` renders react-router `<Link>`s (the games-page and
// example-game links, matching Dashboard's convention), which need a
// `Router` context even for static rendering — hence `MemoryRouter`.

function profile(partial: Partial<PlayerProfile> = {}): PlayerProfile {
  return {
    username: "alice",
    time_class: "rapid",
    games_covered: 42,
    games_in_scope: 42,
    window_start: 1_700_000_000,
    window_end: 1_720_000_000,
    player_moves: 1_000,
    overall_acpl: 35,
    judgment_counts: {
      best: 700,
      good: 200,
      inaccuracy: 60,
      mistake: 30,
      blunder: 10,
    },
    phases: {},
    time_classes: [
      {
        time_class: "rapid",
        record: { games: 42, wins: 20, losses: 15, draws: 7 },
        rating_start: 1400,
        rating_end: 1523,
        rating_min: 1380,
        rating_max: 1540,
        rating_max_at: 1_710_000_000,
        rating_min_at: 1_700_000_000,
      },
    ],
    months: [],
    periods: [],
    record: { games: 42, wins: 20, losses: 15, draws: 7 },
    color_records: {
      white: { games: 22, wins: 12, losses: 8, draws: 2 },
      black: { games: 20, wins: 8, losses: 7, draws: 5 },
    },
    best_win: null,
    streaks: null,
    opponents: null,
    terminations: [],
    openings: [],
    error_patterns: [],
    // Defaulted on the backend so snapshots stored under an older shape
    // still parse; stated here because the generated type keeps them
    // optional and the fixture is the card's whole contract.
    comparisons: [],
    trajectory: null,
    window_spans_level_change: false,
    analyzed_window_start: null,
    analyzed_window_end: null,
    narrative: null,
    ...partial,
  };
}

function narrativeMeta(
  partial: Partial<ProfileNarrative> = {},
): ProfileNarrative {
  return {
    agent_id: "coach-a",
    prompt_version: "profile-v1",
    generated_at: 1_720_000_000,
    games_covered: 42,
    ...partial,
  };
}

type RenderProps = {
  profile: PlayerProfile;
  narrative?: ProfileNarrative | null;
  narrativeGamesNow?: number | null;
  generating?: boolean;
  generateError?: string | null;
};

function render({
  profile: p,
  narrative = null,
  narrativeGamesNow = null,
  generating = false,
  generateError = null,
}: RenderProps): string {
  return renderToStaticMarkup(
    <MemoryRouter>
      <ProfileCard
        profile={p}
        narrative={narrative}
        narrativeGamesNow={narrativeGamesNow}
        agentLabel={(id) => `Agent ${id}`}
        generating={generating}
        generateError={generateError}
        onGenerate={() => {}}
      />
    </MemoryRouter>,
  );
}

describe("ProfileCard", () => {
  it("renders the facts and the narrative from a fetched profile", () => {
    const html = render({
      profile: profile({
        narrative: "Alice hangs pieces under pressure in the endgame.",
        openings: [
          {
            color: "white",
            name: "Italian Game",
            moves: "1.e4 2.Nf3 3.Bc4",
            games: 12,
            score: 0.625,
            faced: false,
          },
          {
            color: "black",
            name: "Sicilian Defense",
            moves: "1.e4 c5 2.Nf3 d6",
            games: 9,
            score: 0.4,
            faced: true,
          },
        ],
        error_patterns: [
          {
            pattern: "hangs_piece_to_check",
            label: "Hangs a piece to check",
            count: 7,
            share_of_blunders: 0.3,
            example_game_id: "g1",
            example_ply: 25,
            example_end_time: 1_700_000_000,
            example_move_number: 13,
            example_opponent: "marko77",
          },
        ],
      }),
      narrative: narrativeMeta(),
    });

    // Rating tile.
    expect(html).toContain("1523");
    expect(html).toContain("rapid rating");
    // Quality line: 35 cp -> 0.35 pawns, 10/1000 blunders -> 1%.
    expect(html).toContain("0.35 pawns");
    expect(html).toContain("1% of moves are blunders");
    // Repertoire.
    expect(html).toContain("Italian Game");
    expect(html).toContain("1.e4 2.Nf3 3.Bc4");
    expect(html).toContain("Sicilian Defense");
    // Recurring mistakes.
    expect(html).toContain("Hangs a piece to check");
    expect(html).toContain("7");
    // Narrative block, with its agent + generation date. The
    // apostrophe is checked separately since React SSR renders text
    // content apostrophes as the "&#x27;" entity.
    expect(html).toContain("The coach");
    expect(html).toContain("read on alice");
    expect(html).toContain("Alice hangs pieces under pressure in the endgame.");
    expect(html).toContain("Agent coach-a");
  });

  describe("trajectory and splits", () => {
    const trajectory = {
      rating_now: 1479,
      deltas: [
        { days: 30, rating_then: 1370, delta: 109, games: 190 },
        { days: 90, rating_then: 1494, delta: -15, games: 511 },
        { days: 365, rating_then: 1036, delta: 443, games: 1378 },
      ],
      rating_max: 1574,
      rating_max_at: 1_780_000_000,
      rating_min: 173,
      rating_min_at: 1_700_000_000,
      games: 1925,
      window_start: 1_700_000_000,
      window_end: 1_790_000_000,
      drawdown: {
        peak: 1574,
        peak_at: 1_780_000_000,
        trough: 1329,
        trough_at: 1_782_000_000,
        record: { games: 142, wins: 50, losses: 85, draws: 7 },
        since_record: { games: 263, wins: 130, losses: 120, draws: 13 },
        recovered: false,
      },
    };

    it("shows direction over the whole archive, not the level window", () => {
      const html = render({ profile: profile({ trajectory }) });
      expect(html).toContain("+443 over 365 days");
      expect(html).toContain("-15 over 90 days");
      expect(html).toContain("Over all 1925 games in this time control");
      expect(html).toContain("Largest setback: -245 points");
    });

    it("suppresses the peak gap while the student is improving", () => {
      // "95 below peak" beside "+443 over the year" is the misread the
      // whole rework exists to stop (docs/06-coach.md, "Trajectory").
      const improving = render({ profile: profile({ trajectory }) });
      expect(improving).not.toContain("(-17)");

      const falling = render({
        profile: profile({
          trajectory: {
            ...trajectory,
            deltas: [{ days: 365, rating_then: 1600, delta: -121, games: 900 }],
          },
        }),
      });
      expect(falling).toContain("(-17)"); // 1523 against a 1540 peak
    });

    it("states each split's verdict and never its arithmetic", () => {
      const html = render({
        profile: profile({
          comparisons: [
            {
              label: "Tilt",
              left_label: "within 2 hours of a loss",
              left: { games: 380, wins: 173, losses: 186, draws: 21 },
              right_label: "every other game",
              right: { games: 778, wins: 391, losses: 343, draws: 44 },
              gap: -4.8,
              resolution: 6.1,
              significant: false,
            },
          ],
        }),
      });
      expect(html).toContain("Tilt");
      expect(html).toContain("within noise");
      expect(html).not.toContain("sigma");
      expect(html).not.toContain("6.1");
    });

    it("drops a split too thin to measure rather than labelling it", () => {
      const html = render({
        profile: profile({
          comparisons: [
            {
              label: "Tilt",
              left_label: "within 2 hours of a loss",
              left: { games: 1, wins: 0, losses: 1, draws: 0 },
              right_label: "every other game",
              right: { games: 41, wins: 20, losses: 14, draws: 7 },
              gap: -50,
              resolution: 0,
              significant: false,
            },
          ],
        }),
      });
      expect(html).not.toContain("Splits");
    });

    it("says so when the window had to span a change in level", () => {
      const html = render({
        profile: profile({ window_spans_level_change: true }),
      });
      expect(html).toContain("span a change in your level");
    });
  });

  describe("staleness hint", () => {
    it("is absent when the narrative covers the same games as the fresh profile", () => {
      const html = render({
        profile: profile({ games_covered: 42, narrative: "Some read." }),
        narrative: narrativeMeta({ games_covered: 42 }),
      });
      expect(html).not.toContain(
        "Regenerate for a profile that covers your latest games",
      );
    });

    it("appears once more games are analyzed than the narrative covered", () => {
      const html = render({
        profile: profile({ games_covered: 50, narrative: "Some read." }),
        narrative: narrativeMeta({ games_covered: 42 }),
      });
      expect(html).toContain('role="alert"');
      expect(html).toContain("generated over 42 analyzed rapid games");
      expect(html).toContain("you now have 50");
    });

    it("appears when the fresh count is lower too, not only higher", () => {
      const html = render({
        profile: profile({ games_covered: 30, narrative: "Some read." }),
        narrative: narrativeMeta({ games_covered: 42 }),
      });
      expect(html).toContain("generated over 42 analyzed rapid games");
      expect(html).toContain("you now have 30");
    });
  });

  describe("generate / regenerate action", () => {
    it("offers a primary Generate call-to-action when no narrative exists yet", () => {
      const html = render({ profile: profile({ games_covered: 10 }) });
      expect(html).toContain("No rapid narrative yet");
      expect(html).toContain(">Generate<");
      expect(html).not.toContain(">Regenerate<");
    });

    it("disables and relabels the button while a generate request is in flight", () => {
      const html = render({
        profile: profile({ games_covered: 10 }),
        generating: true,
      });
      expect(html).toContain("Generating…");
      expect(html).toMatch(/<button[^>]*disabled(=""|\/?>| )/);
    });

    it("offers Regenerate, disabled while pending, once a narrative is stored", () => {
      const html = render({
        profile: profile({ games_covered: 10, narrative: "Read." }),
        narrative: narrativeMeta({ games_covered: 10 }),
        generating: true,
      });
      expect(html).toContain("Regenerating…");
      expect(html).toMatch(/<button[^>]*disabled(=""|\/?>| )/);
    });

    it("renders the freshly generated narrative once the mutation resolves", () => {
      const before = render({ profile: profile({ games_covered: 10 }) });
      expect(before).not.toContain("Alice overextends on the kingside.");

      const after = render({
        profile: profile({
          games_covered: 10,
          narrative: "Alice overextends on the kingside.",
        }),
        narrative: narrativeMeta({ games_covered: 10 }),
      });
      expect(after).toContain("Alice overextends on the kingside.");
      expect(after).toContain(">Regenerate<");
    });

    it("surfaces a generate error via the app's alert convention", () => {
      const html = render({
        profile: profile({ games_covered: 10 }),
        generateError: "no analyzed games yet -- sync and analyze first",
      });
      expect(html).toContain('role="alert"');
      expect(html).toContain("no analyzed games yet -- sync and analyze first");
    });
  });

  describe("coverage and scope", () => {
    it("names the time control the profile covers", () => {
      const html = render({ profile: profile({ time_class: "bullet" }) });
      expect(html).toContain("Player profile — bullet");
    });

    it("says all time controls when the facts mix them", () => {
      const html = render({ profile: profile({ time_class: null }) });
      expect(html).toContain("Player profile — all time controls");
    });

    // The headline correctness fix: quality figures come from analyzed
    // games, everything else from all of them, and the card must say so
    // rather than presenting the analyzed subset as the whole history.
    it("states both denominators when coverage is partial", () => {
      const html = render({
        profile: profile({ games_covered: 40, games_in_scope: 120 }),
      });
      expect(html).toContain("cover all 120 games");
      expect(html).toContain("40 analyzed");
    });

    it("says so plainly when everything in scope is analyzed", () => {
      const html = render({
        profile: profile({ games_covered: 42, games_in_scope: 42 }),
      });
      expect(html).toContain("All 42 games in scope are analyzed");
    });

    it("renders the recent-form windows with their own denominators", () => {
      const html = render({
        profile: profile({
          periods: [
            {
              label: "last 30 days",
              days: 30,
              games: 12,
              record: { games: 12, wins: 6, losses: 4, draws: 2 },
              analyzed_games: 8,
              player_moves: 300,
              acpl: 42,
              blunder_rate: 0.031,
              rating_end: 1540,
            },
            {
              label: "whole span",
              days: null,
              games: 42,
              record: { games: 42, wins: 20, losses: 15, draws: 7 },
              analyzed_games: 42,
              player_moves: 1000,
              acpl: 35,
              blunder_rate: 0.01,
              rating_end: 1523,
            },
          ],
        }),
      });
      expect(html).toContain("Recent form");
      expect(html).toContain("last 30 days");
      expect(html).toContain("whole span");
      expect(html).toContain("0.42");
      expect(html).toContain("3.1%");
    });

    it("omits recent form when there is only the whole span to show", () => {
      const html = render({
        profile: profile({
          periods: [
            {
              label: "whole span",
              days: null,
              games: 42,
              record: { games: 42, wins: 20, losses: 15, draws: 7 },
              analyzed_games: 42,
              player_moves: 1000,
              acpl: 35,
              blunder_rate: 0.01,
              rating_end: 1523,
            },
          ],
        }),
      });
      expect(html).not.toContain("Recent form");
    });

    it("shows an absent-quality window as a dash, never as zero loss", () => {
      const html = render({
        profile: profile({
          periods: [
            {
              label: "last 30 days",
              days: 30,
              games: 5,
              record: { games: 5, wins: 3, losses: 2, draws: 0 },
              analyzed_games: 0,
              player_moves: 0,
              acpl: null,
              blunder_rate: null,
              rating_end: 1540,
            },
            {
              label: "whole span",
              days: null,
              games: 42,
              record: { games: 42, wins: 20, losses: 15, draws: 7 },
              analyzed_games: 42,
              player_moves: 1000,
              acpl: 35,
              blunder_rate: 0.01,
              rating_end: 1523,
            },
          ],
        }),
      });
      expect(html).toContain("—");
      expect(html).not.toContain("0.00");
    });
  });

  // Volume-layer figures: they cover every game in scope, analyzed or
  // not (docs/06-coach.md, "Milestones").
  describe("milestones", () => {
    const streaks = {
      current_result: "loss" as const,
      current_length: 3,
      longest_win: 6,
      longest_loss: 4,
      after_loss: { games: 10, wins: 3, losses: 6, draws: 1 },
    };

    it("dates the rating peak beside the current rating", () => {
      const html = render({ profile: profile() });
      expect(html).toContain("peak 1540");
      expect(html).toContain(
        new Date(1_710_000_000 * 1000).toLocaleDateString(),
      );
      expect(html).toContain("(-17)"); // 1523 now, 1540 at the peak
    });

    it("omits the shortfall when the current rating is the peak", () => {
      const html = render({
        profile: profile({
          time_classes: [
            {
              time_class: "rapid",
              record: { games: 42, wins: 20, losses: 15, draws: 7 },
              rating_start: 1400,
              rating_end: 1540,
              rating_min: 1380,
              rating_max: 1540,
              rating_max_at: 1_710_000_000,
              rating_min_at: 1_700_000_000,
            },
          ],
        }),
      });
      expect(html).toContain("peak 1540");
      expect(html).not.toContain("(-");
    });

    it("deep-links the best win to its game", () => {
      const html = render({
        profile: profile({
          best_win: {
            game_id: "g-best",
            end_time: 1_700_000_000,
            time_class: "rapid",
            color: "white",
            opponent: "marko77",
            opponent_rating: 1750,
            player_rating: 1500,
          },
        }),
      });
      expect(html).toContain('href="/games/g-best"');
      expect(html).toContain("beat marko77 (1750)");
      // Gap first: the gap is the achievement, where the opponent's
      // rating alone restates the student's own peak.
      expect(html).toContain("250 points above them");
      expect(html).toContain("while rated 1500");
    });

    it("leaves the after-a-loss split to the verdicted Splits table", () => {
      // The raw gap is not shown beside the milestones any more: the
      // Splits table states it against a matched baseline and with a
      // verdict, and showing both puts the number above the judgement
      // of it (docs/06-coach.md, "Reading a comparison").
      const html = render({ profile: profile({ streaks }) });
      expect(html).toContain("3-game losing run");
      expect(html).toContain("longest 6 wins, 4 losses");
      expect(html).not.toContain("After a loss");
      expect(html).not.toContain("56% overall");
    });

    it("states how losses end, leaving the color split to Splits", () => {
      const html = render({
        profile: profile({
          terminations: [
            { result: "loss", termination: "timeout", games: 12 },
            { result: "loss", termination: "resigned", games: 8 },
          ],
        }),
      });
      expect(html).not.toContain("White 59% (22)");
      expect(html).toContain("timeout 60%");
      expect(html).toContain("resigned 40%");
    });

    // Matchmaking keeps nearly every game inside the "similar" band, so
    // on a real archive the other two rest on a handful of games —
    // "stronger 0%" without its denominator reads as a verdict.
    it("carries the sample size on every opposition band", () => {
      const html = render({
        profile: profile({
          opponents: {
            avg_rating_diff: 1.4,
            vs_stronger: { games: 6, wins: 0, losses: 6, draws: 0 },
            vs_similar: { games: 1062, wins: 511, losses: 492, draws: 59 },
            vs_weaker: { games: 5, wins: 4, losses: 1, draws: 0 },
          },
        }),
      });
      expect(html).toContain("stronger 0% (6)");
      expect(html).toContain("similar 51% (1062)");
      expect(html).toContain("weaker 80% (5)");
    });

    it("omits the losing breakdown when one cause covers every loss", () => {
      const html = render({
        profile: profile({
          terminations: [
            { result: "loss", termination: "resigned", games: 15 },
          ],
        }),
      });
      expect(html).not.toContain("How you lose");
    });

    it("drops the whole section when no milestone has data", () => {
      const html = render({
        profile: profile({ color_records: {}, terminations: [] }),
      });
      expect(html).not.toContain("Milestones");
    });
  });

  describe("empty states", () => {
    it("shows a short note with no Generate action at zero analyzed games", () => {
      const html = render({ profile: profile({ games_covered: 0 }) });
      expect(html).toContain("No analyzed rapid games in this window");
      expect(html).not.toContain("<button");
      expect(html).not.toContain("No rapid narrative yet");
    });

    it("shows the facts plus a Generate call-to-action with no narrative yet", () => {
      const html = render({
        profile: profile({
          games_covered: 10,
          openings: [],
          error_patterns: [],
        }),
      });
      expect(html).toContain("No rapid narrative yet");
      expect(html).toContain(">Generate<");
      expect(html).toContain("No chosen systems with enough games yet.");
      expect(html).toContain("No recurring problem lines yet.");
      expect(html).toContain("No tagged error patterns yet.");
    });
  });
});
