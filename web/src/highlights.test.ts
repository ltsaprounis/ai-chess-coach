import { describe, expect, it } from "vitest";
import {
  foldToPlayerPov,
  formatMoveLoss,
  formatPlayerEval,
  moveLabel,
} from "./highlights";

describe("moveLabel", () => {
  it("renders a White move with a dot", () => {
    expect(moveLabel({ move_number: 26, san: "Nb6", color: "white" })).toBe(
      "26.Nb6",
    );
  });

  it("renders a Black move with an ellipsis", () => {
    expect(moveLabel({ move_number: 26, san: "Nb6", color: "black" })).toBe(
      "26...Nb6",
    );
  });
});

describe("foldToPlayerPov", () => {
  it("leaves a White row's eval unchanged", () => {
    expect(
      foldToPlayerPov({
        eval_after_cp: 150,
        eval_after_mate: null,
        color: "white",
      }),
    ).toEqual({ cp: 150, mate: null });
  });

  it("negates a Black row's eval — a good Black move is white-POV negative", () => {
    expect(
      foldToPlayerPov({
        eval_after_cp: -150,
        eval_after_mate: null,
        color: "black",
      }),
    ).toEqual({ cp: 150, mate: null });
  });

  it("negates mate the same way", () => {
    expect(
      foldToPlayerPov({
        eval_after_cp: null,
        eval_after_mate: -3,
        color: "black",
      }),
    ).toEqual({ cp: null, mate: 3 });
  });

  it("passes through nulls untouched", () => {
    expect(
      foldToPlayerPov({
        eval_after_cp: null,
        eval_after_mate: null,
        color: "white",
      }),
    ).toEqual({ cp: null, mate: null });
  });
});

describe("formatPlayerEval", () => {
  it("formats a positive cp eval with a sign", () => {
    expect(formatPlayerEval({ cp: 235, mate: null })).toBe("+2.35");
  });

  it("formats a negative cp eval with a sign", () => {
    expect(formatPlayerEval({ cp: -80, mate: null })).toBe("-0.80");
  });

  it("shows equality without a sign", () => {
    expect(formatPlayerEval({ cp: 0, mate: null })).toBe("0.00");
  });

  it("treats a null cp with no mate as equality", () => {
    expect(formatPlayerEval({ cp: null, mate: null })).toBe("0.00");
  });

  it("renders a winning mate as #N", () => {
    expect(formatPlayerEval({ cp: null, mate: 4 })).toBe("#4");
  });

  it("renders a losing mate as -#N", () => {
    expect(formatPlayerEval({ cp: null, mate: -2 })).toBe("-#2");
  });
});

describe("formatMoveLoss", () => {
  it("renders cp_loss as a negative figure in pawns", () => {
    // The same move's explanation says "about 3.4 pawns"
    // (coach/prompt.py::format_cp_loss) — one scale, one page.
    expect(formatMoveLoss(340)).toBe("-3.4");
  });

  it("renders zero as -0.0, since a real blunder always has positive loss", () => {
    expect(formatMoveLoss(0)).toBe("-0.0");
  });

  it("reads on the same scale as the brilliancies column beside it", () => {
    // formatPlayerEval renders +2.50 for 250cp; a blunder table showing
    // -250 next to it was the defect this pass closed.
    expect(formatMoveLoss(250)).toBe("-2.5");
  });
});
