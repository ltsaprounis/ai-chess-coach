// Collapse the fine ECO opening variations the backend returns into
// the broader "systems" the player actually plays, so the repertoire
// table reads as "how do I do with 1.d4?" instead of dozens of
// near-duplicate rows split across engine-generated names. Pure +
// unit-tested in openings.test.ts.
//
// Family rollup rule — docs/06-coach.md "Family rollup": consumers
// (this component's prompt, and the Dashboard) collapse rows by
// (color, system), labelling the family with its most-played member's
// name root. Keying on the player's OWN moves (never the opponent's)
// is what keeps the London and the Torre apart though both are named
// "Queen's Pawn Game", and gathers every Pirc under one heading though
// the opponent's replies split the lichess names a dozen ways. The
// backend implements the identical rule for the coach report — see
// docs/06-coach.md — so the two must never disagree.

import type { Color, OpeningStats } from "./api.ts";

export type OpeningFamily = {
  /** The most-played member's name up to the first colon. Two
   *  families can share this label when they are different systems
   *  of the same name root (e.g. the London and the Torre both read
   *  "Queen's Pawn Game") — the `system` column is what disambiguates
   *  them, never this label alone. */
  family: string;
  color: Color;
  /** The rollup key: the player's own first moves, e.g. "1.d4 2.Nf3
   *  3.Bg5" as White. Identical for every row in this family. */
  system: string;
  /** The most-played member's full line (both sides), e.g.
   *  "1.d4 d5 2.Nf3 Nf6 3.Bg5 e6" — representative, not summed. */
  firstMoves: string;
  games: number;
  wins: number;
  losses: number;
  draws: number;
  analyzedGames: number;
  /** Whole-game ACPL, analysis-weighted; null when nothing is
   *  analyzed. Answers "how do these games go overall", not opening
   *  advice on its own — see `openingAcpl`. */
  avgCpLoss: number | null;
  /** Opening-phase-only ACPL, analysis-weighted; null when nothing is
   *  analyzed. This is the opening-advice number. */
  openingAcpl: number | null;
};

/** The name up to the first colon, e.g. "French Defense: Knight
 *  Variation" -> "French Defense". Used only as a display label — the
 *  rollup key is (color, system), never this string. */
export function openingFamily(name: string): string {
  const colon = name.indexOf(":");
  return (colon === -1 ? name : name.slice(0, colon)).trim();
}

function familyKey(color: Color, system: string): string {
  return `${color} ${system}`;
}

/** Deterministic "most-played member" ordering: more games first, tied
 *  rows broken by eco then name so the pick never depends on input
 *  order. */
function morePlayed(a: OpeningStats, b: OpeningStats): boolean {
  if (a.games !== b.games) {
    return a.games > b.games;
  }
  if (a.eco !== b.eco) {
    return a.eco < b.eco;
  }
  return a.name < b.name;
}

type MutableFamily = {
  family: string;
  color: Color;
  system: string;
  firstMoves: string;
  games: number;
  wins: number;
  losses: number;
  draws: number;
  analyzedGames: number;
  representative: OpeningStats;
};

/**
 * Group openings by (color, system): sum the records and analyzed
 * counts, and roll up `avg_cp_loss` and `opening_acpl` move-weighted
 * over the member rows that have one — per docs/06-coach.md "Family
 * rollup": `opening_acpl` is `Σ(opening_acpl × opening_moves) ÷
 * Σ opening_moves`, `avg_cp_loss` likewise over `player_moves`.
 * Weighting by `analyzed_games` instead (a game count) would rebuild
 * the mean-of-per-game-means one level up from the rows the backend
 * just removed it from — a 15-move loss would weigh as much as a
 * 90-move grind. Two colors of the same system are never merged, and
 * the label/first-moves shown come from the group's most-played
 * member.
 */
