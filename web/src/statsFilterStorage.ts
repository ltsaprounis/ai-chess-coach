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

/**
 * The stored selection, or null when nothing usable is stored (first
 * visit, storage blocked, unparseable value) — the caller applies its
 * own default then. A stored *object* with one bad field (say, a
 * window value dropped from `WINDOWS` in a later release) degrades
 * that field alone to null (all-time / auto class) instead of
 * discarding the whole selection. `allowedDays` is the caller's list
 * of valid window values (`WINDOWS` in `useStatsFilters.ts` — passed
 * in rather than imported to keep this module dependency-free).
 */
export function getStoredStatsFilters(
  allowedDays: readonly (number | null)[],
  store: FilterStore = localStorage,
): StoredStatsFilters | null {
  let raw: string | null;
  try {
    raw = store.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
  if (raw === null) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) {
    return null;
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
