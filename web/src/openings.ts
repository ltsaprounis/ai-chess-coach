// Collapse the fine ECO opening variations the backend returns into
// their broader families, so the repertoire table reads as "how do I
// do in the French?" instead of dozens of near-duplicate rows. Pure +
// unit-tested in openings.test.ts.

import type { OpeningStats } from "./api.ts";

export type OpeningFamily = {
  /** The opening name up to the first colon, e.g. "French Defense". */
  family: string;
  games: number;
  wins: number;
  losses: number;
  draws: number;
  analyzedGames: number;
  /** Analysis-weighted mean cp loss; null when nothing is analyzed. */
  avgCpLoss: number | null;
};

/** The family is the opening name up to the first colon. */
export function openingFamily(name: string): string {
  const colon = name.indexOf(":");
  return (colon === -1 ? name : name.slice(0, colon)).trim();
}

/**
 * Group openings by family: sum the records and analyzed counts, and
 * take an analysis-weighted mean of avg_cp_loss over the sub-openings
 * that have one (null when the family has no analyzed games).
 */
export function groupByFamily(openings: OpeningStats[]): OpeningFamily[] {
  const byFamily = new Map<string, OpeningFamily>();
  const acplSum = new Map<string, number>();

  for (const opening of openings) {
    const family = openingFamily(opening.name);
    let bucket = byFamily.get(family);
    if (bucket === undefined) {
      bucket = {
        family,
        games: 0,
        wins: 0,
        losses: 0,
        draws: 0,
        analyzedGames: 0,
        avgCpLoss: null,
      };
      byFamily.set(family, bucket);
    }
    bucket.games += opening.games;
    bucket.wins += opening.wins;
    bucket.losses += opening.losses;
    bucket.draws += opening.draws;
    bucket.analyzedGames += opening.analyzed_games;
    if (opening.avg_cp_loss !== null && opening.avg_cp_loss !== undefined) {
      const sum = acplSum.get(family) ?? 0;
      acplSum.set(family, sum + opening.avg_cp_loss * opening.analyzed_games);
    }
  }

  for (const bucket of byFamily.values()) {
    if (bucket.analyzedGames > 0) {
      const sum = acplSum.get(bucket.family) ?? 0;
      bucket.avgCpLoss = Math.round((sum / bucket.analyzedGames) * 10) / 10;
    }
  }

  return [...byFamily.values()];
}
