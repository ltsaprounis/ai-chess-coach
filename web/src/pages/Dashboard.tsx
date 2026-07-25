import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type PlayerReport, score } from "../api.ts";
import type { BarDatum } from "../components/BarChart.tsx";
import BarChart from "../components/BarChart.tsx";
import { JUDGMENT_COLORS } from "../components/chartTheme.ts";
import Layout from "../components/Layout.tsx";
import MonthlyActivityChart from "../components/MonthlyActivityChart.tsx";
import MonthlyMetricChart from "../components/MonthlyMetricChart.tsx";
import RatingChart from "../components/RatingChart.tsx";
import RepertoireTable from "../components/RepertoireTable.tsx";
import StatsFilters from "../components/StatsFilters.tsx";
import { groupByFamily, type OpeningFamily } from "../openings.ts";
import {
  latestRatings,
  monthlyActivity,
  ratingSeries,
  splitPhases,
  type Tally,
  tally,
  tallyByColor,
} from "../stats.ts";
import { useStatsFilters } from "../useStatsFilters.ts";

const JUDGMENTS = ["best", "good", "inaccuracy", "mistake", "blunder"] as const;

const percent = (fraction: number): string => `${Math.round(fraction * 100)}%`;

const colorRate = (record: Tally): string =>
  record.games === 0 ? "—" : percent(score(record));

/** A win/loss/draw record's score, or "—" with no games — shared by
 *  the opponent-strength tiles below. */
const recordRate = (record: {
  games: number;
  wins: number;
  losses: number;
  draws: number;
}): string => (record.games === 0 ? "—" : percent(score(record)));

/** Rounds to the nearest integer, prefixing "+" only for a genuinely
 *  positive result — a value that rounds to zero from either side
 *  (e.g. -0.4 or 0.4) reads as plain "0", not an arbitrarily signed
 *  zero. */
const signedRound = (value: number): string => {
  const rounded = Math.round(value);
  return rounded > 0 ? `+${rounded}` : `${rounded}`;
};

function Tile({ value, label }: { value: string | number; label: string }) {
  return (
    <div className="tile">
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
    </div>
  );
}

/** Termination rows grouped by result, each row's share of that
 *  result's games — "38% of losses on the clock" needs the loss total
 *  as its denominator, not the whole game count. */
function terminationRows(
  report: PlayerReport,
): (PlayerReport["terminations"][number] & { share: number })[] {
  const totals = new Map<string, number>();
  for (const row of report.terminations) {
    totals.set(row.result, (totals.get(row.result) ?? 0) + row.games);
  }
  return [...report.terminations]
    .sort((a, b) => a.result.localeCompare(b.result) || b.games - a.games)
    .map((row) => ({
      ...row,
      share: Math.round((row.games / (totals.get(row.result) ?? 1)) * 100),
    }));
}

function errorExampleLabel(
  pattern: PlayerReport["error_patterns"][number],
): string {
  const parts: string[] = [];
  if (
    pattern.example_end_time !== null &&
    pattern.example_end_time !== undefined
  ) {
    parts.push(new Date(pattern.example_end_time * 1000).toLocaleDateString());
  }
  if (
    pattern.example_move_number !== null &&
    pattern.example_move_number !== undefined
  ) {
    parts.push(`move ${pattern.example_move_number}`);
  }
  return parts.length > 0 ? parts.join(" · ") : "view game";
}

