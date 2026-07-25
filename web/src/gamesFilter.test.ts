import { describe, expect, it } from "vitest";
import {
  matchesDrillThrough,
  parseDrillThroughFilter,
  parseOpeningMember,
} from "./gamesFilter";

function game(partial: {
  color: "white" | "black";
  opening?: { eco: string; name: string } | null;
  first_plies?: readonly string[];
}) {
  return {
    color: partial.color,
    opening: partial.opening ?? null,
    first_plies: partial.first_plies ?? [],
  };
}

describe("parseOpeningMember", () => {
  it("splits eco and name on the first pipe", () => {
    expect(parseOpeningMember("C50|Italian Game")).toEqual({
      eco: "C50",
      name: "Italian Game",
    });
  });

  it("splits on the FIRST pipe only, so a name containing one survives whole", () => {
    // ECO codes never contain "|", but nothing rules it out of a name —
    // split("|") would truncate; indexOf + slice must not.
    expect(parseOpeningMember("A00|Weird: Line|With Pipe")).toEqual({
      eco: "A00",
      name: "Weird: Line|With Pipe",
    });
  });

  it("treats a param with no pipe as an eco with an empty name", () => {
    expect(parseOpeningMember("C50")).toEqual({ eco: "C50", name: "" });
  });
});

describe("parseDrillThroughFilter", () => {
  it("reads family, color, system, faced, and repeated opening params", () => {
    const params = new URLSearchParams();
    params.set("family", "Italian Game");
    params.set("color", "white");
    params.set("faced", "true");
    params.append("opening", "C50|Italian Game");
    params.append("opening", "C53|Italian Game: Classical Variation");

    expect(parseDrillThroughFilter(params)).toEqual({
      family: "Italian Game",
      color: "white",
      system: "",
      faced: true,
      members: [
        { eco: "C50", name: "Italian Game" },
        { eco: "C53", name: "Italian Game: Classical Variation" },
      ],
    });
  });

  it("defaults every field for an empty URL", () => {
    expect(parseDrillThroughFilter(new URLSearchParams())).toEqual({
      family: "",
      color: "",
      system: "",
      faced: false,
      members: [],
    });
  });
});

describe("matchesDrillThrough", () => {
  it("matches everything when no filter is present", () => {
    const filter = parseDrillThroughFilter(new URLSearchParams());
    expect(
      matchesDrillThrough(
        game({ color: "white", opening: { eco: "C50", name: "Italian Game" } }),
        filter,
      ),
    ).toBe(true);
  });

  describe("tier 1: member list (current-format links, both partitions)", () => {
    it("matches a game whose (color, opening) is in the member list", () => {
      const params = new URLSearchParams();
      params.set("family", "Italian Game");
      params.set("color", "white");
      params.append("opening", "C50|Italian Game");
      params.append("opening", "C53|Italian Game: Classical Variation");
      const filter = parseDrillThroughFilter(params);

      expect(
        matchesDrillThrough(
          game({
            color: "white",
            opening: { eco: "C53", name: "Italian Game: Classical Variation" },
          }),
          filter,
        ),
      ).toBe(true);
    });

    it("rejects a member match on the wrong color, even with the same opening", () => {
      const params = new URLSearchParams();
      params.set("color", "white");
      params.append("opening", "C50|Italian Game");
      const filter = parseDrillThroughFilter(params);

      expect(
        matchesDrillThrough(
          game({
            color: "black",
            opening: { eco: "C50", name: "Italian Game" },
          }),
          filter,
        ),
      ).toBe(false);
    });

    it("rejects a game whose opening is not in the member list", () => {
      const params = new URLSearchParams();
      params.set("color", "white");
      params.append("opening", "C50|Italian Game");
      const filter = parseDrillThroughFilter(params);

      expect(
        matchesDrillThrough(
          game({ color: "white", opening: { eco: "C60", name: "Ruy Lopez" } }),
          filter,
        ),
      ).toBe(false);
    });

    it("rejects an unclassified game (no opening) when members are present", () => {
      const params = new URLSearchParams();
      params.set("color", "white");
      params.append("opening", "C50|Italian Game");
      const filter = parseDrillThroughFilter(params);

      expect(
        matchesDrillThrough(game({ color: "white", opening: null }), filter),
      ).toBe(false);
    });

    it("takes precedence over a system param present on the same URL", () => {
      // Shouldn't happen from a generated link, but a member list must
      // win over a stray legacy `system` param if both are present.
      const params = new URLSearchParams();
      params.set("color", "white");
      params.set("system", "1.e4");
      params.append("opening", "C50|Italian Game");
      const filter = parseDrillThroughFilter(params);

      expect(
        matchesDrillThrough(
          game({
            color: "white",
            opening: { eco: "C50", name: "Italian Game" },
            first_plies: ["d4", "d5"],
          }),
          filter,
        ),
      ).toBe(true);
    });
  });

  describe("tier 2: system (legacy chosen-partition links)", () => {
    it("matches the exact (color, system) derived from first_plies", () => {
      const params = new URLSearchParams();
      params.set("color", "white");
      params.set("system", "1.e4");
      const filter = parseDrillThroughFilter(params);

      expect(
        matchesDrillThrough(
          game({ color: "white", first_plies: ["e4", "e5"] }),
          filter,
        ),
      ).toBe(true);
      expect(
        matchesDrillThrough(
          game({ color: "white", first_plies: ["d4", "d5"] }),
          filter,
        ),
      ).toBe(false);
    });

    it("rejects the right system on the wrong color", () => {
      const params = new URLSearchParams();
      params.set("color", "white");
      params.set("system", "1.e4");
      const filter = parseDrillThroughFilter(params);

      expect(
        matchesDrillThrough(
          game({ color: "black", first_plies: ["e4", "e5"] }),
          filter,
        ),
      ).toBe(false);
    });
  });

  describe("tier 3: bare family (name-root fallback, now color-aware)", () => {
    it("matches by name root alone when no color is present (oldest links)", () => {
      const params = new URLSearchParams();
      params.set("family", "French Defense");
      const filter = parseDrillThroughFilter(params);

      expect(
        matchesDrillThrough(
          game({
            color: "black",
            opening: { eco: "C00", name: "French Defense: Knight Variation" },
          }),
          filter,
        ),
      ).toBe(true);
      expect(
        matchesDrillThrough(
          game({
            color: "white",
            opening: { eco: "C00", name: "French Defense: Knight Variation" },
          }),
          filter,
        ),
      ).toBe(true);
    });

    it("respects color when the link carries one (the undercount fix's color-aware fallback)", () => {
      const params = new URLSearchParams();
      params.set("family", "French Defense");
      params.set("color", "black");
      const filter = parseDrillThroughFilter(params);

      expect(
        matchesDrillThrough(
          game({
            color: "white",
            opening: { eco: "C00", name: "French Defense: Knight Variation" },
          }),
          filter,
        ),
      ).toBe(false);
      expect(
        matchesDrillThrough(
          game({
            color: "black",
            opening: { eco: "C00", name: "French Defense: Knight Variation" },
          }),
          filter,
        ),
      ).toBe(true);
    });

    it("rejects a different name root", () => {
      const params = new URLSearchParams();
      params.set("family", "French Defense");
      const filter = parseDrillThroughFilter(params);

      expect(
        matchesDrillThrough(
          game({
            color: "black",
            opening: { eco: "B01", name: "Scandinavian Defense" },
          }),
          filter,
        ),
      ).toBe(false);
    });
  });
});
