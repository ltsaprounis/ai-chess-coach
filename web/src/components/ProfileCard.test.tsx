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
      },
    ],
    months: [],
    periods: [],
    openings: [],
    error_patterns: [],
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
