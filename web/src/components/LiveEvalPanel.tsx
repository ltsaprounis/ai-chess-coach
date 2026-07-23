import {
  evalSign,
  formatEval,
  sideToMove,
  whiteFraction,
} from "../liveEval.ts";
import type { LiveEvalState } from "../useLiveEval.ts";

type Props = { state: LiveEvalState; fen: string };

/**
 * Eval bar, header, and one row per MultiPV candidate line for the
 * Game page's live engine stream. Written for a non-engine user: the
 * header names whose move it is and what the list means, each row
 * leads with the candidate move in bold SAN and a sign-colored eval
 * chip, the rest of the line is muted secondary text, and depth (raw
 * engine detail) lives in the row's tooltip rather than in the text.
 * The bar and header reflect the top-ranked line; `done` with no eval
 * means the shown position is terminal — the engine has nothing to
 * say. The server decides how many lines to send; this renders
 * whatever the snapshot carries.
 */
export default function LiveEvalPanel({ state, fen }: Props) {
  const { latest, done, error } = state;
  const side = sideToMove(fen);
  if (error) {
    return <p className="live-eval-note">engine unavailable</p>;
  }
  const lines = latest?.lines ?? [];
  const primary = lines[0];
  if (primary === undefined) {
    return (
      <p className="live-eval-note">
        {done ? "game over — no eval" : "evaluating…"}
      </p>
    );
  }
  return (
    <div className="live-eval">
      <p className="live-eval-header">
        <strong>{side === "white" ? "White" : "Black"} to move</strong> —
        engine's best options
        {!done && <span className="live-eval-searching"> (searching…)</span>}
      </p>
      <div
        className="eval-bar"
        role="img"
        aria-label={`engine eval ${formatEval(primary)}, favoring ${evalSign(primary)}`}
      >
        <div
          className="eval-bar-white"
          style={{ width: `${whiteFraction(primary) * 100}%` }}
        />
      </div>
      <ol className="live-eval-lines">
        {lines.map((line) => {
          const [candidate, ...continuation] = line.pv_san;
          return (
            <li
              key={line.multipv}
              className="live-eval-row"
              title={`depth ${line.depth}`}
            >
              <span className="live-eval-rank">{line.multipv}.</span>
              <span className="live-eval-move">{candidate ?? "—"}</span>
              <span className={`eval-chip eval-chip-${evalSign(line)}`}>
                {formatEval(line)}
              </span>
              {continuation.length > 0 && (
                <span className="live-eval-pv">{continuation.join(" ")}</span>
              )}
            </li>
          );
        })}
      </ol>
      <p className="live-eval-legend">sign: + White ahead · − Black ahead</p>
    </div>
  );
}
