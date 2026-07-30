// Pure chat state logic for the shared ChatPanel (Game and Coach
// pages, docs/08-frontend.md's coach-chat addition) — no fetching, no
// React, unit-tested in chat.test.ts. Mirrors explain.ts's split
// between a pure reducer/selectors and the useChat hook (useChat.ts)
// that wires SSE and thread resolution around it.

import type { ChatMessage, ChatThreadSummary } from "./api.ts";

/**
 * One streamed `text`/`tool` increment of a chat reply from
 * `POST /api/chat/threads/{id}/messages`. Hand-declared because SSE
 * payloads are not part of the OpenAPI schema; mirrors the backend's
 * `ChatEvent` model (`text`: a markdown chunk; `tool`: a short
 * tool-call summary) — same shape as explain's `ExplainEvent`.
 */
export type ChatStreamEvent = {
  type: "text" | "tool";
  text: string;
};

/**
 * Terminal `done` SSE payload — the full markdown reply, already
 * persisted server-side along with the provider's opaque resume token
 * (`provider_state`; null when the provider can't resume, or replayed
 * from scratch). Mirrors the backend's `ChatEvent` model at
 * `type="done"` (docs/future-improvements/coach-chat.md, "The
 * provider seam").
 */
export type ChatDone = {
  text: string;
  provider_state: string | null;
};

/**
 * Mid-stream `error` SSE payload — a provider failure after streaming
 * had already begun; nothing is persisted server-side and the
 * thread's `provider_state` is cleared. Mirrors the same `{message}`
 * convention as explain's `ExplainError`.
 */
export type ChatStreamError = {
  message: string;
};

export type ChatTurnStatus = "idle" | "streaming" | "error";

/** One progress line; `id` is a stable React key (tool text may repeat). */
export type ProgressLine = { id: number; text: string };

export type ChatState = {
  /** Committed transcript, oldest first — hydrated from
   *  `GET /chat/threads/{id}` on reopen, or empty for a thread not
   *  yet created (no send has landed in this mount). */
  messages: ChatMessage[];
  status: ChatTurnStatus;
  /** The user's text for the turn currently in flight or that just
   *  failed — shown ahead of `messages` until `done` reconciles it
   *  into the transcript. Null with no in-flight/failed turn. */
  pendingText: string | null;
  /** Tool-call summaries for the in-flight turn, oldest first. */
  progress: ProgressLine[];
  /** Accumulated assistant markdown for the in-flight turn. */
  streamingText: string;
  error: string | null;
  /** Set on a 409 "message cap" response (docs/07-api.md): sending is
   *  disabled until "New chat" starts a fresh thread. */
  capReached: boolean;
};

export const initialChatState: ChatState = {
  messages: [],
  status: "idle",
  pendingText: null,
  progress: [],
  streamingText: "",
  error: null,
  capReached: false,
};

export type ChatAction =
  | { type: "hydrate"; messages: ChatMessage[] }
  | { type: "send"; text: string }
  | { type: "event"; event: ChatStreamEvent }
  | { type: "done"; payload: ChatDone }
  | { type: "error"; message: string }
  | { type: "cap"; message: string }
  | { type: "reset" };

/**
 * Folds one chat turn's SSE lifecycle into the panel state shown on
 * both mounts (Game page "Ask a follow-up", Coach page report chat).
 */
export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "hydrate":
      // A thread reopen replaces the transcript outright; hydrate only
      // ever runs before the first send in a given mount, so there is
      // no in-flight turn to preserve.
      return { ...initialChatState, messages: action.messages };
    case "send":
      return {
        ...state,
        status: "streaming",
        pendingText: action.text,
        progress: [],
        streamingText: "",
        error: null,
        capReached: false,
      };
    case "event":
      return action.event.type === "tool"
        ? {
            ...state,
            progress: [
              ...state.progress,
              { id: state.progress.length, text: action.event.text },
            ],
          }
        : { ...state, streamingText: state.streamingText + action.event.text };
    case "done": {
      // The backend persists both turns before emitting `done` but the
      // SSE payload carries only the reply text — created_at is
      // display-only (the transcript's order is the array order), so a
      // client-side timestamp is fine here rather than round-tripping
      // for it.
      const now = Math.floor(Date.now() / 1000);
      const userTurn: ChatMessage = {
        role: "user",
        content: state.pendingText ?? "",
        created_at: now,
      };
      const assistantTurn: ChatMessage = {
        role: "assistant",
        content: action.payload.text,
        created_at: now,
      };
      return {
        ...state,
        status: "idle",
        messages: [...state.messages, userTurn, assistantTurn],
        pendingText: null,
        progress: [],
        streamingText: "",
        error: null,
      };
    }
    case "error":
      return { ...state, status: "error", error: action.message };
    case "cap":
      return {
        ...state,
        status: "error",
        error: action.message,
        capReached: true,
      };
    case "reset":
      return initialChatState;
    default:
      return state;
  }
}

/**
 * Progress lines to display: visible while a reply is streaming or
 * just failed (so the tool trail stays on screen for diagnosis),
 * cleared once the turn completes — mirrors explain's
 * `visibleProgress`.
 */
export function visibleChatProgress(state: ChatState): ProgressLine[] {
  return state.status === "streaming" || state.status === "error"
    ? state.progress
    : [];
}

/**
 * The mount's scope identity to match against existing threads: a
 * game anchored at a specific ply, or a report window/time-control —
 * plus the agent, since threads are pinned to their agent at creation
 * (docs/08-frontend.md) and a changed selection must never resume a
 * thread minted under a different one.
 */
export type ChatScopeCriteria =
  | { scope: "game"; gameId: string; ply: number; agentId: string }
  | {
      scope: "report";
      since: number;
      until: number;
      timeClass: string;
      agentId: string;
    };

/**
 * The newest thread matching the mount's scope and agent, or `null`
 * when none exists yet (the caller creates one on first send).
 * `threads` is expected pre-sorted newest-updated-first, as
 * `GET /players/{u}/chat/threads` returns it, so the first match found
 * is the one to reopen — "the newest existing thread matching (game,
 * ply, agent)" / "(since, until, time_class, agent)" per
 * docs/08-frontend.md.
 */
export function findMatchingThread(
  threads: readonly ChatThreadSummary[],
  criteria: ChatScopeCriteria,
): ChatThreadSummary | null {
  return threads.find((thread) => matchesScope(thread, criteria)) ?? null;
}

function matchesScope(
  thread: ChatThreadSummary,
  criteria: ChatScopeCriteria,
): boolean {
  if (thread.scope !== criteria.scope || thread.agent_id !== criteria.agentId) {
    return false;
  }
  return criteria.scope === "game"
    ? thread.game_id === criteria.gameId && thread.ply === criteria.ply
    : thread.since === criteria.since &&
        thread.until === criteria.until &&
        thread.time_class === criteria.timeClass;
}
