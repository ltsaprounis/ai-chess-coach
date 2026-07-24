import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api.ts";
import {
  getStoredAgentId,
  resolveAgentId,
  setStoredAgentId,
} from "../coachAgent.ts";

/** The coach-LLM picker, persisted in localStorage (coachAgent.ts) and
 *  read by the Coach page and the in-game Explain button. Lives on the
 *  Settings page. Quiet while the roster loads or fails. */
export default function AgentSelect() {
  const [choice, setChoice] = useState(getStoredAgentId);
  const agents = useQuery({
    queryKey: ["coachAgents"],
    queryFn: api.coachAgents,
  });
  if (!agents.isSuccess) {
    return null;
  }
  const active = resolveAgentId(
    choice,
    agents.data.agents,
    agents.data.default,
  );
  return (
    <label>
      Coach agent{" "}
      <select
        value={active}
        onChange={(event) => {
          setStoredAgentId(event.target.value);
          setChoice(event.target.value);
        }}
      >
        {agents.data.agents.map((agent) => (
          <option key={agent.id} value={agent.id}>
            {agent.label} ({agent.model})
          </option>
        ))}
      </select>
    </label>
  );
}
