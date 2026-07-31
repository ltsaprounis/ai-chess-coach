import { describe, expect, it } from "vitest";
import type { RepertoireNode } from "./api.ts";
import {
  childRows,
  decodePath,
  encodePath,
  formatAvgEval,
  formatAvgLoss,
  formatLine,
  formatScore,
  isPlayerPly,
  levelLabel,
  plyLabel,
  resolvePath,
  validatePath,
  worstLines,
} from "./repertoireTree.ts";

/** Builds a `RepertoireNode` with sane defaults so each test only
 *  spells out the fields it cares about. */
function node(
  partial: Partial<RepertoireNode> & { san: string; ply: number },
): RepertoireNode {
  return {
    record: { games: 0, wins: 0, losses: 0, draws: 0 },
    analyzed: 0,
    eco: null,
    name: null,
    in_book: false,
    avg_eval_cp: null,
    avg_cp_loss: null,
    exits: 0,
    book_moves: [],
    children: [],
    ...partial,
  };
}

describe("isPlayerPly / levelLabel", () => {
  it("labels White's own plies odd, Black's even", () => {
    expect(isPlayerPly(1, "white")).toBe(true);
    expect(isPlayerPly(2, "white")).toBe(false);
    expect(isPlayerPly(3, "white")).toBe(true);
    expect(isPlayerPly(2, "black")).toBe(true);
    expect(isPlayerPly(1, "black")).toBe(false);
    expect(isPlayerPly(4, "black")).toBe(true);
  });

  it("levelLabel reads 'Your move' / 'Their move' from parity", () => {
    expect(levelLabel(1, "white")).toBe("Your move");
    expect(levelLabel(2, "white")).toBe("Their move");
    expect(levelLabel(1, "black")).toBe("Their move");
    expect(levelLabel(2, "black")).toBe("Your move");
  });
});

describe("resolvePath / validatePath", () => {
  const c5 = node({ san: "c5", ply: 2 });
  const e5 = node({ san: "e5", ply: 2 });
  const e4 = node({ san: "e4", ply: 1, children: [c5, e5] });
  const root = node({ san: "", ply: 0, children: [e4] });

  it("resolves a fully valid path to its node chain", () => {
    const chain = resolvePath(root, ["e4", "c5"]);
    expect(chain.map((n) => n.san)).toEqual(["", "e4", "c5"]);
  });

  it("stops at the first move that isn't a child (missing-path fallback)", () => {
    const chain = resolvePath(root, ["e4", "d5"]);
    expect(chain.map((n) => n.san)).toEqual(["", "e4"]);
  });

  it("resolves the empty path to just the root", () => {
    expect(resolvePath(root, []).map((n) => n.san)).toEqual([""]);
  });

  it("validatePath returns the path unchanged when it fully resolves", () => {
    expect(validatePath(root, ["e4", "e5"])).toEqual(["e4", "e5"]);
  });

  it("validatePath falls back to the root ([]) on any invalid path", () => {
    expect(validatePath(root, ["e4", "d5"])).toEqual([]);
    expect(validatePath(root, ["d4"])).toEqual([]);
  });

  it("validatePath treats [] as already valid", () => {
    expect(validatePath(root, [])).toEqual([]);
  });
});

describe("worstLines", () => {
  it("ranks player-level nodes by games x avg_cp_loss, skipping null-loss nodes", () => {
    // White repertoire: 1.e4 (player's move, ply 1) branches into
    // 1...c5 (opponent, ply 2, no cost) which branches into 2.Nf3
    // (player's move, ply 3) — the deep, expensive habit — and 2.Nc3
    // (player's move, ply 3, cheap). A second first move, 1.d4, is
    // rarer but costlier per game.
    const nf3 = node({
      san: "Nf3",
      ply: 3,
      record: { games: 10, wins: 3, losses: 5, draws: 2 },
      avg_cp_loss: 50, // impact 500 — the worst line
    });
    const nc3 = node({
      san: "Nc3",
      ply: 3,
      record: { games: 8, wins: 6, losses: 1, draws: 1 },
      avg_cp_loss: 5, // impact 40
    });
    const c5 = node({
      san: "c5",
      ply: 2,
      record: { games: 18, wins: 9, losses: 6, draws: 3 },
      avg_cp_loss: null, // opponent move with no analyzed games — skip
      children: [nf3, nc3],
    });
    const e4 = node({
      san: "e4",
      ply: 1,
      record: { games: 18, wins: 9, losses: 6, draws: 3 },
      avg_cp_loss: 5, // impact 90
      children: [c5],
    });
    const d4 = node({
      san: "d4",
      ply: 1,
      record: { games: 4, wins: 1, losses: 3, draws: 0 },
      avg_cp_loss: 30, // impact 120
    });
    const root = node({ san: "", ply: 0, children: [e4, d4] });

    const worst = worstLines(root, "white", 5);
    expect(worst.map((w) => w.path.join(","))).toEqual([
      "e4,c5,Nf3",
      "d4",
      "e4",
      "e4,c5,Nc3",
    ]);
    expect(worst[0]?.impact).toBe(500);
    // The null-loss opponent node (c5) never appears.
    expect(worst.some((w) => w.node.san === "c5")).toBe(false);
  });

  it("only considers the player's own plies for the given color", () => {
    // As Black, the first level (ply 1) is the opponent's — even a
    // huge, costly ply-1 node must not appear in Black's worst lines.
    const e4 = node({
      san: "e4",
      ply: 1,
      record: { games: 100, wins: 80, losses: 10, draws: 10 },
      avg_cp_loss: 200,
    });
    const root = node({ san: "", ply: 0, children: [e4] });
    expect(worstLines(root, "black", 5)).toEqual([]);
  });

  it("respects the limit", () => {
    const children = Array.from({ length: 10 }, (_, i) =>
      node({
        san: `m${i}`,
        ply: 1,
        record: { games: 1, wins: 0, losses: 1, draws: 0 },
        avg_cp_loss: i + 1,
      }),
    );
    const root = node({ san: "", ply: 0, children });
    expect(worstLines(root, "white", 3)).toHaveLength(3);
  });
});

