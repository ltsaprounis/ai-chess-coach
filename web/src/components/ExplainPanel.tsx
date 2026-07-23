import Markdown from "react-markdown";
import type { ExplainState } from "../explain.ts";

type Props = { state: ExplainState };

/**
 * Progress lines, then markdown, for the Game page's move-explanation
 * stream. Renders nothing before the first click (`status === "idle"`);
 * `text` holds the accumulating markdown while streaming and the full
 * cached/generated markdown once `done`.
 */
export default function ExplainPanel({ state }: Props) {
  if (state.status === "idle") {
    return null;
  }
  return (
    <div className="explain-panel">
      {state.ply !== null && (
        <p className="explain-note">explanation for move {state.ply}</p>
      )}
      {state.progress.map((line) => (
        <p key={line.id} className="explain-progress">
          {line.text}
        </p>
      ))}
      {state.status === "streaming" && state.text === "" && (
        <p className="explain-progress">thinking…</p>
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
