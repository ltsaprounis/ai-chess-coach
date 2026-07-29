import { useQuery } from "@tanstack/react-query";
import { Chess } from "chess.js";
import { useEffect, useMemo, useState } from "react";
import { Chessboard } from "react-chessboard";
import { useParams, useSearchParams } from "react-router-dom";
import { api, type Color, score } from "../api.ts";
import Layout from "../components/Layout.tsx";
import LiveEvalPanel from "../components/LiveEvalPanel.tsx";
import SortableTh from "../components/SortableTh.tsx";
import StatsFilters from "../components/StatsFilters.tsx";
import {
  type ChildRow,
  childRows,
  decodePath,
  encodePath,
  formatAvgEval,
  formatAvgLoss,
  formatLine,
  formatScore,
  levelLabel,
  plyLabel,
  resolvePath,
  validatePath,
  worstLines,
} from "../repertoireTree.ts";
import { useLiveEval } from "../useLiveEval.ts";
import { useStatsFilters } from "../useStatsFilters.ts";
import { compareValues, useTableSort } from "../useTableSort.ts";

const WORST_LINES_LIMIT = 5;

type ChildSortKey = "games" | "score" | "avgEval" | "avgLoss";

// Only the count column reads best high-to-low first by default; the
// rate/loss columns start ascending (best first), mirroring
// RepertoireTable's convention on the Dashboard.
const CHILD_DESC: ReadonlySet<ChildSortKey> = new Set(["games"]);

function childSortValue(row: ChildRow, key: ChildSortKey): number {
  switch (key) {
    case "games":
      return row.games;
    case "score":
      return row.games === 0 ? -1 : score(row);
    case "avgEval":
      return row.avgEvalCp ?? Number.NEGATIVE_INFINITY;
    case "avgLoss":
      return row.avgCpLoss ?? Number.POSITIVE_INFINITY;
  }
}

/** Parses `?color=` for the initial toggle; anything but "black"
 *  means White, so a missing or stale param defaults sensibly. */
function colorFromParams(searchParams: URLSearchParams): Color {
  return searchParams.get("color") === "black" ? "black" : "white";
}

