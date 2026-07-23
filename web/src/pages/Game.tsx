import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Chess } from "chess.js";
import { useEffect, useMemo, useState } from "react";
import { Chessboard } from "react-chessboard";
import { Link, useParams } from "react-router-dom";
import { api, type MoveEval } from "../api.ts";
import { getStoredAgentId, resolveAgentId } from "../coachAgent.ts";
import EvalGraph from "../components/EvalGraph.tsx";
import ExplainPanel from "../components/ExplainPanel.tsx";
import LiveEvalPanel from "../components/LiveEvalPanel.tsx";
import { useExplain } from "../useExplain.ts";
import { useLiveEval } from "../useLiveEval.ts";

const LIVE_EVAL_KEY = "liveEval";

function readLiveToggle(): boolean {
  try {
    return localStorage.getItem(LIVE_EVAL_KEY) === "1";
  } catch {
    return false;
  }
}

function storeLiveToggle(on: boolean): void {
  try {
    localStorage.setItem(LIVE_EVAL_KEY, on ? "1" : "0");
  } catch {
    // Storage blocked (private mode): the toggle just doesn't persist.
  }
}

export default function Game() {
  const { id = "" } = useParams();
  const [ply, setPly] = useState(0); // 0 = start position
  const [analyzing, setAnalyzing] = useState(false);
  const queryClient = useQueryClient();

  const game = useQuery({
    queryKey: ["game", id],
    queryFn: () => api.game(id),
    refetchInterval: analyzing ? 1000 : false,
  });

  // Same agent roster + persisted choice as Home/Coach (coachAgent.ts) —
  // explain reuses whichever agent the player already picked there.
  const agents = useQuery({
    queryKey: ["coachAgents"],
    queryFn: api.coachAgents,
  });
  const agentId = agents.data
    ? resolveAgentId(
        getStoredAgentId(),
        agents.data.agents,
        agents.data.default,
      )
    : undefined;
  const { state: explainState, explain } = useExplain();

  const fens = useMemo(() => {
    const chess = new Chess();
    const list = [chess.fen()];
    for (const san of game.data?.san_moves ?? []) {
      chess.move(san);
      list.push(chess.fen());
    }
    return list;
  }, [game.data]);

  const [live, setLive] = useState(readLiveToggle);
  const liveEval = useLiveEval(live && game.data ? (fens[ply] ?? null) : null);
  const toggleLive = (on: boolean) => {
    storeLiveToggle(on);
    setLive(on);
  };

  useEffect(() => {
    if (analyzing && game.data?.analysis) {
      setAnalyzing(false);
      queryClient.invalidateQueries({ queryKey: ["games"] });
    }
  }, [analyzing, game.data, queryClient]);

  if (game.isPending) {
    return <main className="page">Loading…</main>;
  }
  if (game.isError) {
    return (
      <main className="page">
        <p role="alert">{game.error.message}</p>
      </main>
    );
  }

  const data = game.data;
  const evals = data.analysis?.evals ?? null;
  const clamp = (value: number) =>
    Math.max(0, Math.min(fens.length - 1, value));

  return (
    <main className="page">
      <p>
        <Link to={`/players/${data.username}/games`}>← games</Link>
        {" · "}
        <Link to={`/players/${data.username}/dashboard`}>dashboard</Link>
      </p>
      <h1>
        {data.username} ({data.color}) vs {data.opponent} — {data.result}
      </h1>
      <p>
        {data.opening?.name ?? "Unknown opening"}
        {data.opening ? ` (${data.opening.eco})` : ""} ·{" "}
        {new Date(data.end_time * 1000).toLocaleString()} · {data.time_class}
      </p>

      <div className="game-layout">
        <div className="board-column">
          <Chessboard
            options={{
              position: fens[ply],
              boardOrientation: data.color,
              allowDragging: false,
              animationDurationInMs: 150,
            }}
          />
          <div className="board-controls">
            <button type="button" onClick={() => setPly(0)}>
              ⏮
            </button>
            <button type="button" onClick={() => setPly((p) => clamp(p - 1))}>
              ‹
            </button>
            <button type="button" onClick={() => setPly((p) => clamp(p + 1))}>
              ›
            </button>
            <button type="button" onClick={() => setPly(fens.length - 1)}>
              ⏭
            </button>
            <span>
              ply {ply}/{fens.length - 1}
            </span>
          </div>
          <label className="live-toggle">
            <input
              type="checkbox"
              checked={live}
              onChange={(event) => toggleLive(event.target.checked)}
            />
            Live engine
          </label>
          {live && <LiveEvalPanel state={liveEval} />}
          {evals && (
            <EvalGraph evals={evals} selectedPly={ply} onSelect={setPly} />
          )}
          {!evals && (
            <button
              type="button"
              disabled={analyzing}
              onClick={() => {
                setAnalyzing(true);
                void api.analyze(data.username, { gameIds: [data.id] });
              }}
            >
              {analyzing ? "Analyzing…" : "Analyze this game"}
            </button>
          )}
        </div>

        <ol className="moves">
          {data.san_moves.map((san, index) => {
            const movePly = index + 1;
            const moveEval: MoveEval | undefined = evals?.[index];
            return (
              <li key={`${movePly}-${san}`}>
                <button
                  type="button"
                  className={[
                    "move",
                    moveEval ? `j-${moveEval.judgment}` : "",
                    ply === movePly ? "selected" : "",
                  ].join(" ")}
                  onClick={() => setPly(movePly)}
                >
                  {movePly % 2 === 1 ? `${(movePly + 1) / 2}. ` : ""}
                  {san}
                  {moveEval && moveEval.cp_loss >= 100 ? (
                    <span className="loss"> −{moveEval.cp_loss}</span>
                  ) : null}
                </button>
              </li>
            );
          })}
        </ol>
      </div>

      {data.analysis && (
        <p>
          ACPL {data.analysis.overall_acpl} · blunders{" "}
          {data.analysis.judgment_counts.blunder ?? 0} · mistakes{" "}
          {data.analysis.judgment_counts.mistake ?? 0} · depth{" "}
          {data.analysis.depth}
        </p>
      )}

      {data.analysis && ply >= 1 && (
        <div className="explain-section">
          <button
            type="button"
            disabled={
              explainState.status === "streaming" && explainState.ply === ply
            }
            onClick={() => explain(data.id, ply, agentId)}
          >
            {explainState.status === "streaming" && explainState.ply === ply
              ? "Explaining…"
              : "Explain move"}
          </button>
          <ExplainPanel state={explainState} />
        </div>
      )}
    </main>
  );
}
