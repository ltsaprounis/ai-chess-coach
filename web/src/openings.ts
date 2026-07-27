// Collapse the fine ECO opening variations the backend returns into
// the broader "systems" the player actually plays, so the repertoire
// table reads as "how do I do with 1.d4?" instead of dozens of
// near-duplicate rows split across engine-generated names. Pure +
// unit-tested in openings.test.ts.
//
// Family rollup rule — docs/06-coach.md "Family rollup": consumers
// (this component's prompt, and the Dashboard) partition rows by
// `faced` BEFORE rolling up. The chosen partition (the systems the
// player picked) collapses by (color, system) exactly as before,
// labelling the family with its most-played member's name root.
// Keying on the player's OWN moves (never the opponent's) is what
// keeps the London and the Torre apart though both are named "Queen's
// Pawn Game", and gathers every Pirc under one heading though the
// opponent's replies split the lichess names a dozen ways. The faced
// partition (the lines opponents chose) collapses instead by (color,
// name root), because for a faced line the name *is* the opponent's
// choice while the player's own reply varies — keying it on `system`
// would split one opposing gambit across as many families as the
// player has tried answers to it. Two colors of one family never
// merge, and the two partitions never merge into each other even when
// their key happens to collide (a chosen French and a faced French
// stay apart). The backend implements the identical rule for the
// coach report — see docs/06-coach.md — so the two must never
// disagree.

import type { Color, OpeningStats } from "./api.ts";

export type OpeningFamily = {
  /** The most-played member's name up to the first colon. Two
   *  families can share this label when they are different systems
   *  of the same name root (e.g. the London and the Torre both read
   *  "Queen's Pawn Game") — the `system` column is what disambiguates
   *  them, never this label alone. */
  family: string;
  color: Color;
  /** True for the "what you face" partition (rolled up by name root),
   *  false for the "systems you chose" partition (rolled up by
   *  (color, system)) — see the rollup rule above. */
  faced: boolean;
  /** In the chosen partition this is the rollup key: the player's own
   *  first moves, e.g. "1.d4 2.Nf3 3.Bg5" as White, identical for
   *  every row in the family. In the faced partition it is NOT the
   *  key (name root is) and is display-only: the most-played member's
   *  `system`, i.e. the player's most common reply to this opponent
   *  line. */
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
  /** The rolled-up rows' identities — one (eco, name) pair per member
   *  `OpeningStats` row that fed this family. Frozen into the
   *  drill-through link so the Games page can match by exactly the
   *  rows this family counted, transpositions included, instead of
   *  re-deriving membership from a representative line that only some
   *  of the group's games actually played
   *  (docs/fixes-2026-07/03-faced-openings.md). */
  members: { eco: string; name: string }[];
};

/** The name up to the first colon, e.g. "French Defense: Knight
 *  Variation" -> "French Defense". Used as a display label in the
 *  chosen partition (whose rollup key is (color, system), never this
 *  string) and as the rollup key itself, paired with color, in the
 *  faced partition. */
export function openingFamily(name: string): string {
  const colon = name.indexOf(":");
  return (colon === -1 ? name : name.slice(0, colon)).trim();
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
  members: Map<string, { eco: string; name: string }>;
};

/**
 * Roll one partition (already filtered to all-chosen or all-faced) up
 * by `keyOf`, summing records and analyzed counts and move-weighting
 * both ACPL columns over the member rows that have one — per
 * docs/06-coach.md "Family rollup": `opening_acpl` is
 * `Σ(opening_acpl × opening_moves) ÷ Σ opening_moves`, `avg_cp_loss`
 * likewise over `player_moves`. Weighting by `analyzed_games` instead
 * (a game count) would rebuild the mean-of-per-game-means one level up
 * from the rows the backend just removed it from — a 15-move loss
 * would weigh as much as a 90-move grind. These summing rules are
 * identical in both partitions; only `keyOf` differs. The
 * label/first-moves/system shown come from the group's most-played
 * member — for the faced partition `system` is therefore display-only
 * (the player's commonest reply), never the rollup key.
 */
function rollupPartition(
  openings: OpeningStats[],
  keyOf: (opening: OpeningStats) => string,
  faced: boolean,
): OpeningFamily[] {
  const byKey = new Map<string, MutableFamily>();
  const cpLossSum = new Map<string, number>();
  const cpLossMoves = new Map<string, number>();
  const openingAcplSum = new Map<string, number>();
  const openingAcplMoves = new Map<string, number>();

  for (const opening of openings) {
    const key = keyOf(opening);
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
        members: new Map(),
      };
      byKey.set(key, bucket);
    }
    bucket.members.set(`${opening.eco}|${opening.name}`, {
      eco: opening.eco,
      name: opening.name,
    });
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
      bucket.system = opening.system;
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
      faced,
      system: bucket.system,
      firstMoves: bucket.firstMoves,
      games: bucket.games,
      wins: bucket.wins,
      losses: bucket.losses,
      draws: bucket.draws,
      analyzedGames: bucket.analyzedGames,
      avgCpLoss,
      openingAcpl,
      members: [...bucket.members.values()],
    });
  }
  return families;
}

/**
 * Partition rows by `faced` before rolling up (docs/06-coach.md
 * "Family rollup"): the chosen partition (the systems the player
 * picked) by (color, system), the faced partition (the lines
 * opponents picked against them) by (color, name root). The two
 * partitions are rolled up independently and never merge into each
 * other, even when a chosen row and a faced row happen to share both
 * a color and a name root.
 */
export function groupByFamily(openings: OpeningStats[]): OpeningFamily[] {
  const chosen = openings.filter((opening) => !opening.faced);
  const faced = openings.filter((opening) => opening.faced);
  return [
    ...rollupPartition(
      chosen,
      (opening) => `${opening.color} ${opening.system}`,
      false,
    ),
    ...rollupPartition(
      faced,
      (opening) => `${opening.color} ${openingFamily(opening.name)}`,
      true,
    ),
  ];
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
