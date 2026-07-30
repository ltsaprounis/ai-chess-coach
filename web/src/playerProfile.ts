// Pure helpers for the Coach page's player-profile card — no
// fetching, no React, unit-tested in playerProfile.test.ts (mirrors
// how highlights.ts/coachCoverage.ts sit beside their components).

import type { Color, PlayerProfile, ProfileNarrative } from "./api.ts";

type ProfileOpening = PlayerProfile["openings"][number];
type ProfileErrorPattern = PlayerProfile["error_patterns"][number];

/**
 * True once the stored narrative's own snapshot (`games_covered` at
 * generation time) no longer matches the live count for that same
 * scope — the "generated over N, you have M now" signal the Coach
 * page's advice staleness hint uses (docs/07-api.md,
 * `ProfileNarrative`). No narrative at all is never stale — there is
 * nothing to compare against.
 *
 * `narrativeGamesNow` and not `profile.games_covered`: the narrative
 * covers its time control's whole history while the facts honour the
 * page's window filter too, so comparing against the facts' count
 * would flag every windowed view as stale. A null falls back to the
 * facts' count, which is correct precisely when no window is applied.
 */
export function isProfileStale(
  profile: Pick<PlayerProfile, "games_covered">,
  narrative: Pick<ProfileNarrative, "games_covered"> | null,
  narrativeGamesNow: number | null = null,
): boolean {
  if (narrative === null) {
    return false;
  }
  const now = narrativeGamesNow ?? profile.games_covered;
  return narrative.games_covered !== now;
}

/** How the profile's scope reads in a sentence: the time control it
 *  covers, or "all time controls" when the facts mix them. */
export function scopeLabel(profile: Pick<PlayerProfile, "time_class">): string {
  return profile.time_class ?? "all time controls";
}

/** True when the quality figures rest on a subset of the games behind
 *  the volume figures — the case that must be stated rather than
 *  implied (docs/06-coach.md, "Volume and quality"). */
export function hasPartialCoverage(
  profile: Pick<PlayerProfile, "games_covered" | "games_in_scope">,
): boolean {
  return profile.games_in_scope > profile.games_covered;
}

/** Centipawns to pawns, two decimals, no sign — every value this
 *  formats (ACPL) is a magnitude, never a signed eval. */
export function formatPawns(centipawns: number): string {
  return (centipawns / 100).toFixed(2);
}

/** Blunders as a share of the player's own moves, 0-1 — 0 with no
 *  recorded moves rather than dividing by zero (docs/06-coach.md,
 *  "Judgment counts carry their denominator"). */
export function blunderShare(
  profile: Pick<PlayerProfile, "judgment_counts" | "player_moves">,
): number {
  if (profile.player_moves === 0) {
    return 0;
  }
  return (profile.judgment_counts.blunder ?? 0) / profile.player_moves;
}

/**
 * One (color, faced) partition of the repertoire rows `build_profile`
 * already rolled up and capped (docs/06-coach.md, "Player profile"):
 * the card only partitions further, by color, for its two-column
 * layout — ranking and the games floor are the backend's job, not
 * re-derived here.
 */
export function openingsFor(
  openings: readonly ProfileOpening[],
  color: Color,
  faced: boolean,
): ProfileOpening[] {
  return openings.filter((row) => row.color === color && row.faced === faced);
}

/** The recurring-mistakes example link's label: date + move number
 *  when both are known, else a plain fallback (docs/08-frontend.md,
 *  Dashboard "Recurring mistakes"). */
export function errorExampleLabel(pattern: ProfileErrorPattern): string {
  const parts: string[] = [];
  if (
    pattern.example_end_time !== null &&
    pattern.example_end_time !== undefined
  ) {
    parts.push(new Date(pattern.example_end_time * 1000).toLocaleDateString());
  }
  if (
    pattern.example_move_number !== null &&
    pattern.example_move_number !== undefined
  ) {
    parts.push(`move ${pattern.example_move_number}`);
  }
  return parts.length > 0 ? parts.join(" · ") : "view game";
}

/** The recurring-mistakes example link's target: the Game page's ply
 *  deep link (docs/08-frontend.md) when a ply is known, else the game
 *  alone. `null` when there is no example game to link to at all. */
export function errorExampleHref(pattern: ProfileErrorPattern): string | null {
  if (
    pattern.example_game_id === null ||
    pattern.example_game_id === undefined
  ) {
    return null;
  }
  return pattern.example_ply !== null && pattern.example_ply !== undefined
    ? `/games/${pattern.example_game_id}?ply=${pattern.example_ply}`
    : `/games/${pattern.example_game_id}`;
}
