import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, type GameSummary, score } from "../api.ts";
import Layout from "../components/Layout.tsx";
import SortableTh from "../components/SortableTh.tsx";
import { openingFamily } from "../openings.ts";
import { tally } from "../stats.ts";
import { useAnalysisProgress } from "../useAnalysisProgress.ts";
import { compareValues, useTableSort } from "../useTableSort.ts";

const PAGE_SIZE = 25;

type SortKey =
  | "end_time"
  | "color"
  | "opponent"
  | "result"
  | "time_class"
  | "opening"
  | "accuracy"
  | "analyzed";

const COLUMNS: { key: SortKey; label: string }[] = [
  { key: "end_time", label: "Date" },
  { key: "color", label: "Color" },
  { key: "opponent", label: "Opponent" },
  { key: "result", label: "Result" },
  { key: "time_class", label: "Time" },
  { key: "opening", label: "Opening" },
  { key: "accuracy", label: "Accuracy" },
  { key: "analyzed", label: "Analyzed" },
];

function sortValue(game: GameSummary, key: SortKey): string | number {
  switch (key) {
    case "end_time":
      return game.end_time;
    case "color":
      return game.color;
    case "opponent":
      return game.opponent.toLowerCase();
    case "result":
      return game.result;
    case "time_class":
      return game.time_class;
    case "opening":
      return game.opening?.name ?? "";
    case "accuracy":
      return game.accuracy ?? -1;
    case "analyzed":
      return game.analyzed ? 1 : 0;
  }
}

// Numeric columns read best high-to-low first; text columns A-to-Z.
const DEFAULT_DESC: ReadonlySet<SortKey> = new Set([
  "end_time",
  "accuracy",
  "analyzed",
]);

