import { useEffect, useState } from "react";
import { evalUrl } from "./api.ts";
import type { LiveEval } from "./liveEval.ts";

export type LiveEvalState = {
  latest: LiveEval | null;
  done: boolean;
  error: boolean;
};

const IDLE: LiveEvalState = { latest: null, done: false, error: false };

/** ~one keyboard auto-repeat: skip positions the user steps past. */
const DEBOUNCE_MS = 300;

/**
 * Stream `GET /api/eval` for `fen` (null = idle). One EventSource at
 * a time: a fen change or unmount closes the current stream. An
 * error before any eval means the engine is unavailable — close and
 * stop, since EventSource would otherwise auto-reconnect forever; a
 * drop after evals arrived just keeps the last one and stops.
 */
export function useLiveEval(fen: string | null): LiveEvalState {
  const [state, setState] = useState<LiveEvalState>(IDLE);

  useEffect(() => {
    setState(IDLE);
    if (fen === null) {
      return;
    }
    let source: EventSource | null = null;
    const timer = setTimeout(() => {
      const stream = new EventSource(evalUrl(fen));
      source = stream;
      let sawEval = false;
      stream.addEventListener("eval", (event: MessageEvent) => {
        sawEval = true;
        const data = JSON.parse(event.data as string) as LiveEval;
        setState({ latest: data, done: false, error: false });
      });
      stream.addEventListener("done", () => {
        stream.close();
        setState((previous) => ({ ...previous, done: true }));
      });
      stream.onerror = () => {
        stream.close();
        setState((previous) =>
          sawEval ? { ...previous, done: true } : { ...previous, error: true },
        );
      };
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      source?.close();
    };
  }, [fen]);

  return state;
}
