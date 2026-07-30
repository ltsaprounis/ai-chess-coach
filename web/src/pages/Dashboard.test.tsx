import { describe, expect, it } from "vitest";
import type { PlayerReport } from "../api.ts";
import { errorExampleHref } from "./Dashboard";

type ErrorPattern = PlayerReport["error_patterns"][number];

function pattern(overrides: Partial<ErrorPattern>): ErrorPattern {
  return {
    pattern: "hanging_piece",
    label: "Hanging a piece",
    count: 7,
    share_of_blunders: 0.35,
    example_game_id: "abc-123-uuid:leo",
    ...overrides,
  };
}

// The Recurring-mistakes example must deep-link to the mistake's
// position via the Game page's `?ply=` param when the report carries
// one (docs/08-frontend.md "Dashboard"), and fall back to the bare
// game link when it doesn't.
describe("errorExampleHref", () => {
  it("appends ?ply= when the report carries an example ply", () => {
    expect(errorExampleHref(pattern({ example_ply: 42 }))).toBe(
      "/games/abc-123-uuid:leo?ply=42",
    );
  });

  it("keeps ply 0 — the initial position is a valid deep link", () => {
    expect(errorExampleHref(pattern({ example_ply: 0 }))).toBe(
      "/games/abc-123-uuid:leo?ply=0",
    );
  });

  it("links the bare game when the ply is null", () => {
    expect(errorExampleHref(pattern({ example_ply: null }))).toBe(
      "/games/abc-123-uuid:leo",
    );
  });

  it("links the bare game when the ply is absent", () => {
    expect(errorExampleHref(pattern({}))).toBe("/games/abc-123-uuid:leo");
  });
});
