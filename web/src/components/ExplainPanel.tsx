import Markdown from "react-markdown";
import { type ExplainState, formatMoveLabel } from "../explain.ts";

type Props = {
  state: ExplainState;
  /** The game's SAN moves in order (index 0 = ply 1) — labels the title. */
  sanMoves: string[];
  /** Re-runs the displayed explanation with `refresh=true`. */
  onRegenerate: () => void;
};

/**
 * Titled coach panel for the Game page's move-explanation stream.
 * Renders nothing before the first click (`status === "idle"`);
 * `text` holds the accumulating markdown while streaming and the full
 * cached/generated markdown once `done`. Regenerate only appears once
 * a cached or freshly-completed explanation is on screen — clicking
 * it re-issues the same user-triggered request with `refresh: true`.
 */
export default function ExplainPanel({ state, sanMoves, onRegenerate }: Props) {
  if (state.status === "idle") {
    return null;
  }
  const label =
    state.ply !== null
      ? formatMoveLabel(state.ply, sanMoves[state.ply - 1] ?? "?")
      : null;
  const regenerating = state.status === "streaming" && state.isRefresh;

  return (
    <div className="explain-panel">
      <div className="explain-header">
        <h3 className="explain-title">
          {label !== null ? `Coach on ${label}` : "Coach"}
        </h3>
        {state.status === "done" && (
          <button
            type="button"
            className="explain-regenerate"
            onClick={onRegenerate}
          >
            Regenerate
          </button>
        )}
      </div>
      {state.progress.map((line) => (
        <p key={line.id} className="explain-progress">
          {line.text}
        </p>
      ))}
      {state.status === "streaming" && state.text === "" && (
        <p className="explain-progress">
          {regenerating ? "regenerating…" : "thinking…"}
        </p>
      )}
      {state.status === "error" && <p role="alert">{state.error}</p>}
      {state.text !== "" && (
        <article className="advice">
          <Markdown>{state.text}</Markdown>
        </article>
      )}
    </div>
  );
}
