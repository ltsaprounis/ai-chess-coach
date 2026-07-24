import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Chess } from "chess.js";
import { useEffect, useMemo, useState } from "react";
import { Chessboard } from "react-chessboard";
import { useParams } from "react-router-dom";
import { api, type MoveEval } from "../api.ts";
import { getStoredAgentId, resolveAgentId } from "../coachAgent.ts";
import EvalGraph from "../components/EvalGraph.tsx";
import ExplainPanel from "../components/ExplainPanel.tsx";
import Layout from "../components/Layout.tsx";
import LiveEvalPanel from "../components/LiveEvalPanel.tsx";
import { useExplain } from "../useExplain.ts";
import { useLiveEval } from "../useLiveEval.ts";

const LIVE_EVAL_KEY = "liveEval";

// Board overlays for an analyzed game: the last move's squares are
// shaded by its judgment, and a green arrow marks the engine's best
// move at positions where the move actually played was a slip.
const BEST_MOVE_COLOR = "#1a9d54";
const SLIP_JUDGMENTS: ReadonlySet<string> = new Set([
  "inaccuracy",
  "mistake",
  "blunder",
]);
const LAST_MOVE_COLORS: Record<string, string> = {
  best: "color-mix(in srgb, var(--j-best) 40%, transparent)",
  good: "color-mix(in srgb, var(--j-best) 30%, transparent)",
  inaccuracy: "color-mix(in srgb, var(--j-inaccuracy) 45%, transparent)",
  mistake: "color-mix(in srgb, var(--j-mistake) 48%, transparent)",
  blunder: "color-mix(in srgb, var(--j-blunder) 52%, transparent)",
  none: "color-mix(in srgb, #eab308 40%, transparent)",
};

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

  const { fens, moveSquares } = useMemo(() => {
    const chess = new Chess();
    const fenList = [chess.fen()];
    const squares: { from: string; to: string }[] = [];
    for (const san of game.data?.san_moves ?? []) {
      const move = chess.move(san);
      squares.push({ from: move.from, to: move.to });
      fenList.push(chess.fen());
    }
    return { fens: fenList, moveSquares: squares };
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
      void queryClient.invalidateQueries({ queryKey: ["allGames"] });
      void queryClient.invalidateQueries({ queryKey: ["openings"] });
      void queryClient.invalidateQueries({ queryKey: ["report"] });
    }
  }, [analyzing, game.data, queryClient]);

  // Shade the move that produced the shown position by its judgment.
  const squareStyles = useMemo(() => {
    const square = ply >= 1 ? moveSquares[ply - 1] : undefined;
    if (square === undefined) {
      return {};
    }
    const judgment = game.data?.analysis?.evals?.[ply - 1]?.judgment;
    const color = LAST_MOVE_COLORS[judgment ?? "none"];
    return {
      [square.from]: { backgroundColor: color },
      [square.to]: { backgroundColor: color },
    };
  }, [ply, moveSquares, game.data]);

  // Arrow the engine's best move when the move played from here slipped.
  const arrows = useMemo(() => {
    const next = game.data?.analysis?.evals?.[ply];
    if (next === undefined || !SLIP_JUDGMENTS.has(next.judgment)) {
      return [];
    }
    const uci = next.best_move;
    if (uci.length < 4) {
      return [];
    }
    return [
      {
        startSquare: uci.slice(0, 2),
        endSquare: uci.slice(2, 4),
        color: BEST_MOVE_COLOR,
      },
    ];
  }, [ply, game.data]);

  if (game.isPending) {
    return <Layout>Loading…</Layout>;
  }
  if (game.isError) {
    return (
      <Layout>
        <p role="alert">{game.error.message}</p>
      </Layout>
    );
  }

  const data = game.data;
  const evals = data.analysis?.evals ?? null;
  const clamp = (value: number) =>
    Math.max(0, Math.min(fens.length - 1, value));
  const explainingCurrent =
    explainState.status === "streaming" && explainState.ply === ply;

  return (
    <Layout username={data.username}>
      <h1>
        {data.username} ({data.color}) vs {data.opponent} — {data.result}
      </h1>
      <p>
        {data.opening?.name ?? "Unknown opening"}
        {data.opening ? ` (${data.opening.eco})` : ""} ·{" "}
        {new Date(data.end_time * 1000).toLocaleString()} · {data.time_class}
      </p>

      {data.analysis && (
        <div className="acpl-strip">
          <span>
            <strong>{data.analysis.overall_acpl}</strong> ACPL
          </span>
          <span>
            <strong>{data.analysis.judgment_counts.blunder ?? 0}</strong>{" "}
            {(data.analysis.judgment_counts.blunder ?? 0) === 1
              ? "blunder"
              : "blunders"}
          </span>
          <span>
            <strong>{data.analysis.judgment_counts.mistake ?? 0}</strong>{" "}
            {(data.analysis.judgment_counts.mistake ?? 0) === 1
              ? "mistake"
              : "mistakes"}
          </span>
          <span>depth {data.analysis.depth}</span>
        </div>
      )}

      {evals && (
        <div className="eval-graph-strip">
          <EvalGraph evals={evals} selectedPly={ply} onSelect={setPly} />
        </div>
      )}

      <div className="game-layout">
        <div className="board-column">
          <Chessboard
            options={{
              position: fens[ply],
              boardOrientation: data.color,
              allowDragging: false,
              animationDurationInMs: 150,
              squareStyles,
              arrows,
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
          {evals && (
            <p className="board-note">
              Last move shaded by quality; a{" "}
              <span className="board-note-arrow">green arrow</span> shows the
              engine's best move where the next move slipped.
            </p>
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

          <section className="panel engine-panel">
            <h2>Engine</h2>
            <label className="live-toggle">
              <input
                type="checkbox"
                checked={live}
                onChange={(event) => toggleLive(event.target.checked)}
              />
              Live engine
            </label>
            {live ? (
              <LiveEvalPanel
                state={liveEval}
                fen={fens[ply] ?? fens[0] ?? ""}
              />
            ) : (
              <p className="panel-empty">
                Turn on live engine to see the engine's top candidate moves for
                the current position.
              </p>
            )}
          </section>
        </div>

        <div className="side-column">
          <section className="panel coach-panel">
            <h2>Coach</h2>
            {data.analysis ? (
              ply >= 1 ? (
                <>
                  <button
                    type="button"
                    disabled={explainingCurrent}
                    onClick={() => explain(data.id, ply, agentId)}
                  >
                    {explainingCurrent ? "Explaining…" : "Explain move"}
                  </button>
                  <ExplainPanel
                    state={explainState}
                    sanMoves={data.san_moves}
                    onRegenerate={() => {
                      if (explainState.ply !== null) {
                        explain(data.id, explainState.ply, agentId, {
                          refresh: true,
                        });
                      }
                    }}
                  />
                </>
              ) : (
                <p className="panel-empty">
                  Select a move from the list to ask the coach about it.
                </p>
              )
            ) : (
              <p className="panel-empty">
                Analyze this game to enable move explanations.
              </p>
            )}
          </section>

          <section className="moves-panel">
            <h2>Moves</h2>
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
          </section>
        </div>
      </div>
    </Layout>
  );
}
