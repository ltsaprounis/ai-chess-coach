import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, score } from "../api.ts";
import BarChart from "../components/BarChart.tsx";
import { JUDGMENT_COLORS } from "../components/chartTheme.ts";
import Layout from "../components/Layout.tsx";
import MonthlyActivityChart from "../components/MonthlyActivityChart.tsx";
import RatingChart from "../components/RatingChart.tsx";
import { groupByFamily } from "../openings.ts";
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

// Time windows the dashboard can scope to; `days: null` is all-time.
const WINDOWS = [
  { label: "All time", days: null },
  { label: "Last 30 days", days: 30 },
  { label: "Last 90 days", days: 90 },
  { label: "Last 6 months", days: 182 },
  { label: "Last year", days: 365 },
] as const;

const DAY_SECONDS = 86_400;
const ALL_CLASSES = "all";

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
  const [windowDays, setWindowDays] = useState<number | null>(null);
  const [pickedClass, setPickedClass] = useState<string | null>(null);
  const [minGames, setMinGames] = useState(5);

  // Cutoff recomputed only when the window changes, so it stays stable
  // across renders (a fresh Date.now() each render would thrash the
  // report/openings query keys).
  const since = useMemo(
    () =>
      windowDays === null
        ? undefined
        : Math.floor(Date.now() / 1000) - windowDays * DAY_SECONDS,
    [windowDays],
  );

  const games = useQuery({
    queryKey: ["allGames", username],
    queryFn: () => api.allGames(username),
  });

  // The whole games archive is fetched once; the window is applied
  // client-side, then the time control on top of it.
  const windowByTime = useMemo(() => {
    const all = games.data ?? [];
    return since === undefined
      ? all
      : all.filter((game) => game.end_time >= since);
  }, [games.data, since]);

  // Time controls present in the window, most-played first.
  const classOptions = useMemo(
    () => latestRatings(windowByTime),
    [windowByTime],
  );

  // Resolve the selected control: explicit "all", a control that's
  // present, else the most-played one — so the default view mixes
  // controls only when the user asks for it.
  const timeClass =
    pickedClass === ALL_CLASSES
      ? ALL_CLASSES
      : (classOptions.find((entry) => entry.timeClass === pickedClass)
          ?.timeClass ??
        classOptions[0]?.timeClass ??
        ALL_CLASSES);
  const classParam = timeClass === ALL_CLASSES ? undefined : timeClass;

  const openings = useQuery({
    queryKey: ["openings", username, windowDays, timeClass],
    queryFn: () => api.openings(username, { since, time_class: classParam }),
  });
  const report = useQuery({
    queryKey: ["report", username, windowDays, timeClass],
    queryFn: () => api.report(username, { since, time_class: classParam }),
  });

  const scopedGames = useMemo(
    () =>
      classParam === undefined
        ? windowByTime
        : windowByTime.filter((game) => game.time_class === classParam),
    [windowByTime, classParam],
  );

  const stats = useMemo(
    () => ({
      overall: tally(scopedGames),
      byColor: tallyByColor(scopedGames),
      ratings: latestRatings(scopedGames),
      months: monthlyActivity(scopedGames),
    }),
    [scopedGames],
  );

  const series = useMemo(
    () =>
      classParam === undefined ? [] : ratingSeries(scopedGames, classParam),
    [scopedGames, classParam],
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

  // Collapse the fine ECO variations into families, keep those with a
  // meaningful sample, and surface the worst-scoring first.
  const families = useMemo(
    () =>
      groupByFamily(openings.data ?? [])
        .filter((family) => family.games >= minGames)
        .sort((a, b) => score(a) - score(b) || b.games - a.games),
    [openings.data, minGames],
  );

  const hasAnyGames = games.isSuccess && (games.data?.length ?? 0) > 0;
  const hasScopedGames = stats.overall.games > 0;

  return (
    <Layout username={username}>
      <h1>{username}'s dashboard</h1>

      {games.isPending && <p>Loading games…</p>}
      {games.isError && <p role="alert">{games.error.message}</p>}
      {games.isSuccess && !hasAnyGames && (
        <p>
          No games stored yet — <Link to="/">sync this player</Link> from the
          home page first.
        </p>
      )}

      {hasAnyGames && (
        <div className="filters">
          <label>
            Time window{" "}
            <select
              aria-label="time window"
              value={windowDays ?? ""}
              onChange={(event) =>
                setWindowDays(
                  event.target.value === "" ? null : Number(event.target.value),
                )
              }
            >
              {WINDOWS.map((window) => (
                <option key={window.label} value={window.days ?? ""}>
                  {window.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Time control{" "}
            <select
              aria-label="time control"
              value={timeClass}
              onChange={(event) => setPickedClass(event.target.value)}
            >
              <option value={ALL_CLASSES}>All classes</option>
              {classOptions.map((entry) => (
                <option key={entry.timeClass} value={entry.timeClass}>
                  {entry.timeClass} ({entry.games})
                </option>
              ))}
            </select>
          </label>
          <span className="agent-note">
            {stats.overall.games} game{stats.overall.games === 1 ? "" : "s"}
            {classParam !== undefined ? ` · ${classParam}` : " · all classes"}
          </span>
        </div>
      )}

      {hasAnyGames && !hasScopedGames && (
        <p className="panel-empty">
          No games match these filters — widen the window or time control.
        </p>
      )}

      {hasScopedGames && (
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

      {hasScopedGames && classParam !== undefined && series.length > 0 && (
        <section>
          <h2>Rating over time</h2>
          <RatingChart
            points={series}
            label={`${classParam} rating over time`}
          />
        </section>
      )}

      {hasScopedGames && (
        <section>
          <h2>Monthly activity</h2>
          <MonthlyActivityChart data={stats.months} />
        </section>
      )}

      {hasScopedGames && (
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
        <p>
          Openings grouped into families, worst-scoring first — the ones to work
          on. Avg CP loss covers the analyzed games only.
        </p>
        <div className="filters">
          <label>
            Min games{" "}
            <input
              type="number"
              min={1}
              className="limit-input"
              aria-label="minimum games per family"
              value={minGames}
              onChange={(event) =>
                setMinGames(Math.max(1, Number(event.target.value) || 1))
              }
            />
          </label>
          {openings.isSuccess && (
            <span className="agent-note">
              {families.length} famil{families.length === 1 ? "y" : "ies"} with{" "}
              {minGames}+ games
            </span>
          )}
        </div>

        {openings.isPending && <p>Loading…</p>}
        {openings.isError && <p role="alert">{openings.error.message}</p>}
        {openings.isSuccess && openings.data.length === 0 && (
          <p>No classified games match these filters.</p>
        )}
        {openings.isSuccess &&
          openings.data.length > 0 &&
          families.length === 0 && (
            <p className="panel-empty">
              No opening family has {minGames}+ games — lower the threshold.
            </p>
          )}
        {families.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Opening family</th>
                  <th>Games</th>
                  <th>Analyzed</th>
                  <th>W-L-D</th>
                  <th>Score</th>
                  <th>Avg CP loss</th>
                </tr>
              </thead>
              <tbody>
                {families.map((family) => (
                  <tr key={family.family}>
                    <td>{family.family}</td>
                    <td>{family.games}</td>
                    <td>{family.analyzedGames}</td>
                    <td>
                      {family.wins}-{family.losses}-{family.draws}
                    </td>
                    <td>{Math.round(score(family) * 100)}%</td>
                    <td>{family.avgCpLoss ?? "—"}</td>
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
