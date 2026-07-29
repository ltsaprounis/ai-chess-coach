// The Games page's opening drill-through filter, pulled out as a pure
// function so the precedence between the repertoire link formats is
// unit-testable without the router. See
// docs/archive/fixes-2026-07/03-faced-openings.md ("Fold-in: the drill-through
// undercount") for why the `opening`-list match exists at all: a
// family's `games` sums every (color, eco, name) row rolled into it,
// transpositions included, so matching drill-through games by
// re-deriving the player's system from each game's moves only ever
// caught the family's *representative* line — a family reporting 8
// games would drill through to 4. Filtering by the exact member list
// frozen into the link at click time reproduces the family's count by
// construction.

import type { Color, OpeningStats } from "./api.ts";
import { openingFamily, playerSystem } from "./openings.ts";

export type OpeningMember = { eco: string; name: string };

/** Parses one `opening=ECO|Name` URL param value. ECO codes never
 *  contain `|`; a name could in principle, so this splits on the
 *  FIRST `|` only — never `split("|")`, which would silently drop the
 *  remainder of such a name. */
export function parseOpeningMember(raw: string): OpeningMember {
  const separator = raw.indexOf("|");
  if (separator === -1) {
    return { eco: raw, name: "" };
  }
  return { eco: raw.slice(0, separator), name: raw.slice(separator + 1) };
}

export type DrillThroughFilter = {
  /** The chip label; "" means no repertoire filter is active at all. */
  family: string;
  /** Loosely typed: a hand-typed or stale URL could carry anything, and
   *  every use here is a plain `===` comparison against `GameSummary.
   *  color`, so an invalid value just matches nothing rather than
   *  needing a cast. */
  color: string;
  /** Legacy exact-system links (pre-dating the member-list fix). */
  system: string;
  /** Current-format links: the family's frozen (eco, name) members. */
  members: OpeningMember[];
  /** Only used to word the filter chip ("faced as white" vs. "as
   *  white"); it plays no role in matching. */
  faced: boolean;
};

export function parseDrillThroughFilter(
  params: URLSearchParams,
): DrillThroughFilter {
  return {
    family: params.get("family") ?? "",
    color: params.get("color") ?? "",
    system: params.get("system") ?? "",
    members: params.getAll("opening").map(parseOpeningMember),
    faced: params.get("faced") === "true",
  };
}

type FilterableGame = {
  color: Color;
  opening?: Pick<OpeningStats, "eco" | "name"> | null;
  first_plies: readonly string[];
};

/**
 * The Games-page drill-through precedence
 * (docs/archive/fixes-2026-07/03-faced-openings.md): a member list, when
 * present, matches iff the game is the right color and its classified
 * opening is one of the frozen (eco, name) pairs — this is the
 * current format for both the chosen and faced partitions. Otherwise
 * a `system` param falls back to the legacy exact (color, system)
 * match. Otherwise a bare `family` falls back to the name-root match —
 * which now respects `color` when the link carries one (older links
 * may not). No filter at all (every field empty) matches everything.
 */
export function matchesDrillThrough(
  game: FilterableGame,
  filter: DrillThroughFilter,
): boolean {
  if (filter.members.length > 0) {
    if (game.color !== filter.color || !game.opening) {
      return false;
    }
    const opening = game.opening;
    return filter.members.some(
      (member) => member.eco === opening.eco && member.name === opening.name,
    );
  }
  if (filter.system !== "") {
    return (
      game.color === filter.color &&
      playerSystem(game.first_plies, game.color) === filter.system
    );
  }
  if (filter.family !== "") {
    if (filter.color !== "" && game.color !== filter.color) {
      return false;
    }
    return openingFamily(game.opening?.name ?? "") === filter.family;
  }
  return true;
}