export function groupByFamily(openings: OpeningStats[]): OpeningFamily[] {
  const byKey = new Map<string, MutableFamily>();
  const cpLossSum = new Map<string, number>();
  const cpLossMoves = new Map<string, number>();
  const openingAcplSum = new Map<string, number>();
  const openingAcplMoves = new Map<string, number>();

  for (const opening of openings) {
    const key = familyKey(opening.color, opening.system);
    let bucket = byKey.get(key);
    if (bucket === undefined) {
      bucket = {
        family: openingFamily(opening.name),
        color: opening.color,
        system: opening.system,
        firstMoves: opening.first_moves,
        games: 0,
        wins: 0,
        losses: 0,
        draws: 0,
        analyzedGames: 0,
        representative: opening,
      };
      byKey.set(key, bucket);
    }
    bucket.games += opening.games;
    bucket.wins += opening.wins;
    bucket.losses += opening.losses;
    bucket.draws += opening.draws;
    bucket.analyzedGames += opening.analyzed_games;
    if (opening.avg_cp_loss !== null && opening.avg_cp_loss !== undefined) {
      cpLossSum.set(
        key,
        (cpLossSum.get(key) ?? 0) + opening.avg_cp_loss * opening.player_moves,
      );
      cpLossMoves.set(key, (cpLossMoves.get(key) ?? 0) + opening.player_moves);
    }
    if (opening.opening_acpl !== null && opening.opening_acpl !== undefined) {
      openingAcplSum.set(
        key,
        (openingAcplSum.get(key) ?? 0) +
          opening.opening_acpl * opening.opening_moves,
      );
      openingAcplMoves.set(
        key,
        (openingAcplMoves.get(key) ?? 0) + opening.opening_moves,
      );
    }
    if (morePlayed(opening, bucket.representative)) {
      bucket.representative = opening;
      bucket.family = openingFamily(opening.name);
      bucket.firstMoves = opening.first_moves;
    }
  }

  const families: OpeningFamily[] = [];
  for (const [key, bucket] of byKey) {
    const cpLossDenom = cpLossMoves.get(key) ?? 0;
    const avgCpLoss =
      cpLossDenom > 0
        ? Math.round(((cpLossSum.get(key) ?? 0) / cpLossDenom) * 10) / 10
        : null;
    const openingAcplDenom = openingAcplMoves.get(key) ?? 0;
    const openingAcpl =
      openingAcplDenom > 0
        ? Math.round(((openingAcplSum.get(key) ?? 0) / openingAcplDenom) * 10) /
          10
        : null;
    families.push({
      family: bucket.family,
      color: bucket.color,
      system: bucket.system,
      firstMoves: bucket.firstMoves,
      games: bucket.games,
      wins: bucket.wins,
      losses: bucket.losses,
      draws: bucket.draws,
      analyzedGames: bucket.analyzedGames,
      avgCpLoss,
      openingAcpl,
    });
  }
  return families;
}

/**
 * The player's own first three moves, in the same notation the
 * backend's `OpeningStats.system` uses ("1.d4 2.Nf3 3.Bg5" as White,
 * "1...d6 2...Nf6 3...g6" as Black) — computed from one game's SAN
 * moves (ply 1 = index 0) so the Games page can filter a drill-through
 * link down to the exact (color, system) a repertoire row represents,
 * not just its name-root label (which two different systems can
 * share, e.g. the London and the Torre).
 */
export function playerSystem(
  sanMoves: readonly string[],
  color: Color,
): string {
  const firstPly = color === "white" ? 0 : 1;
  const parts: string[] = [];
  for (let ply = firstPly, count = 0; count < 3; ply += 2, count++) {
    const san = sanMoves[ply];
    if (san === undefined) {
      break;
    }
    const moveNumber = Math.floor(ply / 2) + 1;
    parts.push(
      color === "white" ? `${moveNumber}.${san}` : `${moveNumber}...${san}`,
    );
  }
  return parts.join(" ");
}
