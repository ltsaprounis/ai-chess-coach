import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import { Link, useParams } from "react-router-dom";
import { api, type PlayerReport } from "../api.ts";
import { getStoredAgentId, resolveAgentId } from "../coachAgent.ts";
import {
  coverageGap,
  isRunConflict,
  shouldChainAfterRun,
} from "../coachCoverage.ts";
import ChatPanel from "../components/ChatPanel.tsx";
import Layout from "../components/Layout.tsx";
import { gameLinkMarkdownComponents } from "../components/markdownLinks.tsx";
import StatsFilters from "../components/StatsFilters.tsx";
import { useAnalysisProgress } from "../useAnalysisProgress.ts";
import { useChat } from "../useChat.ts";
import { useStatsFilters } from "../useStatsFilters.ts";

// Re-exported so `Coach.test.tsx`'s existing import keeps working —
// the new-tab link override moved to a shared module once ChatPanel
// needed the same behavior for chat replies (both render markdown
// whose citations are app-relative game deep links).
export { gameLinkMarkdownComponents as adviceMarkdownComponents } from "../components/markdownLinks.tsx";

type CoachOptions = { refresh?: boolean };

export default function Coach() {
  const { username = "" } = useParams();
  const [copied, setCopied] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const queryClient = useQueryClient();

  // Guards the chain's post-`await` `analyzeRest.mutate()` call below:
  // without it, navigating away mid-`invalidateQueries` still lets the
  // resolved await fire one more analyze run after unmount.
  const isMountedRef = useRef(true);
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const games = useQuery({
    queryKey: ["allGames", username],
    queryFn: () => api.allGames(username),
  });

  // The same time-window/time-control controls the Dashboard uses, so
  // the advice covers the period the student is actually looking at
  // rather than every game they have ever played.
  const {
    windowDays,
    setWindowDays,
    setPickedClass,
    classOptions,
    timeClass,
    since,
    classParam,
  } = useStatsFilters(games.data ?? []);

  // Which agent to use comes from Settings (localStorage). Quiet while
  // the roster loads or fails — no agent_id sent means server default.
  const agents = useQuery({
    queryKey: ["coachAgents"],
    queryFn: api.coachAgents,
  });
  const activeAgentId = agents.data
    ? resolveAgentId(
        getStoredAgentId(),
        agents.data.agents,
        agents.data.default,
      )
    : null;

  // The report over the same window/time-control: read for its
  // `games_analyzed`/`games_in_scope` server-truth coverage counts
  // (never recomputed client-side, see coverageGap) as well as to
  // compare a cached coach report's coverage against the live count.
  const reportQueryKey = ["report", username, windowDays, timeClass] as const;
  const report = useQuery({
    queryKey: reportQueryKey,
    queryFn: () => api.report(username, { since, time_class: classParam }),
    enabled: games.isSuccess,
  });

  const gap = report.data ? coverageGap(report.data) : null;

  // "Analyze the rest": posts the page's current filters, same as
  // /report and /coach get. A 409 means a run is already active for
  // this player (started here, from the Games page, or a backfill CLI
  // run) — attach to its progress instead of surfacing an error.
  const analyzeRest = useMutation({
    mutationFn: () => api.analyze(username, { since, time_class: classParam }),
    onSuccess: (outcome) => {
      if (outcome.queued > 0) {
        setAnalyzing(true);
      }
    },
    onError: (error) => {
      if (isRunConflict(error)) {
        setAnalyzing(true);
      }
    },
  });

  // Each analyze run caps at `engine.analyze_limit`, so draining a
  // large gap takes several runs. After a *clean* finish, re-read the
  // report; if this window still has a gap, start the next run
  // automatically while the user stays on the page. Leaving the page
  // unmounts useAnalysisProgress, which closes the SSE stream before a
  // later `finished` event can call back here — that's most of what
  // stops the chain; `isMountedRef` covers the narrow race where
  // navigation happens mid-`await` (see its comment above). The
  // server-side run itself continues regardless.
  const continueIfGapRemains = async () => {
    await queryClient.invalidateQueries({ queryKey: ["report"] });
    if (!isMountedRef.current) {
      return;
    }
    const refreshed = queryClient.getQueryData<PlayerReport>(reportQueryKey);
    if (refreshed !== undefined && coverageGap(refreshed) !== null) {
      analyzeRest.mutate();
    }
  };

  const progress = useAnalysisProgress(username, analyzing, (runOutcome) => {
    setAnalyzing(false);
    void queryClient.invalidateQueries({ queryKey: ["allGames"] });
    void queryClient.invalidateQueries({ queryKey: ["openings"] });
    // A failed run or a lost stream stops the chain — the same broken
    // game or connection would otherwise re-fire an unbounded
    // fail -> re-read -> fail loop (it stays at the head of
    // games_needing_analysis). The user resumes manually with
    // "Analyze the rest".
    if (shouldChainAfterRun(runOutcome)) {
      void continueIfGapRemains();
    }
  });

  const coach = useMutation({
    mutationFn: (options: CoachOptions) =>
      api.coach(username, {
        agentId: activeAgentId ?? undefined,
        since,
        time_class: classParam,
        refresh: options.refresh ?? false,
      }),
  });

  // Advice is generated over a specific window/time-control; without
  // this, changing the filters leaves the previous window's advice on
  // screen (and the staleness check below would compare its
  // `games_analyzed` against a report fetched for a *different*
  // window, so "stale"/"fresh" would mean nothing). Reset on filter
  // change so the panel shows either advice for the current window or
  // nothing.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset on filter change only, not on every mutation identity change
  useEffect(() => {
    coach.reset();
  }, [windowDays, classParam]);

  const agentLabel = (id: string) =>
    agents.data?.agents.find((agent) => agent.id === id)?.label ?? id;

  const copyPrompt = async () => {
    if (coach.data) {
      await navigator.clipboard.writeText(coach.data.prompt);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const isRefreshing = coach.isPending && coach.variables?.refresh === true;
  const currentAnalyzed = report.data?.games_analyzed;
  const isStale =
    coach.data !== undefined &&
    currentAnalyzed !== undefined &&
    coach.data.games_analyzed < currentAnalyzed;

  // Report-scoped chat, carrying the same window/time-control filters
  // as the report and advice above it (docs/08-frontend.md). Memoized
  // so `useChat` only re-resolves the thread when the scope actually
  // changes, not on every render.
  const chatScope = useMemo(
    () => ({ scope: "report" as const, since, timeClass: classParam }),
    [since, classParam],
  );
  const chat = useChat(username, chatScope, activeAgentId ?? undefined);

  return (
    <Layout username={username}>
      <h1>Coach {username}</h1>
      <p>
        Builds a report from the analyzed games in the window below and asks
        your coach agent for prioritized training advice.
      </p>

      {games.isSuccess && (games.data?.length ?? 0) > 0 && (
        <>
          <StatsFilters
            windowDays={windowDays}
            setWindowDays={setWindowDays}
            timeClass={timeClass}
            setPickedClass={setPickedClass}
            classOptions={classOptions}
          />
          {report.isSuccess && gap === null && (
            <p className="agent-note">
              {report.data.games_analyzed} game
              {report.data.games_analyzed === 1 ? "" : "s"} analyzed
              {classParam !== undefined ? ` · ${classParam}` : " · all classes"}
            </p>
          )}
        </>
      )}

      {gap !== null && (
        <p role="alert">
          {gap.analyzed} of {gap.inScope} games in this window are analyzed —
          advice will only cover the analyzed games.{" "}
          <button
            type="button"
            className="btn-low-emphasis"
            disabled={analyzing || analyzeRest.isPending}
            onClick={() => analyzeRest.mutate()}
          >
            {analyzing ? "Analyzing…" : "Analyze the rest"}
          </button>
        </p>
      )}

      {analyzeRest.isError && !isRunConflict(analyzeRest.error) && (
        <p role="alert">{analyzeRest.error.message}</p>
      )}

      {progress && (
        <p className="progress-row">
          <progress value={progress.gamesDone} max={progress.gamesTotal} />{" "}
          {progress.gamesDone}/{progress.gamesTotal} games
          {progress.currentPly !== undefined
            ? ` — current game ply ${progress.currentPly}/${progress.totalPlies}`
            : ""}
          {progress.failed
            ? " — run failed, see server logs. Analyze the rest to try again."
            : ""}
          {progress.streamLost
            ? " — progress stream lost; the run may have been interrupted. Analyze the rest to continue."
            : ""}
        </p>
      )}

      {activeAgentId !== null && (
        <p className="agent-note">
          Using {agentLabel(activeAgentId)} — change in{" "}
          <Link to="/settings">Settings</Link>.
        </p>
      )}

      <button
        type="button"
        disabled={coach.isPending}
        onClick={() => coach.mutate({})}
      >
        {coach.isPending && !isRefreshing
          ? "Asking the coach… (can take a minute)"
          : "Get advice"}
      </button>

      {coach.isError && <p role="alert">{coach.error.message}</p>}

      {coach.isSuccess && (
        <>
          <p className="agent-note">
            Advice from {agentLabel(coach.data.agent_id)} ({coach.data.agent_id}
            ){" — "}
            {coach.data.cached ? "cached, generated " : "generated "}
            {new Date(coach.data.generated_at * 1000).toLocaleString()} over{" "}
            {coach.data.games_analyzed} game
            {coach.data.games_analyzed === 1 ? "" : "s"}{" "}
            <button
              type="button"
              className="explain-regenerate"
              disabled={coach.isPending}
              onClick={() => coach.mutate({ refresh: true })}
            >
              {isRefreshing ? "Regenerating…" : "Regenerate"}
            </button>
          </p>
          {isStale && (
            <p role="alert">
              This advice was generated over {coach.data.games_analyzed} games —
              you have {currentAnalyzed} analyzed now for this window.
              Regenerate for advice that covers your latest games.
            </p>
          )}
          <article className="advice">
            <Markdown components={gameLinkMarkdownComponents}>
              {coach.data.advice}
            </Markdown>
          </article>
          <details>
            <summary>
              The prompt that was sent{" "}
              <button type="button" onClick={() => void copyPrompt()}>
                {copied ? "copied ✓" : "copy"}
              </button>
            </summary>
            <pre className="prompt">{coach.data.prompt}</pre>
          </details>
        </>
      )}

      {activeAgentId !== null && (
        <section className="panel chat-panel-section">
          <ChatPanel
            state={chat.state}
            loading={chat.loading}
            onSend={chat.send}
            onNewChat={chat.newChat}
          />
        </section>
      )}
    </Layout>
  );
}
