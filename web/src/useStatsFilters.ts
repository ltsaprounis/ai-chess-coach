// Time-window and time-control filtering shared by the Dashboard and
// Coach pages (docs/08-frontend.md): "the same time-window and
// time-control controls the Dashboard uses" scope the coach request
// too, so advice covers the period the student is looking at rather
// than every game they have ever played. The selection is persisted
// via statsFilterStorage.ts (localStorage), one selection for both
// pages.

import { useMemo, useState } from "react";
import type { GameSummary, TimeClass } from "./api.ts";
import { type ClassRating, latestRatings } from "./stats.ts";
import {
  getStoredStatsFilters,
  setStoredStatsFilters,
} from "./statsFilterStorage.ts";

/** Time windows the filter can scope to; `days: null` is all-time. */
export const WINDOWS = [
  { label: "All time", days: null },
  { label: "Last 30 days", days: 30 },
  { label: "Last 90 days", days: 90 },
  { label: "Last 6 months", days: 182 },
  { label: "Last year", days: 365 },
] as const;

/** Valid `windowDays` values, for validating a stored selection. */
const WINDOW_DAYS: readonly (number | null)[] = WINDOWS.map(
  (window) => window.days,
);

const DAY_SECONDS = 86_400;

/** Sentinel `time_class` value meaning "don't scope by time control". */
export const ALL_CLASSES = "all" as const;

export type StatsFiltersState = {
  windowDays: number | null;
  setWindowDays: (days: number | null) => void;
  pickedClass: string | null;
  setPickedClass: (value: string) => void;
  /** Games within the selected window, before the time-control filter. */
  windowByTime: GameSummary[];
  /** Time controls present in the window, most-played first. */
  classOptions: ClassRating[];
  /** Resolved time class: the explicit "all", a picked class that's
   *  still present, else the most-played one — so the default view
   *  mixes controls only when the user asks for it. */
  timeClass: TimeClass | typeof ALL_CLASSES;
  /** `since` epoch-seconds for the window, or undefined for all-time. */
  since: number | undefined;
  /** `timeClass` as the query param the API expects, or undefined for
   *  "all classes". */
  classParam: TimeClass | undefined;
};

/**
 * Time-window/time-control filter state derived from a player's full
 * game list. `games` is typically `api.allGames(username)` — the same
 * paged fetch the Dashboard and Coach pages both already use.
 */
export function useStatsFilters(
  games: readonly GameSummary[],
): StatsFiltersState {
  // Seeded from localStorage and written back on every pick, so the
  // selection survives leaving the page (both pages unmount on any
  // navigation) and reloads, and Dashboard and Coach share one
  // selection — the "same controls" contract in docs/08-frontend.md.
  const [windowDays, setWindowDaysState] = useState<number | null>(
    () => getStoredStatsFilters(WINDOW_DAYS).windowDays,
  );
  const [pickedClass, setPickedClassState] = useState<string | null>(
    () => getStoredStatsFilters(WINDOW_DAYS).pickedClass,
  );

  const setWindowDays = (days: number | null): void => {
    setWindowDaysState(days);
    setStoredStatsFilters({ windowDays: days, pickedClass });
  };
  const setPickedClass = (value: string): void => {
    setPickedClassState(value);
    setStoredStatsFilters({ windowDays, pickedClass: value });
  };

  // Cutoff recomputed only when the window changes, so it stays stable
  // across renders (a fresh Date.now() each render would thrash query
  // keys downstream) — and quantized to today's UTC midnight rather
  // than the live second, because Coach.tsx sends this value verbatim
  // as `since` in the `POST /coach` body, and the backend uses it
  // as part of the report cache's primary key (see the `reports`
  // table). A raw `Date.now()`-derived cutoff is different on every
  // request, so every reload or navigate-away-and-back would produce
  // a fresh cache key, guaranteeing a cache miss, a fresh (paid) LLM
  // report, and an orphaned cache row. Quantizing to a day boundary
  // makes the same window produce the same `since` all day, so the
  // cache actually hits. Do not "simplify" this back to
  // `Date.now() - windowDays * DAY_SECONDS`.
  const since = useMemo(() => {
    if (windowDays === null) {
      return undefined;
    }
    const now = new Date();
    const todayUtcMidnight =
      Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) /
      1000;
    return todayUtcMidnight - windowDays * DAY_SECONDS;
  }, [windowDays]);

  const windowByTime = useMemo(
    () =>
      since === undefined
        ? [...games]
        : games.filter((g) => g.end_time >= since),
    [games, since],
  );

  const classOptions = useMemo(
    () => latestRatings(windowByTime),
    [windowByTime],
  );

  const timeClass =
    pickedClass === ALL_CLASSES
      ? ALL_CLASSES
      : (classOptions.find((entry) => entry.timeClass === pickedClass)
          ?.timeClass ??
        classOptions[0]?.timeClass ??
        ALL_CLASSES);
  const classParam = timeClass === ALL_CLASSES ? undefined : timeClass;

  return {
    windowDays,
    setWindowDays,
    pickedClass,
    setPickedClass,
    windowByTime,
    classOptions,
    timeClass,
    since,
    classParam,
  };
}
