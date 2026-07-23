// The player's coach-agent choice, kept in localStorage so Home and
// Coach share it across visits. Pure + injectable, unit-tested in
// coachAgent.test.ts.

const STORAGE_KEY = "coachAgentId";

type AgentStore = Pick<Storage, "getItem" | "setItem">;

/** The stored choice, or null when unset or storage is unavailable. */
export function getStoredAgentId(
  store: AgentStore = localStorage,
): string | null {
  try {
    return store.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setStoredAgentId(
  id: string,
  store: AgentStore = localStorage,
): void {
  try {
    store.setItem(STORAGE_KEY, id);
  } catch {
    // Storage blocked (private mode): the choice just doesn't persist.
  }
}

/** The stored id while the roster still has it, else the server default. */
export function resolveAgentId(
  stored: string | null,
  agents: readonly { id: string }[],
  serverDefault: string,
): string {
  if (stored !== null && agents.some((agent) => agent.id === stored)) {
    return stored;
  }
  return serverDefault;
}