export default function Openings() {
  const { username = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const [showOneOffs, setShowOneOffs] = useState(false);
  const [live, setLive] = useState(false);

  const color = colorFromParams(searchParams);
  const rawPath = decodePath(searchParams.get("path"));

  const games = useQuery({
    queryKey: ["allGames", username],
    queryFn: () => api.allGames(username),
  });

  const {
    windowDays,
    setWindowDays,
    setPickedClass,
    classOptions,
    timeClass,
    since,
    classParam,
  } = useStatsFilters(games.data ?? []);

  const tree = useQuery({
    queryKey: [
      "openingsTree",
      username,
      color,
      windowDays,
      timeClass,
      showOneOffs,
    ],
    queryFn: () =>
      api.openingsTree(username, {
        color,
        since,
        time_class: classParam,
        min_games: showOneOffs ? 1 : undefined,
      }),
  });

  const validPath = tree.data ? validatePath(tree.data.root, rawPath) : [];

  // A path restored from the URL that no longer resolves against the
  // freshly loaded tree (a stale link, a color swap, or a line pruned
  // by min_games) falls back to the root for display; this also drops
  // it from the URL so the dead deep link doesn't linger.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-validate only when the tree itself (re)loads
  useEffect(() => {
    if (!tree.data) {
      return;
    }
    if (validatePath(tree.data.root, rawPath).length !== rawPath.length) {
      const next = new URLSearchParams(searchParams);
      next.delete("path");
      setSearchParams(next, { replace: true });
    }
  }, [tree.data]);

  const goToPath = (nextPath: string[]): void => {
    const next = new URLSearchParams(searchParams);
    next.set("color", color);
    if (nextPath.length > 0) {
      next.set("path", encodePath(nextPath));
    } else {
      next.delete("path");
    }
    setSearchParams(next, { replace: true });
  };

  const setColor = (nextColor: Color): void => {
    const next = new URLSearchParams(searchParams);
    next.set("color", nextColor);
    next.delete("path");
    setSearchParams(next, { replace: true });
  };

  const chain = tree.data ? resolvePath(tree.data.root, validPath) : [];
  const currentNode = chain[chain.length - 1] ?? tree.data?.root ?? null;

  const rows = useMemo(
    () => (currentNode ? childRows(currentNode) : []),
    [currentNode],
  );

  const childSort = useTableSort<ChildSortKey>("games", "desc", CHILD_DESC);
  const sortedRows = useMemo(() => {
    const copy = [...rows];
    copy.sort((a, b) => {
      const cmp = compareValues(
        childSortValue(a, childSort.sortKey),
        childSortValue(b, childSort.sortKey),
      );
      const primary = childSort.sortDir === "asc" ? cmp : -cmp;
      return primary !== 0 ? primary : b.games - a.games;
    });
    return copy;
  }, [rows, childSort.sortKey, childSort.sortDir]);

  const worst = useMemo(
    () =>
      tree.data ? worstLines(tree.data.root, color, WORST_LINES_LIMIT) : [],
    [tree.data, color],
  );

  const fen = useMemo(() => {
    const chess = new Chess();
    for (const san of validPath) {
      chess.move(san);
    }
    return chess.fen();
  }, [validPath]);

  const liveEval = useLiveEval(live ? fen : null);

  const hasGames = (tree.data?.games ?? 0) > 0;

  return (
    <Layout username={username}>
      <h1>{username}'s openings</h1>
      <p>
        Drill from your first move into any line: games, score, engine eval, and
        whether you're still in book at every step. Unplayed book continuations
        are greyed in — the moves you haven't tried yet.
      </p>

      {games.isPending && <p>Loading games…</p>}
      {games.isError && <p role="alert">{games.error.message}</p>}

      <div className="filters">
        <StatsFilters
          windowDays={windowDays}
          setWindowDays={setWindowDays}
          timeClass={timeClass}
          setPickedClass={setPickedClass}
          classOptions={classOptions}
        />
        <div className="color-toggle">
          <button
            type="button"
            aria-pressed={color === "white"}
            className={
              color === "white" ? "color-toggle-btn active" : "color-toggle-btn"
            }
            onClick={() => setColor("white")}
          >
            White
          </button>
          <button
            type="button"
            aria-pressed={color === "black"}
            className={
              color === "black" ? "color-toggle-btn active" : "color-toggle-btn"
            }
            onClick={() => setColor("black")}
          >
            Black
          </button>
        </div>
        <label>
          <input
            type="checkbox"
            checked={showOneOffs}
            onChange={(event) => setShowOneOffs(event.target.checked)}
          />{" "}
          Show one-off lines
        </label>
      </div>

      {tree.isPending && <p>Loading repertoire…</p>}
      {tree.isError && <p role="alert">{tree.error.message}</p>}

      {tree.isSuccess && !hasGames && (
        <p className="panel-empty">
          No {color} games match these filters — widen the window, time control,
          or try the other color.
        </p>
      )}

      {tree.isSuccess && hasGames && tree.data && (
        <>
          <p className="agent-note">
            {tree.data.analyzed} of {tree.data.games} games in this window are
            analyzed.
          </p>

          <section>
            <h2>Worst lines</h2>
            {worst.length === 0 ? (
              <p className="panel-empty">
                Not enough analyzed repetition yet to rank lines — play (and
                analyze) more games in a line to see it here.
              </p>
            ) : (
              <ol className="worst-lines">
                {worst.map((entry) => (
                  <li key={entry.path.join(",")}>
                    <button
                      type="button"
                      className="worst-line-card"
                      onClick={() => goToPath(entry.path)}
                    >
                      <span className="worst-line-moves">
                        {formatLine(entry.path)}
                      </span>
                      <span className="worst-line-stats">
                        {entry.node.record.games} games ·{" "}
                        {formatScore(entry.node.record)} · avg loss{" "}
                        {formatAvgLoss(entry.node.avg_cp_loss)}
                      </span>
                    </button>
                  </li>
                ))}
              </ol>
            )}
          </section>

          <nav className="breadcrumb" aria-label="drill path">
            <button
              type="button"
              className="breadcrumb-item"
              disabled={validPath.length === 0}
              onClick={() => goToPath([])}
            >
              Start
            </button>
            {validPath.map((san, index) => (
              <span
                key={validPath.slice(0, index + 1).join(",")}
                className="breadcrumb-crumb"
              >
                <span className="breadcrumb-sep" aria-hidden="true">
                  ›
                </span>
                <button
                  type="button"
                  className="breadcrumb-item"
                  disabled={index === validPath.length - 1}
                  onClick={() => goToPath(validPath.slice(0, index + 1))}
                >
                  {plyLabel(index + 1, san)}
                </button>
              </span>
            ))}
          </nav>

          <div className="game-layout">
            <div className="board-column">
              <Chessboard
                options={{
                  position: fen,
                  boardOrientation: color,
                  allowDragging: false,
                  animationDurationInMs: 150,
                }}
              />

              <section className="panel engine-panel">
                <h2>Engine</h2>
                <label className="live-toggle">
                  <input
                    type="checkbox"
                    checked={live}
                    onChange={(event) => setLive(event.target.checked)}
                  />
                  Live engine
                </label>
                {live ? (
                  <LiveEvalPanel state={liveEval} fen={fen} />
                ) : (
                  <p className="panel-empty">
                    Turn on live engine to see the engine's top candidate moves
                    for the current position.
                  </p>
                )}
              </section>
            </div>

            <div className="side-column">
              <section>
                <h2>
                  {currentNode ? levelLabel(currentNode.ply + 1, color) : ""}
                </h2>
                {sortedRows.length === 0 ? (
                  <p className="panel-empty">
                    No moves recorded past this point.
                  </p>
                ) : (
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Move</th>
                          <th>Opening</th>
                          <SortableTh
                            column="games"
                            label="Games"
                            sortKey={childSort.sortKey}
                            sortDir={childSort.sortDir}
                            onSort={childSort.onSort}
                          />
                          <SortableTh
                            column="score"
                            label="Score"
                            sortKey={childSort.sortKey}
                            sortDir={childSort.sortDir}
                            onSort={childSort.onSort}
                          />
                          <SortableTh
                            column="avgEval"
                            label="Avg eval"
                            sortKey={childSort.sortKey}
                            sortDir={childSort.sortDir}
                            onSort={childSort.onSort}
                          />
                          <SortableTh
                            column="avgLoss"
                            label="Avg loss"
                            sortKey={childSort.sortKey}
                            sortDir={childSort.sortDir}
                            onSort={childSort.onSort}
                          />
                          <th>Book</th>
                          <th>Exits</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sortedRows.map((row) => (
                          <tr
                            key={row.san}
                            className={row.played ? undefined : "row-unplayed"}
                          >
                            <td>
                              {row.played ? (
                                <button
                                  type="button"
                                  className="link-button"
                                  onClick={() =>
                                    goToPath([...validPath, row.san])
                                  }
                                >
                                  {row.san}
                                </button>
                              ) : (
                                <>
                                  {row.san}{" "}
                                  <span className="unplayed-badge">
                                    to learn
                                  </span>
                                </>
                              )}
                            </td>
                            <td>
                              {row.name ?? "—"}
                              {row.eco ? ` (${row.eco})` : ""}
                            </td>
                            <td>{row.games}</td>
                            <td>{formatScore(row)}</td>
                            <td>{formatAvgEval(row.avgEvalCp)}</td>
                            <td>{formatAvgLoss(row.avgCpLoss)}</td>
                            <td>
                              {row.inBook ? (
                                <span className="book-badge">book</span>
                              ) : (
                                "—"
                              )}
                            </td>
                            <td>{row.exits > 0 ? row.exits : "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </div>
          </div>
        </>
      )}
    </Layout>
  );
}
