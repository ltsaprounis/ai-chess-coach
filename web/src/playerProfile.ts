// Pure helpers for the Coach page's player-profile card — no
// fetching, no React, unit-tested in playerProfile.test.ts (mirrors
// how highlights.ts/coachCoverage.ts sit beside their components).

import type { Color, PlayerProfile, ProfileNarrative } from "./api.ts";
import { score } from "./api.ts";

type ProfileOpening = PlayerProfile["openings"][number];
type ProfileErrorPattern = PlayerProfile["error_patterns"][number];
type ProfileTimeClass = PlayerProfile["time_classes"][number];
type ProfileTermination = PlayerProfile["terminations"][number];
type Record_ = ProfileTimeClass["record"];
type Streaks = NonNullable<PlayerProfile["streaks"]>;
export type RatingTrajectory = NonNullable<PlayerProfile["trajectory"]>;
export type Drawdown = NonNullable<RatingTrajectory["drawdown"]>;
export type ProfileComparison = PlayerProfile["comparisons"][number];

/** True unless both the 90- and 365-day deltas are ≤ 0 — the backend's
 *  `RatingTrajectory.improving`, which pydantic does not serialize
 *  because it is a property.
 *
 *  Mirrored rather than imported because the card must not contradict
 *  the prompt: one saying "up 443 points over the year" while the tile
 *  beside it labels the same student "95 below peak" is the misread
 *  this whole rule exists to stop. A student with neither delta yet
 *  measured counts as improving, so the gap stays suppressed rather
 *  than asserted on no evidence. */
export function isImproving(trajectory: RatingTrajectory | null): boolean {
  if (trajectory === null) return false;
  const long = trajectory.deltas.filter((d) => d.days === 90 || d.days === 365);
  return long.length === 0 || long.some((d) => d.delta > 0);
}

/** The delta for one trailing span, or null when the archive is too
 *  short to cover it — "up 443 over the last year" on an eight-month
 *  archive is a claim about a year that does not exist. */
export function deltaOver(
  trajectory: RatingTrajectory,
  days: number,
): RatingTrajectory["deltas"][number] | null {
  return trajectory.deltas.find((d) => d.days === days) ?? null;
}

/** The verdict a comparison renders, and nothing else: no sigmas, no
 *  p-values, no arithmetic. A number the reader cannot calibrate
 *  invites exactly the false confidence the guard removes
 *  (docs/06-coach.md, "Reading a comparison"). Unmeasurable splits are
 *  filtered out by `measurableComparisons` rather than labelled. */
export function comparisonVerdict(c: ProfileComparison): string {
  return c.significant ? "a real difference" : "within noise";
}

/** Only the splits that could actually be measured. An unmeasurable one
 *  says nothing in either direction, and a row reading "n/a — too few
 *  games" spends space on the absence of a finding. */
export function measurableComparisons(
  comparisons: readonly ProfileComparison[],
): ProfileComparison[] {
  return comparisons.filter((c) => c.left.games > 1 && c.right.games > 1);
}

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

/** A win/loss/draw record as a rounded percentage, counting draws as
 *  half — or `null` with no games, which the card renders as a dash.
 *  Zero games is an absent sample, never a 0% score. */
export function scorePercent(record: Record_): string | null {
  return record.games === 0 ? null : `${Math.round(score(record) * 100)}%`;
}

/** A game's date the way every other list view in the app writes one. */
export function formatGameDate(endTime: number): string {
  return new Date(endTime * 1000).toLocaleDateString();
}

/**
 * The dated rating peak for one time control (docs/06-coach.md,
 * "Milestones"): "1540 · 12/03/2026", or the bare rating on a profile
 * snapshot stored before `rating_max_at` existed. The date is the
 * point — a peak with no when is trivia.
 */
export function peakLabel(
  entry: Pick<ProfileTimeClass, "rating_max" | "rating_max_at">,
): string {
  const at = entry.rating_max_at;
  return at === null || at === undefined
    ? `${entry.rating_max}`
    : `${entry.rating_max} · ${formatGameDate(at)}`;
}

/** How far the current rating sits below the peak, as a negative
 *  number — 0 when the player is at their peak right now, which is the
 *  case worth *not* labelling as a shortfall. */
export function peakGap(
  entry: Pick<ProfileTimeClass, "rating_max" | "rating_end">,
): number {
  return Math.min(0, entry.rating_end - entry.rating_max);
}

/** The current run in words. A run of one is not a run — calling it a
 *  "1-game winning run" reads as momentum that does not exist. */
export function streakLabel(streaks: Streaks): string {
  const noun = { win: "winning", loss: "losing", draw: "drawn" }[
    streaks.current_result
  ];
  const last = { win: "won", loss: "lost", draw: "drawn" }[
    streaks.current_result
  ];
  return streaks.current_length === 1
    ? `Last game ${last}`
    : `${streaks.current_length}-game ${noun} run`;
}

/**
 * How one result came about, share-of-that-result included: the "38% of
 * losses are on the clock" read the raw counts leave to the reader.
 * Rows keep the backend's order (most games first within a result) and
 * are empty when that result never happened.
 */
export function terminationShares(
  terminations: readonly ProfileTermination[],
  result: ProfileTermination["result"],
): { termination: string; games: number; share: number }[] {
  const rows = terminations.filter((row) => row.result === result);
  const total = rows.reduce((sum, row) => sum + row.games, 0);
  return rows.map((row) => ({
    termination: row.termination,
    games: row.games,
    share: total === 0 ? 0 : row.games / total,
  }));
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
    parts.push(formatGameDate(pattern.example_end_time));
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