export default function Dashboard() {
  const { username = "" } = useParams();
  const [minGames, setMinGames] = useState(5);

  const games = useQuery({
    queryKey: ["allGames", username],
    queryFn: () => api.allGames(username),
  });

  const {
    windowDays,
    setWindowDays,
    setPickedClass,
    windowByTime,
    classOptions,
    timeClass,
    since,
    classParam,
  } = useStatsFilters(games.data ?? []);

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

  const { phaseData, emptyPhases } = useMemo(
    () =>
      analyzed === null
        ? { phaseData: [], emptyPhases: [] }
        : splitPhases(analyzed.phases),
    [analyzed],
  );
  const judgmentData: BarDatum[] =
    analyzed === null
      ? []
      : JUDGMENTS.map((judgment) => ({
          label: judgment,
          value: analyzed.judgment_counts[judgment] ?? 0,
          color: JUDGMENT_COLORS[judgment],
        }));

  const terminations = useMemo(
    () => (analyzed === null ? [] : terminationRows(analyzed)),
    [analyzed],
  );

  const monthlyAcpl = useMemo(
    () =>
      analyzed === null
        ? []
        : analyzed.months.map((m) => ({ month: m.month, value: m.acpl })),
    [analyzed],
  );
  const monthlyBlunderRate = useMemo(
    () =>
      analyzed === null
        ? []
        : analyzed.months.map((m) => ({
            month: m.month,
            value: m.blunder_rate === null ? null : m.blunder_rate * 100,
          })),
    [analyzed],
  );

  // Collapse the fine ECO variations into (color, system) families —
  // never merging a color's own choices with what it faces — split by
  // color for the two repertoire tables below.
  const allFamilies = useMemo(
    () => groupByFamily(openings.data ?? []),
    [openings.data],
  );
  const whiteFamilies = useMemo(
    () => allFamilies.filter((family) => family.color === "white"),
    [allFamilies],
  );
  const blackFamilies = useMemo(
    () => allFamilies.filter((family) => family.color === "black"),
    [allFamilies],
  );

  const familyLink = (family: OpeningFamily): string => {
    const params = new URLSearchParams({
      family: family.family,
      color: family.color,
      system: family.system,
    });
    if (classParam !== undefined) {
      params.set("time_class", classParam);
    }
    return `/players/${username}/games?${params.toString()}`;
  };

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
          <StatsFilters
            windowDays={windowDays}
            setWindowDays={setWindowDays}
            timeClass={timeClass}
            setPickedClass={setPickedClass}
            classOptions={classOptions}
          />
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
                {emptyPhases.length > 0 && (
                  <p className="panel-empty">
                    {emptyPhases
                      .map((phase) => `No ${phase} moves`)
                      .join(" · ")}
                  </p>
                )}
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

      {hasScopedGames && analyzed !== null && (
        <section>
          <h2>Trend</h2>
          <p>
            ACPL and blunder rate by month — the rating chart already shows
            form.
          </p>
          <div className="chart-row">
            <div>
              <h3>ACPL by month</h3>
              <MonthlyMetricChart
                data={monthlyAcpl}
                label="average centipawn loss by month"
              />
            </div>
            <div>
              <h3>Blunder rate by month</h3>
              <MonthlyMetricChart
                data={monthlyBlunderRate}
                label="blunder rate by month"
                formatValue={(value) => `${value.toFixed(1)}%`}
              />
            </div>
          </div>
        </section>
      )}

      {hasScopedGames && analyzed !== null && (
        <section>
          <h2>How games end</h2>
          <p>
            Each result split by termination — a loss on the clock needs
            different work than a loss on the board.
          </p>
          {terminations.length > 0 ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Result</th>
                    <th>Termination</th>
                    <th>Games</th>
                    <th>Share of that result</th>
                  </tr>
                </thead>
                <tbody>
                  {terminations.map((row) => (
                    <tr key={`${row.result}-${row.termination}`}>
                      <td className={`result-${row.result}`}>{row.result}</td>
                      <td>{row.termination}</td>
                      <td>{row.games}</td>
                      <td>{row.share}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="panel-empty">No termination data yet.</p>
          )}
        </section>
      )}

      {hasScopedGames && analyzed !== null && (
        <section>
          <h2>Opponent strength</h2>
          {analyzed.opponents !== null ? (
            <div className="tiles">
              <Tile
                value={signedRound(analyzed.opponents.avg_rating_diff)}
                label="avg rating edge (you vs opponents)"
              />
              <Tile
                value={recordRate(analyzed.opponents.vs_stronger)}
                label={`vs stronger (${analyzed.opponents.vs_stronger.games})`}
              />
              <Tile
                value={recordRate(analyzed.opponents.vs_similar)}
                label={`vs similar (${analyzed.opponents.vs_similar.games})`}
              />
              <Tile
                value={recordRate(analyzed.opponents.vs_weaker)}
                label={`vs weaker (${analyzed.opponents.vs_weaker.games})`}
              />
            </div>
          ) : (
            <p className="panel-empty">
              Not enough analyzed games to compare opponent strength.
            </p>
          )}
        </section>
      )}

      <section>
        <h2>Repertoire</h2>
        <p>
          Split by the color you had — an opponent's choice is not your
          repertoire. The system column is your own first moves; the line
          beneath it is the most common continuation, both sides answering.
          Opening ACPL covers the opening phase only; whole-game ACPL covers the
          full game and is not opening advice on its own.
        </p>
        <div className="filters">
          <label>
            Min games{" "}
            <input
              type="number"
              min={1}
              className="limit-input"
              aria-label="minimum games per system"
              value={minGames}
              onChange={(event) =>
                setMinGames(Math.max(1, Number(event.target.value) || 1))
              }
            />
          </label>
        </div>

        {openings.isPending && <p>Loading…</p>}
        {openings.isError && <p role="alert">{openings.error.message}</p>}
        {openings.isSuccess && openings.data.length === 0 && (
          <p>No classified games match these filters.</p>
        )}
        {openings.isSuccess && openings.data.length > 0 && (
          <>
            <RepertoireTable
              title="As White"
              families={whiteFamilies}
              minGames={minGames}
              familyLink={familyLink}
            />
            <RepertoireTable
              title="As Black"
              families={blackFamilies}
              minGames={minGames}
              familyLink={familyLink}
            />
          </>
        )}
      </section>

      {hasScopedGames && analyzed !== null && (
        <section>
          <h2>Recurring mistakes</h2>
          {analyzed.error_patterns.length > 0 ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Pattern</th>
                    <th>Count</th>
                    <th>% of blunders</th>
                    <th>Example</th>
                  </tr>
                </thead>
                <tbody>
                  {analyzed.error_patterns.map((pattern) => (
                    <tr key={pattern.pattern}>
                      <td>{pattern.label}</td>
                      <td>{pattern.count}</td>
                      <td>{Math.round(pattern.share_of_blunders * 100)}%</td>
                      <td>
                        {pattern.example_game_id !== null &&
                        pattern.example_game_id !== undefined ? (
                          <Link to={`/games/${pattern.example_game_id}`}>
                            {errorExampleLabel(pattern)}
                          </Link>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="panel-empty">No tagged error patterns yet.</p>
          )}
        </section>
      )}
    </Layout>
  );
}
