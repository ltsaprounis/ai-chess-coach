import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, score, sortWorstFirst } from "../api.ts";

export default function Dashboard() {
  const { username = "" } = useParams();

  const openings = useQuery({
    queryKey: ["openings", username],
    queryFn: () => api.openings(username),
  });
  const report = useQuery({
    queryKey: ["report", username],
    queryFn: () => api.report(username),
  });

  return (
    <main className="page">
      <p>
        <Link to="/">← change player</Link>
        {" · "}
        <Link to={`/players/${username}/games`}>games</Link>
        {" · "}
        <Link to={`/players/${username}/coach`}>coach</Link>
      </p>
      <h1>{username}'s repertoire</h1>

      {report.isSuccess && report.data.games_analyzed > 0 && (
        <p>
          {report.data.games_analyzed} games analyzed · ACPL{" "}
          {report.data.overall_acpl} (opening{" "}
          {report.data.acpl_by_phase.opening ?? "—"}, middlegame{" "}
          {report.data.acpl_by_phase.middlegame ?? "—"}, endgame{" "}
          {report.data.acpl_by_phase.endgame ?? "—"}) · blunders{" "}
          {report.data.judgment_counts.blunder ?? 0} · mistakes{" "}
          {report.data.judgment_counts.mistake ?? 0} · inaccuracies{" "}
          {report.data.judgment_counts.inaccuracy ?? 0}
        </p>
      )}

      <p>Sorted worst-scoring first — the openings to work on.</p>

      {openings.isPending && <p>Loading…</p>}
      {openings.isError && <p role="alert">{openings.error.message}</p>}
      {openings.isSuccess && openings.data.length === 0 && (
        <p>No classified games yet — sync this player first.</p>
      )}
      {openings.isSuccess && openings.data.length > 0 && (
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
      )}
    </main>
  );
}
