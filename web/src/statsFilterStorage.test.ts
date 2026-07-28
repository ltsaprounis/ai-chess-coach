import { describe, expect, it } from "vitest";
import {
  getStoredStatsFilters,
  setStoredStatsFilters,
} from "./statsFilterStorage";

const ALLOWED_DAYS: readonly (number | null)[] = [null, 30, 90, 182, 365];

/** In-memory stand-in for localStorage (vitest runs in node). */
function memoryStore() {
  const map = new Map<string, string>();
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => {
      map.set(key, value);
    },
  };
}

const throwingStore = {
  getItem: (): string | null => {
    throw new Error("storage blocked");
  },
  setItem: (): void => {
    throw new Error("storage blocked");
  },
};

const DEFAULTS = { windowDays: null, pickedClass: null };

describe("stored stats filters", () => {
  it("round-trips a stored selection", () => {
    const store = memoryStore();
    setStoredStatsFilters({ windowDays: 90, pickedClass: "blitz" }, store);
    expect(getStoredStatsFilters(ALLOWED_DAYS, store)).toEqual({
      windowDays: 90,
      pickedClass: "blitz",
    });
  });

  it("round-trips the all-time / all-classes sentinels", () => {
    const store = memoryStore();
    setStoredStatsFilters({ windowDays: null, pickedClass: "all" }, store);
    expect(getStoredStatsFilters(ALLOWED_DAYS, store)).toEqual({
      windowDays: null,
      pickedClass: "all",
    });
  });

  it("returns defaults when nothing is stored", () => {
    expect(getStoredStatsFilters(ALLOWED_DAYS, memoryStore())).toEqual(
      DEFAULTS,
    );
  });

  it("swallows storage failures", () => {
    expect(() =>
      setStoredStatsFilters(
        { windowDays: 30, pickedClass: null },
        throwingStore,
      ),
    ).not.toThrow();
    expect(getStoredStatsFilters(ALLOWED_DAYS, throwingStore)).toEqual(
      DEFAULTS,
    );
  });

  it("returns defaults for malformed JSON", () => {
    const store = memoryStore();
    store.setItem("statsFilters", "{not json");
    expect(getStoredStatsFilters(ALLOWED_DAYS, store)).toEqual(DEFAULTS);
  });

  it("returns defaults for non-object JSON", () => {
    const store = memoryStore();
    store.setItem("statsFilters", '"blitz"');
    expect(getStoredStatsFilters(ALLOWED_DAYS, store)).toEqual(DEFAULTS);
  });

  it("drops a window no longer in the allowed list, keeping the class", () => {
    const store = memoryStore();
    store.setItem(
      "statsFilters",
      JSON.stringify({ windowDays: 60, pickedClass: "rapid" }),
    );
    expect(getStoredStatsFilters(ALLOWED_DAYS, store)).toEqual({
      windowDays: null,
      pickedClass: "rapid",
    });
  });

  it("drops a wrongly-typed class, keeping the window", () => {
    const store = memoryStore();
    store.setItem(
      "statsFilters",
      JSON.stringify({ windowDays: 365, pickedClass: 7 }),
    );
    expect(getStoredStatsFilters(ALLOWED_DAYS, store)).toEqual({
      windowDays: 365,
      pickedClass: null,
    });
  });
});
