import { formatEval, whiteFraction } from "../liveEval.ts";
import type { LiveEvalState } from "../useLiveEval.ts";

type Props = { state: LiveEvalState };

/**
 * Eval bar, score, depth, and principal variation for the Game
 * page's live engine stream. `done` with no eval means the shown
 * position is terminal — the engine has nothing to say.
 */
export default function LiveEvalPanel({ state }: Props) {
  const { latest, done, error } = state;
  if (error) {
    return <p className="live-eval-note">engine unavailable</p>;
  }
  if (latest === null) {
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
        aria-label={`engine eval ${formatEval(latest)}`}
      >
        <div
          className="eval-bar-white"
          style={{ width: `${whiteFraction(latest) * 100}%` }}
        />
      </div>
      <p className="live-eval-line">
        <strong>{formatEval(latest)}</strong> · depth {latest.depth}
        {done ? "" : "…"}
      </p>
      {latest.pv_san.length > 0 && (
        <p className="live-eval-pv" title={latest.pv_san.join(" ")}>
          {latest.pv_san.join(" ")}
        </p>
      )}
    </div>
  );
}
