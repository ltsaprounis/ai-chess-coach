import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api.ts";
import {
  getStoredAgentId,
  resolveAgentId,
  setStoredAgentId,
} from "../coachAgent.ts";

export default function Home() {
  const [username, setUsername] = useState("");
  const [agentChoice, setAgentChoice] = useState(getStoredAgentId);
  const navigate = useNavigate();
  const sync = useMutation({
    mutationFn: api.sync,
    onSuccess: (_result, user) => navigate(`/players/${user}/games`),
  });
  // Quiet while loading or failed — the sync form never depends on it.
  const agents = useQuery({
    queryKey: ["coachAgents"],
    queryFn: api.coachAgents,
  });

  const chooseAgent = (id: string) => {
    setStoredAgentId(id);
    setAgentChoice(id);
  };

  return (
    <main className="page">
      <h1>AI Chess Coach</h1>
      <p>Enter a chess.com username to fetch their games.</p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const user = username.trim().toLowerCase();
          if (user) {
            sync.mutate(user);
          }
        }}
      >
        <input
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          placeholder="chess.com username"
          aria-label="chess.com username"
        />
        <button type="submit" disabled={sync.isPending}>
          {sync.isPending ? "Syncing…" : "Sync games"}
        </button>
      </form>
      {agents.isSuccess && (
        <p className="agent-row">
          <label>
            Coach agent{" "}
            <select
              value={resolveAgentId(
                agentChoice,
                agents.data.agents,
                agents.data.default,
              )}
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
      {sync.isError && <p role="alert">{sync.error.message}</p>}
    </main>
  );
}
