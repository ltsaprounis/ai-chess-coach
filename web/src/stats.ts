// Pure stat computations behind the Dashboard page — no fetching,
// no React, unit-tested in stats.test.ts.

import type { GameSummary, PlayerReport, TimeClass } from "./api.ts";
import type { BarDatum } from "./components/BarChart.tsx";

export type { TimeClass };

/** The GameSummary fields the dashboard stats need. */
export type StatGame = Pick<
  GameSummary,
  "result" | "color" | "time_class" | "end_time" | "player_rating"
>;

export type Tally = {
  games: number;
  wins: number;
  losses: number;
  draws: number;
};

function emptyTally(): Tally {
  return { games: 0, wins: 0, losses: 0, draws: 0 };
}

function addResult(bucket: Tally, result: StatGame["result"]): void {
  bucket.games += 1;
  if (result === "win") {
    bucket.wins += 1;
  } else if (result === "loss") {
    bucket.losses += 1;
  } else {
    bucket.draws += 1;
  }
}

/** W-L-D record over all the given games. */
export function tally(games: readonly StatGame[]): Tally {
  const bucket = emptyTally();
  for (const game of games) {
    addResult(bucket, game.result);
  }
  return bucket;
}

/** W-L-D record split by the color the player had. */
export function tallyByColor(games: readonly StatGame[]): {
  white: Tally;
  black: Tally;
} {
  return {
    white: tally(games.filter((game) => game.color === "white")),
    black: tally(games.filter((game) => game.color === "black")),
  };
}

export type ClassRating = {
  timeClass: TimeClass;
  rating: number;
  games: number;
};

/**
 * Current rating per time class — the rating from that class's most
 * recent game — most-played class first.
 */
export function latestRatings(games: readonly StatGame[]): ClassRating[] {
  const byClass = new Map<TimeClass, { latest: StatGame; games: number }>();
  for (const game of games) {
    const entry = byClass.get(game.time_class);
    if (entry === undefined) {
      byClass.set(game.time_class, { latest: game, games: 1 });
    } else {
      entry.games += 1;
      if (game.end_time >= entry.latest.end_time) {
        entry.latest = game;
      }
    }
  }
  return [...byClass.entries()]
    .map(([timeClass, entry]) => ({
      timeClass,
      rating: entry.latest.player_rating,
      games: entry.games,
    }))
    .sort(
      (a, b) => b.games - a.games || a.timeClass.localeCompare(b.timeClass),
    );
}

/** The time class with the most games, or null with no games. */
export function mostPlayedClass(games: readonly StatGame[]): TimeClass | null {
  return latestRatings(games)[0]?.timeClass ?? null;
}

export type RatingPoint = { endTime: number; rating: number };

/** One class's rating after each game, oldest first. */
export function ratingSeries(
  games: readonly StatGame[],
  timeClass: TimeClass,
): RatingPoint[] {
  return games
    .filter((game) => game.time_class === timeClass)
    .sort((a, b) => a.end_time - b.end_time)
    .map((game) => ({ endTime: game.end_time, rating: game.player_rating }));
}

export type MonthActivity = Tally & { month: string };

/** UTC month bucket for an epoch-seconds timestamp, e.g. "2026-03". */
export function monthKey(endTime: number): string {
  const date = new Date(endTime * 1000);
  const month = String(date.getUTCMonth() + 1).padStart(2, "0");
  return `${date.getUTCFullYear()}-${month}`;
}

function nextMonth(key: string): string {
  const [year = 0, month = 1] = key.split("-").map(Number);
  return month === 12
    ? `${year + 1}-01`
    : `${year}-${String(month + 1).padStart(2, "0")}`;
}

/**
 * Games per UTC month split by result, oldest month first; months
 * without games between the first and last are filled with zeros so
 * the activity chart keeps an even time axis.
 */
export function monthlyActivity(games: readonly StatGame[]): MonthActivity[] {
  const byMonth = new Map<string, MonthActivity>();
  for (const game of games) {
    const key = monthKey(game.end_time);
    let bucket = byMonth.get(key);
    if (bucket === undefined) {
      bucket = { month: key, ...emptyTally() };
      byMonth.set(key, bucket);
    }
    addResult(bucket, game.result);
  }

  const keys = [...byMonth.keys()].sort();
  const first = keys[0];
  const last = keys[keys.length - 1];
  if (first === undefined || last === undefined) {
    return [];
  }
  const months: MonthActivity[] = [];
  for (let key = first; ; key = nextMonth(key)) {
    months.push(byMonth.get(key) ?? { month: key, ...emptyTally() });
    if (key === last) {
      break;
    }
  }
  return months;
}

/** The three phases a game passes through, in order. */
export const PHASES = ["opening", "middlegame", "endgame"] as const;

/**
 * Splits a report's per-phase ACPL into chartable bars and the phases
 * with no player moves at all. `PhaseStats.acpl` is `null` — never
 * `0.0` — when `moves` is zero, and the two must never be conflated:
 * a phase the player genuinely played error-free (moves > 0, acpl 0)
 * still gets a real bar, while a phase never reached gets an explicit
 * "no moves" state instead of a bar indistinguishable from flawless
 * play. `?? 0` on `acpl` would silently reintroduce that bug.
 */
export function splitPhases(phases: PlayerReport["phases"]): {
  phaseData: BarDatum[];
  emptyPhases: string[];
} {
  const phaseData: BarDatum[] = [];
  const emptyPhases: string[] = [];
  for (const phase of PHASES) {
    const stat = phases[phase];
    if (stat !== undefined && stat.moves > 0 && stat.acpl !== null) {
      phaseData.push({
        label: phase,
        value: stat.acpl,
        note: `${stat.moves} move${stat.moves === 1 ? "" : "s"}`,
      });
    } else {
      emptyPhases.push(phase);
    }
  }
  return { phaseData, emptyPhases };
}
