import { describe, expect, it } from "vitest";
import { queryString } from "./api";

describe("queryString", () => {
  it("returns an empty string for no params", () => {
    expect(queryString({})).toBe("");
  });

  it("drops undefined and empty values", () => {
    expect(queryString({ result: undefined, time_class: "" })).toBe("");
  });

  it("stringifies booleans and numbers", () => {
    expect(queryString({ analyzed: true, limit: 25 })).toBe(
      "?analyzed=true&limit=25",
    );
  });

  it("url-encodes values", () => {
    expect(queryString({ opening: "Ruy Lopez" })).toBe("?opening=Ruy+Lopez");
  });
});
