import { formatEval, whiteFraction } from "../liveEval.ts";
import type { LiveEvalState } from "../useLiveEval.ts";

type Props = { state: LiveEvalState };

/**
 * Eval bar, headline, and one row per MultiPV candidate line for the
 * Game page's live engine stream. The bar and headline reflect the
 * top-ranked line; `done` with no eval means the shown position is
 * terminal — the engine has nothing to say. The server decides how
 * many lines to send; this renders whatever the snapshot carries.
 */
export default function LiveEvalPanel({ state }: Props) {
  const { latest, done, error } = state;
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
      <div
        className="eval-bar"
        role="img"
        aria-label={`engine eval ${formatEval(primary)}`}
      >
        <div
          className="eval-bar-white"
          style={{ width: `${whiteFraction(primary) * 100}%` }}
        />
      </div>
      <p className="live-eval-line">
        <strong>{formatEval(primary)}</strong> · depth {primary.depth}
        {done ? "" : "…"}
      </p>
      <ol className="live-eval-lines">
        {lines.map((line) => (
          <li key={line.multipv} className="live-eval-row">
            <span className="live-eval-rank">{line.multipv}.</span>
            <span className="live-eval-score">{formatEval(line)}</span>
            <span className="live-eval-depth">d{line.depth}</span>
            {line.pv_san.length > 0 && (
              <span className="live-eval-pv" title={line.pv_san.join(" ")}>
                {line.pv_san.join(" ")}
              </span>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
