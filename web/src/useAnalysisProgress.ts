import { useEffect, useRef, useState } from "react";
import { progressUrl } from "./api.ts";

export type RunProgress = {
  gamesTotal: number;
  gamesDone: number;
  currentPly?: number;
  totalPlies?: number;
  failed: boolean;
};

type RunEventData = {
  games_total: number;
  games_done: number;
  finished: boolean;
  progress: { game_id: string; ply: number; total_plies: number } | null;
};

const EVENT_TYPES = [
  "snapshot",
  "progress",
  "game_done",
  "run_done",
  "run_failed",
] as const;

/** Follow the backend's SSE analysis progress stream while `active`. */
export function useAnalysisProgress(
  username: string,
  active: boolean,
  onFinished: () => void,
): RunProgress | null {
  const [state, setState] = useState<RunProgress | null>(null);
  const onFinishedRef = useRef(onFinished);
  onFinishedRef.current = onFinished;

  useEffect(() => {
    if (!active) {
      setState(null);
      return;
    }
    const source = new EventSource(progressUrl(username));

    const handle = (event: MessageEvent) => {
      const data = JSON.parse(event.data as string) as RunEventData;
      setState({
        gamesTotal: data.games_total,
        gamesDone: data.games_done,
        currentPly: data.progress?.ply,
        totalPlies: data.progress?.total_plies,
        failed: event.type === "run_failed",
      });
      if (data.finished) {
        source.close();
        onFinishedRef.current();
      }
    };

    for (const type of EVENT_TYPES) {
      source.addEventListener(type, handle);
    }
    source.onerror = () => source.close();
    return () => source.close();
  }, [username, active]);

  return state;
}
