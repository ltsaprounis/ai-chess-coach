// Pure reducer for the Game page's move-explanation stream — no
// fetching, no React, unit-tested in explain.test.ts.

/**
 * One streamed increment of a move explanation from
 * `GET /api/games/{id}/explain`. Hand-declared because SSE payloads
 * are not part of the OpenAPI schema; mirrors the backend's
 * `ExplainEvent` model (`text`: a markdown chunk; `tool`: a short
 * tool-call summary such as "engine: analyzing <fen>").
 */
export type ExplainEvent = {
  type: "text" | "tool";
  text: string;
};

/**
 * Terminal `done` SSE payload — the full markdown explanation
 * (now cached server-side). Mirrors the backend's `ExplainDone`.
 */
export type ExplainDone = {
  text: string;
};

/**
 * Mid-stream `error` SSE payload — a provider failure after
 * streaming had already begun. Mirrors the backend's `ExplainError`.
 */
export type ExplainError = {
  message: string;
};

export type ExplainStatus = "idle" | "streaming" | "done" | "error";

/** One progress line; `id` is a stable React key (tool text may repeat). */
export type ProgressLine = { id: number; text: string };

export type ExplainState = {
  status: ExplainStatus;
  /** The 1-based ply this explanation is for (null before the first click). */
  ply: number | null;
  /** Tool-call summaries, oldest first, shown as progress lines. */
  progress: ProgressLine[];
  /** Accumulated markdown while streaming; replaced wholesale on `done`. */
  text: string;
  error: string | null;
};

export const initialExplainState: ExplainState = {
  status: "idle",
  ply: null,
  progress: [],
  text: "",
  error: null,
};

export type ExplainAction =
  | { type: "start"; ply: number }
  | { type: "event"; event: ExplainEvent }
  | { type: "done"; payload: ExplainDone }
  | { type: "error"; message: string };

/** Folds one SSE lifecycle into the panel state shown on the Game page. */
export function explainReducer(
  state: ExplainState,
  action: ExplainAction,
): ExplainState {
  switch (action.type) {
    case "start":
      return {
        status: "streaming",
        ply: action.ply,
        progress: [],
        text: "",
        error: null,
      };
    case "event":
      return action.event.type === "tool"
        ? {
            ...state,
            progress: [
              ...state.progress,
              { id: state.progress.length, text: action.event.text },
            ],
          }
        : { ...state, text: state.text + action.event.text };
    case "done":
      return { ...state, status: "done", text: action.payload.text };
    case "error":
      return { ...state, status: "error", error: action.message };
    default:
      return state;
  }
}
