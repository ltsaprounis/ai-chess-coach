import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import {
  api,
  type ChatThreadCreateRequest,
  chatMessageUrl,
  type TimeClass,
} from "./api.ts";
import {
  type ChatDone,
  type ChatScopeCriteria,
  type ChatState,
  type ChatStreamError,
  type ChatStreamEvent,
  chatReducer,
  findMatchingThread,
  initialChatState,
} from "./chat.ts";
import { consumeSse, errorMessage } from "./sse.ts";

/**
 * The mount's scope identity: a game anchored at a specific ply (the
 * Game page's "Ask a follow-up", only shown once a ply is selected —
 * same gating as Explain), or a report window/time-control (the Coach
 * page). Callers should build this with `useMemo` keyed on its own
 * fields — `useChat` re-resolves the thread whenever this object's
 * *reference* changes, so a fresh literal every render would re-fetch
 * on every render instead of only on a genuine scope change.
 */
export type ChatMountScope =
  | { scope: "game"; gameId: string; ply: number }
  | { scope: "report"; since?: number; timeClass?: TimeClass };

export type UseChatResult = {
  state: ChatState;
  /** True while resolving which thread to reopen (the list + hydrate
   *  GETs) — never an LLM call. */
  loading: boolean;
  send: (text: string) => void;
  newChat: () => void;
};

const CAP_MESSAGE =
  "This chat has reached its message limit. Start a new chat to continue.";

function criteriaFor(
  scope: ChatMountScope,
  agentId: string,
): ChatScopeCriteria {
  return scope.scope === "game"
    ? { scope: "game", gameId: scope.gameId, ply: scope.ply, agentId }
    : {
        scope: "report",
        since: scope.since ?? 0,
        until: 0,
        timeClass: scope.timeClass ?? "",
        agentId,
      };
}

function createBody(
  scope: ChatMountScope,
  agentId: string,
): ChatThreadCreateRequest {
  return scope.scope === "game"
    ? {
        scope: "game",
        agent_id: agentId,
        game_id: scope.gameId,
        ply: scope.ply,
      }
    : {
        scope: "report",
        agent_id: agentId,
        since: scope.since ?? null,
        time_class: scope.timeClass ?? null,
      };
}

/**
 * Drives one `ChatPanel` mount: resolves (or creates) the thread for
 * the given scope + agent, hydrates its transcript, and streams
 * `POST /chat/threads/{id}/messages` on each send — the same
 * fetch-based SSE consumption `useExplain` uses (`sse.ts`). Nothing
 * here spends an LLM call on mount: thread resolution is a `GET
 * /chat/threads` list plus a `GET /chat/threads/{id}` hydrate, both
 * cheap, per docs/08-frontend.md ("Every LLM call is an explicit
 * send").
 */
