import { describe, expect, it } from "vitest";
import {
  getStoredAgentId,
  resolveAgentId,
  setStoredAgentId,
} from "./coachAgent";

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

describe("stored agent id", () => {
  it("round-trips a stored choice", () => {
    const store = memoryStore();
    setStoredAgentId("haiku", store);
    expect(getStoredAgentId(store)).toBe("haiku");
  });

  it("returns null when nothing is stored", () => {
    expect(getStoredAgentId(memoryStore())).toBeNull();
  });

  it("swallows storage failures", () => {
    expect(() => setStoredAgentId("haiku", throwingStore)).not.toThrow();
    expect(getStoredAgentId(throwingStore)).toBeNull();
  });
});

describe("resolveAgentId", () => {
  const roster = [{ id: "default-agent" }, { id: "haiku" }];

  it("keeps the stored id while the roster has it", () => {
    expect(resolveAgentId("haiku", roster, "default-agent")).toBe("haiku");
  });

  it("falls back to the server default when nothing is stored", () => {
    expect(resolveAgentId(null, roster, "default-agent")).toBe("default-agent");
  });

  it("falls back when the stored id is no longer configured", () => {
    expect(resolveAgentId("removed-agent", roster, "default-agent")).toBe(
      "default-agent",
    );
  });
});