export default function Games() {
  const { username = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  // `family` (and an initial `time_class`) can arrive from a repertoire
  // drill-through on the dashboard.
  const family = searchParams.get("family") ?? "";

  const [result, setResult] = useState("");
  const [timeClass, setTimeClass] = useState(
    () => searchParams.get("time_class") ?? "",
  );
  const [analyzedFilter, setAnalyzedFilter] = useState("");
  const [opponent, setOpponent] = useState("");

  const { sortKey, sortDir, onSort } = useTableSort<SortKey>(
    "end_time",
    "desc",
    DEFAULT_DESC,
  );
  const [page, setPage] = useState(0);

  const [analyzing, setAnalyzing] = useState(false);
  const [limit, setLimit] = useState(100);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());

  // The whole archive, shared with the dashboard's cache; filtering,
  // sorting, and paging all happen client-side over it.
  const games = useQuery({
    queryKey: ["allGames", username],
    queryFn: () => api.allGames(username),
  });

  // Pull new games for this player from chess.com (incremental from the
  // latest stored game), then refresh everything derived from them.
  const sync = useMutation({
    mutationFn: () => api.sync(username),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["allGames"] });
      void queryClient.invalidateQueries({ queryKey: ["openings"] });
      void queryClient.invalidateQueries({ queryKey: ["report"] });
      void queryClient.invalidateQueries({ queryKey: ["players"] });
    },
  });

  const filtered = useMemo(() => {
    const needle = opponent.trim().toLowerCase();
    return (games.data ?? []).filter(
      (game) =>
        (result === "" || game.result === result) &&
        (timeClass === "" || game.time_class === timeClass) &&
        (analyzedFilter === "" || String(game.analyzed) === analyzedFilter) &&
        (family === "" || openingFamily(game.opening?.name ?? "") === family) &&
        (needle === "" || game.opponent.toLowerCase().includes(needle)),
    );
  }, [games.data, result, timeClass, analyzedFilter, family, opponent]);

  const sorted = useMemo(() => {
    const rows = [...filtered];
    rows.sort((a, b) => {
      const cmp = compareValues(sortValue(a, sortKey), sortValue(b, sortKey));
      const primary = sortDir === "asc" ? cmp : -cmp;
      return primary !== 0 ? primary : b.end_time - a.end_time;
    });
    return rows;
  }, [filtered, sortKey, sortDir]);

  const record = useMemo(() => tally(filtered), [filtered]);

  const pageCount = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const clampedPage = Math.min(page, pageCount - 1);
  const start = clampedPage * PAGE_SIZE;
  const pageGames = sorted.slice(start, start + PAGE_SIZE);

  // Any filter or sort change returns to the first page.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset on filter/sort change
  useEffect(() => {
    setPage(0);
  }, [result, timeClass, analyzedFilter, family, opponent, sortKey, sortDir]);

  const analyze = useMutation({
    mutationFn: () =>
      api.analyze(
        username,
        selected.size > 0 ? { gameIds: [...selected] } : { limit },
      ),
    onSuccess: (outcome) => {
      if (outcome.queued > 0) {
        setAnalyzing(true);
      }
      setSelected(new Set());
    },
  });

  const toggle = (id: string) =>
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });

  const pageIds = pageGames.map((game) => game.id);
  const allPageSelected =
    pageIds.length > 0 && pageIds.every((id) => selected.has(id));

  const progress = useAnalysisProgress(username, analyzing, () => {
    setAnalyzing(false);
    void queryClient.invalidateQueries({ queryKey: ["allGames"] });
    void queryClient.invalidateQueries({ queryKey: ["openings"] });
    void queryClient.invalidateQueries({ queryKey: ["report"] });
  });

  const clearFamily = () => {
    const next = new URLSearchParams(searchParams);
    next.delete("family");
    setSearchParams(next, { replace: true });
  };

  return (
    <Layout username={username}>
      <h1>{username}'s games</h1>

      <div className="games-toolbar">
        <button
          type="button"
          onClick={() => sync.mutate()}
          disabled={sync.isPending}
        >
          {sync.isPending ? "Syncing…" : "⟳ Sync new games"}
        </button>
        {sync.isSuccess && (
          <span className="agent-note">
            {sync.data.games_synced === 0
              ? "Already up to date"
              : `Synced ${sync.data.games_synced} new game${
                  sync.data.games_synced === 1 ? "" : "s"
                }`}
          </span>
        )}
        {sync.isError && <span role="alert">{sync.error.message}</span>}
      </div>

      <div className="filters">
        <select
          aria-label="result"
          value={result}
          onChange={(event) => setResult(event.target.value)}
        >
          <option value="">any result</option>
          <option value="win">wins</option>
          <option value="loss">losses</option>
          <option value="draw">draws</option>
        </select>
        <select
          aria-label="time class"
          value={timeClass}
          onChange={(event) => setTimeClass(event.target.value)}
        >
          <option value="">any time class</option>
          <option value="bullet">bullet</option>
          <option value="blitz">blitz</option>
          <option value="rapid">rapid</option>
          <option value="daily">daily</option>
        </select>
        <select
          aria-label="analyzed"
          value={analyzedFilter}
          onChange={(event) => setAnalyzedFilter(event.target.value)}
        >
          <option value="">any analysis</option>
          <option value="true">analyzed</option>
          <option value="false">not analyzed</option>
        </select>
        <input
          type="search"
          aria-label="search opponent"
          placeholder="opponent…"
          value={opponent}
          onChange={(event) => setOpponent(event.target.value)}
        />
      </div>

      {family !== "" && (
        <p className="games-summary">
          <span className="filter-chip">
            Opening: <strong>{family}</strong>
            <button
              type="button"
              className="chip-clear"
              aria-label="clear opening filter"
              onClick={clearFamily}
            >
              ✕
            </button>
          </span>
        </p>
      )}

      {games.isSuccess && (
        <p className="games-summary">
          {sorted.length} game{sorted.length === 1 ? "" : "s"} · {record.wins}-
          {record.losses}-{record.draws}
          {sorted.length > 0
            ? ` · ${Math.round(score(record) * 100)}% win rate`
            : ""}
        </p>
      )}

      <div className="analyze-bar">
        <span className="analyze-bar-label">Analyze</span>
        <label>
          latest{" "}
          <input
            type="number"
            min={1}
            aria-label="games to analyze"
            className="limit-input"
            value={limit}
            disabled={selected.size > 0}
            onChange={(event) =>
              setLimit(Math.max(1, Number(event.target.value) || 1))
            }
          />
        </label>
        <button
          type="button"
          className="btn-primary"
          disabled={analyzing || analyze.isPending}
          onClick={() => analyze.mutate()}
        >
          {analyzing
            ? "Analyzing…"
            : selected.size > 0
              ? `Analyze selected (${selected.size})`
              : `Analyze latest ${limit}`}
        </button>
      </div>

      {analyze.isError && <p role="alert">{analyze.error.message}</p>}
      {analyze.data && (
        <p>
          Queued {analyze.data.queued} game
          {analyze.data.queued === 1 ? "" : "s"}
          {analyze.data.remaining > 0
            ? ` — ${analyze.data.remaining} more still unanalyzed`
            : ""}
        </p>
      )}
      {progress && (
        <p className="progress-row">
          <progress value={progress.gamesDone} max={progress.gamesTotal} />{" "}
          {progress.gamesDone}/{progress.gamesTotal} games
          {progress.currentPly !== undefined
            ? ` — current game ply ${progress.currentPly}/${progress.totalPlies}`
            : ""}
          {progress.failed ? " — run failed, see server logs" : ""}
          {progress.streamLost
            ? " — progress stream lost; the run may have been interrupted. Re-analyze to continue."
            : ""}
        </p>
      )}

      {games.isPending && <p>Loading…</p>}
      {games.isError && <p role="alert">{games.error.message}</p>}
      {games.isSuccess && (games.data?.length ?? 0) === 0 && (
        <p>No games stored yet.</p>
      )}
      {games.isSuccess &&
        sorted.length === 0 &&
        (games.data?.length ?? 0) > 0 && (
          <p className="panel-empty">No games match these filters.</p>
        )}
      {sorted.length > 0 && (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>
                    <input
                      type="checkbox"
                      aria-label="select all games on this page"
                      checked={allPageSelected}
                      onChange={(event) =>
                        setSelected((previous) => {
                          const next = new Set(previous);
                          for (const id of pageIds) {
                            if (event.target.checked) {
                              next.add(id);
                            } else {
                              next.delete(id);
                            }
                          }
                          return next;
                        })
                      }
                    />
                  </th>
                  {COLUMNS.map((col) => (
                    <SortableTh
                      key={col.key}
                      column={col.key}
                      label={col.label}
                      sortKey={sortKey}
                      sortDir={sortDir}
                      onSort={onSort}
                    />
                  ))}
                </tr>
              </thead>
              <tbody>
                {pageGames.map((game: GameSummary) => (
                  <tr key={game.id}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`select game against ${game.opponent}`}
                        checked={selected.has(game.id)}
                        onChange={() => toggle(game.id)}
                      />
                    </td>
                    <td>
                      <Link to={`/games/${game.id}`}>
                        {new Date(game.end_time * 1000).toLocaleDateString()}
                      </Link>
                    </td>
                    <td>{game.color}</td>
                    <td>
                      {game.opponent} ({game.opponent_rating})
                    </td>
                    <td className={`result-${game.result}`}>{game.result}</td>
                    <td>{game.time_class}</td>
                    <td>{game.opening?.name ?? "—"}</td>
                    <td>{game.accuracy ?? "—"}</td>
                    <td>{game.analyzed ? "✓" : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="pagination">
            <button
              type="button"
              disabled={clampedPage === 0}
              onClick={() => setPage(clampedPage - 1)}
            >
              ‹ Prev
            </button>
            <span>
              {start + 1}–{Math.min(sorted.length, start + PAGE_SIZE)} of{" "}
              {sorted.length}
            </span>
            <button
              type="button"
              disabled={clampedPage >= pageCount - 1}
              onClick={() => setPage(clampedPage + 1)}
            >
              Next ›
            </button>
          </div>
        </>
      )}
    </Layout>
  );
}
