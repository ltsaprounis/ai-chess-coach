import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type GameFilters, type GameSummary } from "../api.ts";
import { useAnalysisProgress } from "../useAnalysisProgress.ts";

export default function Games() {
  const { username = "" } = useParams();
  const [filters, setFilters] = useState<GameFilters>({});
  const [analyzing, setAnalyzing] = useState(false);
  const queryClient = useQueryClient();

  const games = useQuery({
    queryKey: ["games", username, filters],
    queryFn: () => api.games(username, filters),
  });

  const analyze = useMutation({
    mutationFn: () => api.analyze(username),
    onSuccess: (result) => {
      if (result.queued > 0) {
        setAnalyzing(true);
      }
    },
  });

  const progress = useAnalysisProgress(username, analyzing, () => {
    setAnalyzing(false);
    void queryClient.invalidateQueries({ queryKey: ["games"] });
    void queryClient.invalidateQueries({ queryKey: ["openings"] });
  });

  return (
    <main className="page">
      <p>
        <Link to="/">← change player</Link>
        {" · "}
        <Link to={`/players/${username}/dashboard`}>dashboard</Link>
      </p>
      <h1>{username}'s games</h1>

      <div className="filters">
        <select
          aria-label="result"
          value={filters.result ?? ""}
          onChange={(e) => setFilters({ ...filters, result: e.target.value })}
        >
          <option value="">any result</option>
          <option value="win">wins</option>
          <option value="loss">losses</option>
          <option value="draw">draws</option>
        </select>
        <select
          aria-label="time class"
          value={filters.time_class ?? ""}
          onChange={(e) =>
            setFilters({ ...filters, time_class: e.target.value })
          }
        >
          <option value="">any time class</option>
          <option value="bullet">bullet</option>
          <option value="blitz">blitz</option>
          <option value="rapid">rapid</option>
          <option value="daily">daily</option>
        </select>
        <button
          type="button"
          disabled={analyzing || analyze.isPending}
          onClick={() => analyze.mutate()}
        >
          {analyzing ? "Analyzing…" : "Analyze all"}
        </button>
      </div>

      {analyze.isError && <p role="alert">{analyze.error.message}</p>}
      {progress && (
        <p className="progress-row">
          <progress value={progress.gamesDone} max={progress.gamesTotal} />{" "}
          {progress.gamesDone}/{progress.gamesTotal} games
          {progress.currentPly !== undefined
            ? ` — current game ply ${progress.currentPly}/${progress.totalPlies}`
            : ""}
          {progress.failed ? " — run failed, see server logs" : ""}
        </p>
      )}

      {games.isPending && <p>Loading…</p>}
      {games.isError && <p role="alert">{games.error.message}</p>}
      {games.isSuccess && games.data.length === 0 && (
        <p>No games stored yet.</p>
      )}
      {games.isSuccess && games.data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Color</th>
              <th>Opponent</th>
              <th>Result</th>
              <th>Time</th>
              <th>Opening</th>
              <th>Accuracy</th>
              <th>Analyzed</th>
            </tr>
          </thead>
          <tbody>
            {games.data.map((game: GameSummary) => (
              <tr key={game.id}>
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
      )}
    </main>
  );
}
