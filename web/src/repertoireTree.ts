// Pure drilling logic for the Openings explorer page
// (docs/future-improvements/openings-explorer.md): path resolution
// against the fetched tree, worst-line ranking, parity-based level
// labels, children-table row merging (played + unplayed book moves),
// URL path encode/decode, and small formatters. No fetching, no
// React; unit-tested in repertoireTree.test.ts. Named apart from
// openings.ts, which is the Dashboard's flat repertoire-table module
// (a different, unrelated aggregation over `OpeningStats`).

import { type Color, type RepertoireNode, score } from "./api.ts";

/**
 * Whether the arriving move at `ply` was the player's own, given the
 * color the tree was built for — the same parity rule behind
 * `OpeningStats.faced` and this page's "Your move"/"Their move"
 * labels (docs/future-improvements/openings-explorer.md "Parity"):
 * White's moves land on odd plies, Black's on even ones.
 */
export function isPlayerPly(ply: number, color: Color): boolean {
  return color === "white" ? ply % 2 === 1 : ply % 2 === 0;
}

/** Level label for a table of children at `childPly` — "Your move" if
 *  that ply is the player's own, else "Their move". */
export function levelLabel(
  childPly: number,
  color: Color,
): "Your move" | "Their move" {
  return isPlayerPly(childPly, color) ? "Your move" : "Their move";
}

/**
 * Resolves `path` (a sequence of SAN moves from the root) against the
 * tree, returning the chain of nodes from the root up to as far as
 * the path matches. Stops at the first move that isn't a child of the
 * current node, so the result may be shorter than `path` — ordinary
 * drilling always builds `path` by clicking an existing child (so it
 * fully resolves); a path restored from the URL may not (stale link,
 * pruned by `min_games`, or a color swap), which is what
 * `validatePath` below checks for.
 */
export function resolvePath(
  root: RepertoireNode,
  path: readonly string[],
): RepertoireNode[] {
  const chain = [root];
  let current = root;
  for (const san of path) {
    const next = current.children.find((child) => child.san === san);
    if (next === undefined) {
      return chain;
    }
    chain.push(next);
    current = next;
  }
  return chain;
}

/**
 * Validates a candidate path (typically restored from `?path=`)
 * against the tree: returns it unchanged when every move resolves,
 * or `[]` (the root) otherwise. The URL-restore contract falls back
 * to the root entirely rather than the deepest valid prefix, so a
 * stale deep link never strands the page on an arbitrary partial
 * line.
 */
export function validatePath(
  root: RepertoireNode,
  path: readonly string[],
): string[] {
  const chain = resolvePath(root, path);
  return chain.length - 1 === path.length ? [...path] : [];
}

export type WorstLine = {
  /** SAN path from the root to this node (inclusive) — jump target. */
  path: string[];
  node: RepertoireNode;
  impact: number;
};

/**
 * Top `limit` player-level nodes ranked by impact = games x avg loss
 * (docs/future-improvements/openings-explorer.md "worst lines" —
 * "which line do I fix first"). Nodes with no analyzed games
 * (`avg_cp_loss === null`) are skipped rather than sorting as zero
 * impact, since that would rank an unanalyzed line above a genuinely
 * cheap one. Ties break by more games first (more signal), then the
 * path itself for a deterministic order.
 */
export function worstLines(
  root: RepertoireNode,
  color: Color,
  limit = 5,
): WorstLine[] {
  const found: WorstLine[] = [];
  const walk = (node: RepertoireNode, path: string[]): void => {
    if (node.avg_cp_loss !== null && isPlayerPly(node.ply, color)) {
      found.push({
        path,
        node,
        impact: node.record.games * node.avg_cp_loss,
      });
    }
    for (const child of node.children) {
      walk(child, [...path, child.san]);
    }
  };
  walk(root, []);
  found.sort(
    (a, b) =>
      b.impact - a.impact ||
      b.node.record.games - a.node.record.games ||
      a.path.join(",").localeCompare(b.path.join(",")),
  );
  return found.slice(0, limit);
}

/**
 * One row of the children table: either a played child (real games
 * through it) or an unplayed book continuation — the "still to learn"
 * rows. `name`/`eco` fall back to the current node's own when the
 * child or book move carries none of its own (docs/future-
 * improvements/openings-explorer.md: "display inherits").
 */
export type ChildRow = {
  played: boolean;
  san: string;
  name: string | null;
  eco: string | null;
  games: number;
  wins: number;
  losses: number;
  draws: number;
  avgEvalCp: number | null;
  avgCpLoss: number | null;
  inBook: boolean;
  exits: number;
  /** The real node to drill into; `null` for an unplayed book move,
   *  which has no games and so no node of its own. */
  node: RepertoireNode | null;
};

