import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import Markdown from "react-markdown";
import { Link, useParams } from "react-router-dom";
import { api } from "../api.ts";
import {
  getStoredAgentId,
  resolveAgentId,
  setStoredAgentId,
} from "../coachAgent.ts";

export default function Coach() {
  const { username = "" } = useParams();
  const [copied, setCopied] = useState(false);
  const [agentChoice, setAgentChoice] = useState(getStoredAgentId);

  // Quiet while loading or failed — coaching works without the
  // roster: no agent_id sent means the server picks its default.
  const agents = useQuery({
    queryKey: ["coachAgents"],
    queryFn: api.coachAgents,
  });
  const activeAgentId = agents.data
    ? resolveAgentId(agentChoice, agents.data.agents, agents.data.default)
    : null;

  const coach = useMutation({
    mutationFn: () => api.coach(username, activeAgentId ?? undefined),
  });

  const chooseAgent = (id: string) => {
    setStoredAgentId(id);
    setAgentChoice(id);
  };

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
    <main className="page">
      <p>
        <Link to={`/players/${username}/games`}>← games</Link>
        {" · "}
        <Link to={`/players/${username}/dashboard`}>dashboard</Link>
      </p>
      <h1>Coach {username}</h1>
      <p>
        Builds a report from the analyzed games and asks the selected agent for
        prioritized training advice.
      </p>

      {agents.isSuccess && (
        <p className="agent-row">
          <label>
            Agent{" "}
            <select
              value={activeAgentId ?? agents.data.default}
              onChange={(event) => chooseAgent(event.target.value)}
            >
              {agents.data.agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.label} ({agent.model})
                </option>
              ))}
            </select>
          </label>
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
    </main>
  );
}
