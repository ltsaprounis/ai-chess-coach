// The time-window/time-control selection from the Dashboard and Coach
// filter row, kept in localStorage so it survives navigating between
// pages and reloads instead of resetting every time the page unmounts.
// Pure + injectable, mirroring coachAgent.ts; unit-tested in
// statsFilterStorage.test.ts.

const STORAGE_KEY = "statsFilters";

type FilterStore = Pick<Storage, "getItem" | "setItem">;

/** What `useStatsFilters` persists: the raw user picks, not the
 *  resolved time class — resolution against the current game list
 *  (most-played fallback etc.) stays in the hook. */
export type StoredStatsFilters = {
  windowDays: number | null;
  pickedClass: string | null;
};

/** The pre-persistence behavior: all-time, most-played class. */
const defaults = (): StoredStatsFilters => ({
  windowDays: null,
  pickedClass: null,
});

/** Narrows untrusted parsed JSON field-by-field, so one bad field
 *  (say, a window value dropped from `WINDOWS` in a later release)
 *  falls back alone instead of discarding the whole selection. */
function narrow(
  parsed: unknown,
  allowedDays: readonly (number | null)[],
): StoredStatsFilters {
  if (typeof parsed !== "object" || parsed === null) {
    return defaults();
  }
  const record = parsed as Record<string, unknown>;
  const rawDays = record.windowDays;
  const rawClass = record.pickedClass;
  return {
    windowDays:
      (typeof rawDays === "number" || rawDays === null) &&
      allowedDays.includes(rawDays)
        ? rawDays
        : null,
    pickedClass: typeof rawClass === "string" ? rawClass : null,
  };
}

/**
 * The stored selection, or the defaults when nothing valid is stored
 * or storage is unavailable. `allowedDays` is the caller's list of
 * valid window values (`WINDOWS` in `useStatsFilters.ts` — passed in
 * rather than imported to keep this module dependency-free).
 */
export function getStoredStatsFilters(
  allowedDays: readonly (number | null)[],
  store: FilterStore = localStorage,
): StoredStatsFilters {
  let raw: string | null;
  try {
    raw = store.getItem(STORAGE_KEY);
  } catch {
    return defaults();
  }
  if (raw === null) {
    return defaults();
  }
  try {
    return narrow(JSON.parse(raw), allowedDays);
  } catch {
    return defaults();
  }
}

export function setStoredStatsFilters(
  filters: StoredStatsFilters,
  store: FilterStore = localStorage,
): void {
  try {
    store.setItem(STORAGE_KEY, JSON.stringify(filters));
  } catch {
    // Storage blocked (private mode): the choice just doesn't persist.
  }
}