function inheritedLabel(
  ownName: string | null,
  ownEco: string | null,
  parent: Pick<RepertoireNode, "name" | "eco">,
): { name: string | null; eco: string | null } {
  return ownName === null
    ? { name: parent.name, eco: parent.eco }
    : { name: ownName, eco: ownEco };
}

/**
 * Children-table rows for `node`: every played child, plus the book's
 * unplayed continuations (`played: false`) that aren't already one of
 * those children. A book move is `played` exactly when it already has
 * a corresponding child (server-side, per docs/future-improvements/
 * openings-explorer.md), so filtering to `!played` is normally enough
 * on its own — the extra SAN check just makes the no-duplicate
 * guarantee hold even if that invariant is ever violated.
 */
export function childRows(node: RepertoireNode): ChildRow[] {
  const playedSans = new Set(node.children.map((child) => child.san));

  const played: ChildRow[] = node.children.map((child) => {
    const label = inheritedLabel(child.name, child.eco, node);
    return {
      played: true,
      san: child.san,
      name: label.name,
      eco: label.eco,
      games: child.record.games,
      wins: child.record.wins,
      losses: child.record.losses,
      draws: child.record.draws,
      avgEvalCp: child.avg_eval_cp,
      avgCpLoss: child.avg_cp_loss,
      inBook: child.in_book,
      exits: child.exits,
      node: child,
    };
  });

  const unplayed: ChildRow[] = node.book_moves
    .filter((move) => !move.played && !playedSans.has(move.san))
    .map((move) => {
      const label = inheritedLabel(move.name, move.eco, node);
      return {
        played: false,
        san: move.san,
        name: label.name,
        eco: label.eco,
        games: 0,
        wins: 0,
        losses: 0,
        draws: 0,
        avgEvalCp: null,
        avgCpLoss: null,
        inBook: true,
        exits: 0,
        node: null,
      };
    });

  return [...played, ...unplayed];
}

/** Score % for a row's W/L/D — "—" with no games. */
export function formatScore(record: {
  games: number;
  wins: number;
  losses: number;
  draws: number;
}): string {
  return record.games === 0 ? "—" : `${Math.round(score(record) * 100)}%`;
}

/** Signed pawns to two decimals, or "—" when no analyzed game reaches
 *  the node — `avg_eval_cp` arrives already player-POV, sign-flipped
 *  server-side for Black. */
export function formatAvgEval(cp: number | null): string {
  if (cp === null) {
    return "—";
  }
  const pawns = cp / 100;
  const magnitude = Math.abs(pawns).toFixed(2);
  if (magnitude === "0.00") {
    return "0.00";
  }
  return pawns > 0 ? `+${magnitude}` : `-${magnitude}`;
}

/** Avg cp lost by the arriving mover, to one decimal, or "—" when
 *  nothing analyzed reaches the node. Always a magnitude (a cost), so
 *  unlike `formatAvgEval` it carries no sign. */
export function formatAvgLoss(loss: number | null): string {
  return loss === null ? "—" : loss.toFixed(1);
}

/** One SAN move, PGN move-text style: White's move gets the move
 *  number prefix ("1.e4"), Black's follows bare ("c5") — `ply` is
 *  1-based position along the path (1 = the first move played,
 *  White's, regardless of which color's repertoire the tree is). */
export function plyLabel(ply: number, san: string): string {
  const moveNumber = Math.ceil(ply / 2);
  return ply % 2 === 1 ? `${moveNumber}.${san}` : san;
}

/** A path as one line of move-number notation, e.g.
 *  `["e4", "c5", "Nf3"]` -> `"1.e4 c5 2.Nf3"` — used for the
 *  worst-lines strip and the breadcrumb. */
export function formatLine(path: readonly string[]): string {
  return path.map((san, index) => plyLabel(index + 1, san)).join(" ");
}

/**
 * Encodes a drill path for the `?path=` query param: each SAN move is
 * `encodeURIComponent`-escaped — SAN can contain `+` (check) or `#`
 * (checkmate), both otherwise special in a query string — then joined
 * with commas. SAN itself never contains a comma, so the join is
 * unambiguous.
 */
export function encodePath(path: readonly string[]): string {
  return path.map((san) => encodeURIComponent(san)).join(",");
}

/** Inverse of `encodePath`; a missing or empty param decodes to the
 *  root (`[]`). */
export function decodePath(raw: string | null | undefined): string[] {
  if (raw === null || raw === undefined || raw === "") {
    return [];
  }
  return raw.split(",").map((segment) => decodeURIComponent(segment));
}
