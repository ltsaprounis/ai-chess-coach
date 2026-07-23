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

/** One `event:`/`data:` block off the wire, before its data is parsed. */
type RawSseEvent = { event: string; data: string };

// sse-starlette's default separator is "\r\n", with a blank line (so
// "\r\n\r\n") between events; accept plain "\n\n" too since intermediate
// proxies/dev servers may normalize line endings.
const BLOCK_SEP = /\r\n\r\n|\n\n/;
const LINE_SEP = /\r\n|\n/;

/** Splits whatever has arrived so far into complete blocks plus a remainder. */
function splitBlocks(buffer: string): { blocks: string[]; rest: string } {
  const parts = buffer.split(BLOCK_SEP);
  const rest = parts.pop() ?? "";
  return { blocks: parts, rest };
}

/** Parses one block's `event:`/`data:` lines; comment/ping lines are ignored. */
function parseBlock(block: string): RawSseEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split(LINE_SEP)) {
    if (line === "" || line.startsWith(":")) {
      continue;
    }
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      data.push(line.slice("data:".length).trim());
    }
  }
  return data.length === 0 ? null : { event, data: data.join("\n") };
}

/** The same `{error:{message}}` convention `api.ts`'s `json()` unwraps. */
async function errorMessage(response: Response): Promise<string> {
  const body = (await response.json().catch(() => null)) as {
    error?: { message?: string };
  } | null;
  return body?.error?.message ?? `HTTP ${response.status}`;
}

/**
 * Drive one `GET /api/games/{id}/explain` stream into `explainReducer`.
 * Uses `fetch` rather than `EventSource`: a pre-stream failure
 * (404/409/400/503, per docs/07-api.md) arrives as an ordinary JSON
 * error body, which `EventSource` has no way to surface — it only
 * ever fires a bare, bodyless `error` event.
 *
 * Explicitly user-triggered only (`explain()`); nothing here runs on
 * mount, and changing which ply is selected does not touch this
 * state — the caller decides when a fresh click is warranted.
 */
export function useExplain(): {
  state: ExplainState;
  explain: (gameId: string, ply: number, agentId?: string) => void;
} {
  const [state, dispatch] = useReducer(explainReducer, initialExplainState);
  const abortRef = useRef<AbortController | null>(null);

  const explain = useCallback(
    (gameId: string, ply: number, agentId?: string) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      dispatch({ type: "start", ply });

      void (async () => {
        let response: Response;
        try {
          response = await fetch(explainUrl(gameId, ply, agentId), {
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

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        try {
          while (true) {
            const { value, done: streamDone } = await reader.read();
            if (streamDone) {
              return;
            }
            buffer += decoder.decode(value, { stream: true });
            const { blocks, rest } = splitBlocks(buffer);
            buffer = rest;
            for (const block of blocks) {
              const raw = parseBlock(block);
              if (raw === null) {
                continue;
              }
              if (raw.event === "done") {
                dispatch({
                  type: "done",
                  payload: JSON.parse(raw.data) as ExplainDone,
                });
                return;
              }
              if (raw.event === "error") {
                const payload = JSON.parse(raw.data) as ExplainError;
                dispatch({ type: "error", message: payload.message });
                return;
              }
              if (raw.event === "text" || raw.event === "tool") {
                dispatch({
                  type: "event",
                  event: JSON.parse(raw.data) as ExplainEvent,
                });
              }
            }
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