describe("childRows", () => {
  it("turns played children into rows, carrying their own stats", () => {
    const child = node({
      san: "e4",
      ply: 1,
      name: "King's Pawn",
      eco: "C20",
      record: { games: 10, wins: 6, losses: 2, draws: 2 },
      avg_eval_cp: 35,
      avg_cp_loss: 12,
      in_book: true,
      exits: 3,
    });
    const parent = node({ san: "", ply: 0, children: [child] });

    const rows = childRows(parent);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      played: true,
      san: "e4",
      name: "King's Pawn",
      eco: "C20",
      games: 10,
      wins: 6,
      losses: 2,
      draws: 2,
      avgEvalCp: 35,
      avgCpLoss: 12,
      inBook: true,
      exits: 3,
    });
    expect(rows[0]?.node).toBe(child);
  });

  it("inherits the parent's name/eco when a child carries none of its own", () => {
    const child = node({ san: "Nf3", ply: 3, name: null, eco: null });
    const parent = node({
      san: "",
      ply: 2,
      name: "Sicilian Defense",
      eco: "B20",
      children: [child],
    });
    const [row] = childRows(parent);
    expect(row?.name).toBe("Sicilian Defense");
    expect(row?.eco).toBe("B20");
  });

  it("adds unplayed book moves as 'still to learn' rows, without a node", () => {
    const parent = node({
      san: "",
      ply: 0,
      children: [],
      book_moves: [
        { san: "e4", eco: "C20", name: "King's Pawn", played: false },
        { san: "d4", eco: "A40", name: "Queen's Pawn", played: false },
      ],
    });
    const rows = childRows(parent);
    expect(rows.map((r) => r.san).sort()).toEqual(["d4", "e4"]);
    expect(rows.every((r) => r.played === false)).toBe(true);
    expect(rows.every((r) => r.node === null)).toBe(true);
    expect(rows.every((r) => r.games === 0)).toBe(true);
  });

  it("never duplicates a played move as an unplayed row", () => {
    const child = node({
      san: "e4",
      ply: 1,
      record: { games: 5, wins: 5, losses: 0, draws: 0 },
    });
    const parent = node({
      san: "",
      ply: 0,
      children: [child],
      book_moves: [
        { san: "e4", eco: null, name: null, played: true },
        { san: "d4", eco: null, name: null, played: false },
      ],
    });
    const rows = childRows(parent);
    expect(rows).toHaveLength(2);
    expect(rows.filter((r) => r.san === "e4")).toHaveLength(1);
    expect(rows.find((r) => r.san === "e4")?.played).toBe(true);
    expect(rows.find((r) => r.san === "d4")?.played).toBe(false);
  });
});

describe("formatScore / formatAvgEval / formatAvgLoss", () => {
  it("formats a W/L/D record as a rounded percentage", () => {
    expect(formatScore({ games: 4, wins: 2, losses: 1, draws: 1 })).toBe("63%");
  });

  it("formats zero games as an em dash", () => {
    expect(formatScore({ games: 0, wins: 0, losses: 0, draws: 0 })).toBe("—");
  });

  it("formats a signed eval in pawns, or an em dash when null", () => {
    expect(formatAvgEval(150)).toBe("+1.50");
    expect(formatAvgEval(-75)).toBe("-0.75");
    expect(formatAvgEval(0)).toBe("0.00");
    expect(formatAvgEval(null)).toBe("—");
  });

  it("formats an avg loss as an unsigned magnitude, or an em dash when null", () => {
    expect(formatAvgLoss(12.34)).toBe("0.12");
    expect(formatAvgLoss(null)).toBe("—");
    // Same scale as formatAvgEval, which sits in the next column over.
    expect(formatAvgLoss(250)).toBe("2.50");
    expect(formatAvgEval(250)).toBe("+2.50");
  });
});

describe("plyLabel / formatLine", () => {
  it("prefixes White's ply with its move number, leaves Black's bare", () => {
    expect(plyLabel(1, "e4")).toBe("1.e4");
    expect(plyLabel(2, "c5")).toBe("c5");
    expect(plyLabel(3, "Nf3")).toBe("2.Nf3");
  });

  it("renders a path as one move-number-notation line", () => {
    expect(formatLine(["e4", "c5", "Nf3"])).toBe("1.e4 c5 2.Nf3");
  });

  it("renders an empty path as an empty string", () => {
    expect(formatLine([])).toBe("");
  });
});

describe("encodePath / decodePath", () => {
  it("round-trips a plain path", () => {
    const path = ["e4", "c5", "Nf3"];
    expect(decodePath(encodePath(path))).toEqual(path);
  });

  it("round-trips SAN containing '+' and '#'", () => {
    const path = ["e4", "e5", "Qh5", "Nc6", "Qxf7#"];
    const encoded = encodePath(path);
    // '+'/'#' must not survive unescaped in the query value.
    expect(encoded).not.toContain("#");
    expect(decodePath(encoded)).toEqual(path);

    const checkPath = ["e4", "e5", "Bc4", "Nc6", "Qh5", "g6", "Qf3+"];
    expect(decodePath(encodePath(checkPath))).toEqual(checkPath);
  });

  it("decodes a missing or empty param to the root path", () => {
    expect(decodePath(null)).toEqual([]);
    expect(decodePath(undefined)).toEqual([]);
    expect(decodePath("")).toEqual([]);
  });
});
