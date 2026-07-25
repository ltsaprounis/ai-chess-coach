import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import Markdown from "react-markdown";
import { Link, useParams } from "react-router-dom";
import { api } from "../api.ts";
import { getStoredAgentId, resolveAgentId } from "../coachAgent.ts";
import Layout from "../components/Layout.tsx";
import StatsFilters from "../components/StatsFilters.tsx";
import { useStatsFilters } from "../useStatsFilters.ts";

type CoachOptions = { refresh?: boolean };

export default function Coach() {
  const { username = "" } = useParams();
  const [copied, setCopied] = useState(false);

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
    windowByTime,
    classOptions,
    timeClass,
    since,
    classParam,
  } = useStatsFilters(games.data ?? []);

  const scopedGameCount = useMemo(
    () =>
      classParam === undefined
        ? windowByTime.length
        : windowByTime.filter((game) => game.time_class === classParam).length,
    [windowByTime, classParam],
  );

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

  // The report over the same window/time-control, fetched only to read
  // its `games_analyzed` — the live count to compare a cached coach
  // report against, never rendered itself.
  const report = useQuery({
    queryKey: ["report", username, windowDays, timeClass],
    queryFn: () => api.report(username, { since, time_class: classParam }),
    enabled: games.isSuccess,
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
          <p className="agent-note">
            Covers {scopedGameCount} game{scopedGameCount === 1 ? "" : "s"}
            {classParam !== undefined ? ` · ${classParam}` : " · all classes"}
          </p>
        </>
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
            <Markdown>{coach.data.advice}</Markdown>
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
    </Layout>
  );
}