export function useChat(
  username: string,
  scope: ChatMountScope,
  agentId: string | undefined,
): UseChatResult {
  const queryClient = useQueryClient();
  const [state, dispatch] = useReducer(chatReducer, initialChatState);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [hydrating, setHydrating] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  // Guards a hydrate response landing after the scope/agent (or a
  // "New chat") has already moved on.
  const resolveTokenRef = useRef(0);
  // Set by "New chat" so a background refetch of the thread list
  // (e.g. window refocus) can't silently re-attach the abandoned
  // thread before the student has sent a fresh message under a truly
  // new one. Cleared on a genuine scope/agent change (a different
  // conversation to resolve regardless of any prior "New chat") and
  // once a new thread actually exists for this mount.
  const skipResolutionRef = useRef(false);

  const threadsQuery = useQuery({
    queryKey: ["chatThreads", username],
    queryFn: () => api.chatThreads(username),
    // `username` is empty on the Game page until the game (and its
    // owning player) has loaded — nothing to resolve yet.
    enabled: agentId !== undefined && username !== "",
  });

  // Cleared on a genuine scope/agent change (a different conversation
  // to resolve regardless of any prior "New chat") rather than read
  // inside the effect, hence the dependency-only usage below.
  // biome-ignore lint/correctness/useExhaustiveDependencies: clear the override only on a genuine scope/agent change, not on every render
  useEffect(() => {
    skipResolutionRef.current = false;
  }, [scope, agentId]);

  // Resolve which thread this mount should show whenever the scope,
  // agent, or thread list changes — reopening the newest match at no
  // LLM cost, or leaving the transcript empty for a thread created on
  // first send (docs/08-frontend.md).
  useEffect(() => {
    if (
      agentId === undefined ||
      !threadsQuery.isSuccess ||
      skipResolutionRef.current
    ) {
      return;
    }
    const token = ++resolveTokenRef.current;
    const match = findMatchingThread(
      threadsQuery.data,
      criteriaFor(scope, agentId),
    );
    if (match === null) {
      setThreadId(null);
      dispatch({ type: "reset" });
      return;
    }
    setThreadId(match.id);
    setHydrating(true);
    void api.chatThread(match.id).then(
      (detail) => {
        if (resolveTokenRef.current === token) {
          dispatch({ type: "hydrate", messages: detail.messages });
          setHydrating(false);
        }
      },
      () => {
        if (resolveTokenRef.current === token) {
          setHydrating(false);
        }
      },
    );
  }, [agentId, scope, threadsQuery.isSuccess, threadsQuery.data]);

  const send = useCallback(
    (text: string) => {
      if (agentId === undefined || state.status === "streaming") {
        return;
      }
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      resolveTokenRef.current += 1; // invalidate any in-flight hydrate
      dispatch({ type: "send", text });

      void (async () => {
        let id = threadId;
        if (id === null) {
          try {
            const created = await api.createChatThread(
              username,
              createBody(scope, agentId),
            );
            id = created.id;
          } catch (err) {
            if (!controller.signal.aborted) {
              dispatch({
                type: "error",
                message: err instanceof Error ? err.message : String(err),
              });
            }
            return;
          }
          skipResolutionRef.current = false;
          setThreadId(id);
        }

        let response: Response;
        try {
          response = await fetch(chatMessageUrl(id), {
            method: "POST",
            signal: controller.signal,
            headers: {
              "Content-Type": "application/json",
              Accept: "text/event-stream",
            },
            body: JSON.stringify({ text }),
          });
        } catch (err) {
          if (!controller.signal.aborted) {
            dispatch({ type: "error", message: (err as Error).message });
          }
          return;
        }

        if (!response.ok) {
          if (response.status === 409) {
            // Two distinct 409s (docs/07-api.md): the message cap, which
            // permanently disables this thread's input, and "a reply is
            // already streaming" (reachable from a second tab), which is
            // transient and must not lock the panel into the cap state.
            const message = await errorMessage(response);
            if (message.includes("message cap")) {
              dispatch({ type: "cap", message: CAP_MESSAGE });
            } else {
              dispatch({ type: "error", message });
            }
          } else {
            dispatch({ type: "error", message: await errorMessage(response) });
          }
          return;
        }
        if (response.body === null) {
          dispatch({ type: "error", message: "empty chat reply stream" });
          return;
        }

        try {
          let sawDone = false;
          const outcome = await consumeSse(response.body, (raw) => {
            if (raw.event === "done") {
              sawDone = true;
              dispatch({
                type: "done",
                payload: JSON.parse(raw.data) as ChatDone,
              });
              return false;
            }
            if (raw.event === "error") {
              const payload = JSON.parse(raw.data) as ChatStreamError;
              dispatch({ type: "error", message: payload.message });
              return false;
            }
            if (raw.event === "text" || raw.event === "tool") {
              dispatch({
                type: "event",
                event: JSON.parse(raw.data) as ChatStreamEvent,
              });
            }
          });
          if (outcome === "ended" && !controller.signal.aborted) {
            dispatch({
              type: "error",
              message: "chat reply stream ended unexpectedly — try again",
            });
          }
          if (sawDone) {
            // Cheap GET refresh only — title/message-count/updated_at
            // for the thread list, never another LLM call.
            void queryClient.invalidateQueries({
              queryKey: ["chatThreads", username],
            });
          }
        } catch (err) {
          if (!controller.signal.aborted) {
            dispatch({ type: "error", message: (err as Error).message });
          }
        }
      })();
    },
    [agentId, threadId, scope, username, queryClient, state.status],
  );

  const newChat = useCallback(() => {
    abortRef.current?.abort();
    resolveTokenRef.current += 1;
    skipResolutionRef.current = true;
    setThreadId(null);
    dispatch({ type: "reset" });
  }, []);

  useEffect(() => () => abortRef.current?.abort(), []);

  return {
    state,
    loading: threadsQuery.isLoading || hydrating,
    send,
    newChat,
  };
}
