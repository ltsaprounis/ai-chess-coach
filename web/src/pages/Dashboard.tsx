import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, score, sortWorstFirst } from "../api.ts";
import BarChart from "../components/BarChart.tsx";
import { JUDGMENT_COLORS } from "../components/chartTheme.ts";
import Layout from "../components/Layout.tsx";
import MonthlyActivityChart from "../components/MonthlyActivityChart.tsx";
import RatingChart from "../components/RatingChart.tsx";
import {
  latestRatings,
  monthlyActivity,
  ratingSeries,
  type Tally,
  tally,
  tallyByColor,
} from "../stats.ts";

const PHASES = ["opening", "middlegame", "endgame"] as const;
const JUDGMENTS = ["best", "good", "inaccuracy", "mistake", "blunder"] as const;

const percent = (fraction: number): string => `${Math.round(fraction * 100)}%`;

const colorRate = (record: Tally): string =>
  record.games === 0 ? "—" : percent(score(record));

function Tile({ value, label }: { value: string | number; label: string }) {
  return (
    <div className="tile">
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
    </div>
  );
}

export default function Dashboard() {
  const { username = "" } = useParams();
  const [pickedClass, setPickedClass] = useState<string | null>(null);

  const games = useQuery({
    queryKey: ["allGames", username],
    queryFn: () => api.allGames(username),
  });
  const openings = useQuery({
    queryKey: ["openings", username],
    queryFn: () => api.openings(username),
  });
  const report = useQuery({
    queryKey: ["report", username],
    queryFn: () => api.report(username),
  });

  const stats = useMemo(() => {
    const list = games.data ?? [];
    return {
      overall: tally(list),
      byColor: tallyByColor(list),
      ratings: latestRatings(list),
      months: monthlyActivity(list),
    };
  }, [games.data]);

  // Default the rating chart to the most-played class.
  const selectedClass =
    stats.ratings.find((entry) => entry.timeClass === pickedClass)?.timeClass ??
    stats.ratings[0]?.timeClass ??
    null;
  const series = useMemo(
    () =>
      selectedClass === null
        ? []
        : ratingSeries(games.data ?? [], selectedClass),
    [games.data, selectedClass],
  );

  const analyzed =
    report.data !== undefined && report.data.games_analyzed > 0
      ? report.data
      : null;
  const perGame = (count: number | undefined): string =>
    analyzed === null
      ? "—"
      : ((count ?? 0) / analyzed.games_analyzed).toFixed(1);
  const phaseData =
    analyzed === null
      ? []
      : PHASES.filter(
          (phase) => analyzed.acpl_by_phase[phase] !== undefined,
        ).map((phase) => ({
          label: phase,
          value: analyzed.acpl_by_phase[phase] ?? 0,
        }));
  const judgmentData =
    analyzed === null
      ? []
      : JUDGMENTS.map((judgment) => ({
          label: judgment,
          value: analyzed.judgment_counts[judgment] ?? 0,
          color: JUDGMENT_COLORS[judgment],
        }));

  const hasGames = stats.overall.games > 0;

  return (
    <Layout username={username}>
      <h1>{username}'s dashboard</h1>

      {games.isPending && <p>Loading games…</p>}
      {games.isError && <p role="alert">{games.error.message}</p>}
      {games.isSuccess && !hasGames && (
        <p>
          No games stored yet — <Link to="/">sync this player</Link> from the
          home page first.
        </p>
      )}

      {hasGames && (
        <section className="tiles">
          <Tile value={stats.overall.games} label="games" />
          <Tile
            value={`${stats.overall.wins}-${stats.overall.losses}-${stats.overall.draws}`}
            label={`record · ${percent(score(stats.overall))} win rate`}
          />
          <Tile
            value={colorRate(stats.byColor.white)}
            label={`win rate as white (${stats.byColor.white.games})`}
          />
          <Tile
            value={colorRate(stats.byColor.black)}
            label={`win rate as black (${stats.byColor.black.games})`}
          />
          {stats.ratings.map((entry) => (
            <Tile
              key={entry.timeClass}
              value={entry.rating}
              label={`${entry.timeClass} rating`}
            />
          ))}
          {analyzed !== null && (
            <>
              <Tile
                value={analyzed.overall_acpl}
                label={`avg centipawn loss (${analyzed.games_analyzed} analyzed)`}
              />
              <Tile
                value={perGame(analyzed.judgment_counts.blunder)}
                label="blunders per game"
              />
              <Tile
                value={perGame(analyzed.judgment_counts.mistake)}
                label="mistakes per game"
              />
            </>
          )}
        </section>
      )}

      {hasGames && selectedClass !== null && (
        <section>
          <h2>Rating over time</h2>
          <div className="filters">
            <select
              aria-label="time class"
              value={selectedClass}
              onChange={(event) => setPickedClass(event.target.value)}
            >
              {stats.ratings.map((entry) => (
                <option key={entry.timeClass} value={entry.timeClass}>
                  {entry.timeClass} ({entry.games} games)
                </option>
              ))}
            </select>
          </div>
          <RatingChart
            points={series}
            label={`${selectedClass} rating over time`}
          />
        </section>
      )}

      {hasGames && (
        <section>
          <h2>Monthly activity</h2>
          <MonthlyActivityChart data={stats.months} />
        </section>
      )}

      {hasGames && (
        <section>
          <h2>Analysis</h2>
          {analyzed !== null ? (
            <div className="chart-row">
              <div>
                <h3>ACPL by phase</h3>
                <BarChart
                  data={phaseData}
                  label="average centipawn loss by phase"
                />
              </div>
              <div>
                <h3>Judgment distribution</h3>
                <BarChart data={judgmentData} label="moves per judgment" />
              </div>
            </div>
          ) : (
            <p>
              No engine analysis yet —{" "}
              <Link to={`/players/${username}/games`}>analyze some games</Link>{" "}
              to see ACPL and judgment breakdowns.
            </p>
          )}
        </section>
      )}

      <section>
        <h2>Repertoire — worst first</h2>
        <p>Sorted worst-scoring first — the openings to work on.</p>

        {openings.isPending && <p>Loading…</p>}
        {openings.isError && <p role="alert">{openings.error.message}</p>}
        {openings.isSuccess && openings.data.length === 0 && (
          <p>No classified games yet — sync this player first.</p>
        )}
        {openings.isSuccess && openings.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ECO</th>
                  <th>Opening</th>
                  <th>Games</th>
                  <th>W-L-D</th>
                  <th>Score</th>
                  <th>Avg CP loss</th>
                </tr>
              </thead>
              <tbody>
                {sortWorstFirst(openings.data).map((opening) => (
                  <tr key={`${opening.eco}-${opening.name}`}>
                    <td>{opening.eco}</td>
                    <td>{opening.name}</td>
                    <td>{opening.games}</td>
                    <td>
                      {opening.wins}-{opening.losses}-{opening.draws}
                    </td>
                    <td>{Math.round(score(opening) * 100)}%</td>
                    <td>{opening.avg_cp_loss ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </Layout>
  );
}
