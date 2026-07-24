import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import Markdown from "react-markdown";
import { Link, useParams } from "react-router-dom";
import { api } from "../api.ts";
import { getStoredAgentId, resolveAgentId } from "../coachAgent.ts";
import Layout from "../components/Layout.tsx";

export default function Coach() {
  const { username = "" } = useParams();
  const [copied, setCopied] = useState(false);

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

  const coach = useMutation({
    mutationFn: () => api.coach(username, activeAgentId ?? undefined),
  });

  const agentLabel = (id: string) =>
    agents.data?.agents.find((agent) => agent.id === id)?.label ?? id;

  const copyPrompt = async () => {
    if (coach.data) {
      await navigator.clipboard.writeText(coach.data.prompt);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <Layout username={username}>
      <h1>Coach {username}</h1>
      <p>
        Builds a report from the analyzed games and asks your coach agent for
        prioritized training advice.
      </p>

      {activeAgentId !== null && (
        <p className="agent-note">
          Using {agentLabel(activeAgentId)} — change in{" "}
          <Link to="/settings">Settings</Link>.
        </p>
      )}

      <button
        type="button"
        disabled={coach.isPending}
        onClick={() => coach.mutate()}
      >
        {coach.isPending
          ? "Asking the coach… (can take a minute)"
          : "Get advice"}
      </button>

      {coach.isError && <p role="alert">{coach.error.message}</p>}

      {coach.isSuccess && (
        <>
          <p className="agent-note">
            Advice from {agentLabel(coach.data.agent_id)} ({coach.data.agent_id}
            )
          </p>
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
