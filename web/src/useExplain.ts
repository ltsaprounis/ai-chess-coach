import { useCallback, useEffect, useReducer, useRef } from "react";
import { explainUrl } from "./api.ts";
import {
  type ExplainDone,
  type ExplainError,
  type ExplainEvent,
  type ExplainState,
  explainReducer,
  initialExplainState,
} from "./explain.ts";
import { consumeSse, errorMessage } from "./sse.ts";

/**
 * Drive one `GET /api/games/{id}/explain` stream into `explainReducer`.
 * Uses `fetch` rather than `EventSource`: a pre-stream failure
 * (404/409/400/503, per docs/07-api.md) arrives as an ordinary JSON
 * error body, which `EventSource` has no way to surface — it only
 * ever fires a bare, bodyless `error` event. The block-parsing and
 * stream-reading machinery lives in `sse.ts`, shared with `useChat.ts`.
 *
 * Explicitly user-triggered only (`explain()`); nothing here runs on
 * mount, and changing which ply is selected does not touch this
 * state — the caller decides when a fresh click is warranted. The
 * same entry point serves Regenerate: pass `refresh: true` to skip
 * the server cache and overwrite it with a fresh explanation.
 */
export function useExplain(): {
  state: ExplainState;
  explain: (
    gameId: string,
    ply: number,
    agentId?: string,
    options?: { refresh?: boolean },
  ) => void;
} {
  const [state, dispatch] = useReducer(explainReducer, initialExplainState);
  const abortRef = useRef<AbortController | null>(null);

  const explain = useCallback(
    (
      gameId: string,
      ply: number,
      agentId?: string,
      options: { refresh?: boolean } = {},
    ) => {
      const refresh = options.refresh ?? false;
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      dispatch({ type: "start", ply, refresh });

      void (async () => {
        let response: Response;
        try {
          response = await fetch(explainUrl(gameId, ply, agentId, refresh), {
            signal: controller.signal,
            headers: { Accept: "text/event-stream" },
          });
        } catch (err) {
          if (!controller.signal.aborted) {
            dispatch({ type: "error", message: (err as Error).message });
          }
          return;
        }
        if (!response.ok) {
          dispatch({ type: "error", message: await errorMessage(response) });
          return;
        }
        if (response.body === null) {
          dispatch({ type: "error", message: "empty explanation stream" });
          return;
        }

        try {
          const outcome = await consumeSse(response.body, (raw) => {
            if (raw.event === "done") {
              dispatch({
                type: "done",
                payload: JSON.parse(raw.data) as ExplainDone,
              });
              return false;
            }
            if (raw.event === "error") {
              const payload = JSON.parse(raw.data) as ExplainError;
              dispatch({ type: "error", message: payload.message });
              return false;
            }
            if (raw.event === "text" || raw.event === "tool") {
              dispatch({
                type: "event",
                event: JSON.parse(raw.data) as ExplainEvent,
              });
            }
          });
          // The body ended without a terminal done/error event (server
          // restarted mid-explanation): surface it, or the panel sits
          // in "streaming" with a disabled button forever. A
          // deliberate abort (new click, unmount) is not an error.
          if (outcome === "ended" && !controller.signal.aborted) {
            dispatch({
              type: "error",
              message: "explanation stream ended unexpectedly — try again",
            });
          }
        } catch (err) {
          if (!controller.signal.aborted) {
            dispatch({ type: "error", message: (err as Error).message });
          }
        }
      })();
    },
    [],
  );

  useEffect(() => () => abortRef.current?.abort(), []);

  return { state, explain };
}
