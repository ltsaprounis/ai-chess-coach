import { describe, expect, it } from "vitest";
import type { ChatThreadSummary } from "./api.ts";
import {
  type ChatState,
  chatReducer,
  findMatchingThread,
  initialChatState,
  visibleChatProgress,
} from "./chat";

function apply(
  actions: Parameters<typeof chatReducer>[1][],
  start: ChatState = initialChatState,
): ChatState {
  return actions.reduce(chatReducer, start);
}

describe("chatReducer", () => {
  it("starts idle with an empty transcript", () => {
    expect(initialChatState).toEqual({
      messages: [],
      status: "idle",
      pendingText: null,
      progress: [],
      streamingText: "",
      error: null,
      capReached: false,
    });
  });

  it("hydrate replaces the transcript wholesale", () => {
    const state = apply([
      {
        type: "hydrate",
        messages: [
          { role: "user", content: "hi", created_at: 1 },
          { role: "assistant", content: "hello", created_at: 2 },
        ],
      },
    ]);
    expect(state.messages).toHaveLength(2);
    expect(state.status).toBe("idle");
  });

  it("send marks streaming and shows the pending user text", () => {
    const state = apply([{ type: "send", text: "what about Nxe5?" }]);
    expect(state).toEqual({
      messages: [],
      status: "streaming",
      pendingText: "what about Nxe5?",
      progress: [],
      streamingText: "",
      error: null,
      capReached: false,
    });
  });

  it("accumulates text chunks in order", () => {
    const state = apply([
      { type: "send", text: "why?" },
      { type: "event", event: { type: "text", text: "Because the " } },
      { type: "event", event: { type: "text", text: "knight is pinned." } },
    ]);
    expect(state.streamingText).toBe("Because the knight is pinned.");
    expect(state.status).toBe("streaming");
  });

  it("collects tool events as progress lines, not streaming text", () => {
    const state = apply([
      { type: "send", text: "check the game" },
      {
        type: "event",
        event: { type: "tool", text: "find_games: opponent=marko77" },
      },
      { type: "event", event: { type: "text", text: "Found it." } },
      { type: "event", event: { type: "tool", text: "get_game: g1" } },
    ]);
    expect(state.progress).toEqual([
      { id: 0, text: "find_games: opponent=marko77" },
      { id: 1, text: "get_game: g1" },
    ]);
    expect(state.streamingText).toBe("Found it.");
  });

  it("done reconciles the pending user turn and the reply into the transcript", () => {
    const state = apply([
      { type: "send", text: "and if I take the knight?" },
      { type: "event", event: { type: "text", text: "partial" } },
      {
        type: "done",
        payload: { text: "Then you win a pawn.", provider_state: "resume-1" },
      },
    ]);
    expect(state.status).toBe("idle");
    expect(state.pendingText).toBeNull();
    expect(state.streamingText).toBe("");
    expect(state.progress).toEqual([]);
    expect(state.messages).toHaveLength(2);
    expect(state.messages[0]).toMatchObject({
      role: "user",
      content: "and if I take the knight?",
    });
    expect(state.messages[1]).toMatchObject({
      role: "assistant",
      content: "Then you win a pawn.",
    });
  });

  it("done appends onto an already-hydrated transcript rather than replacing it", () => {
    const hydrated = apply([
      {
        type: "hydrate",
        messages: [{ role: "user", content: "earlier turn", created_at: 1 }],
      },
    ]);
    const state = apply(
      [
        { type: "send", text: "follow-up" },
        { type: "done", payload: { text: "reply", provider_state: null } },
      ],
      hydrated,
    );
    expect(state.messages).toHaveLength(3);
    expect(state.messages[0]?.content).toBe("earlier turn");
  });

  it("a mid-stream error keeps the pending text and progress for diagnosis", () => {
    const state = apply([
      { type: "send", text: "ping" },
      { type: "event", event: { type: "tool", text: "engine: analyzing" } },
      { type: "error", message: "provider timed out" },
    ]);
    expect(state.status).toBe("error");
    expect(state.error).toBe("provider timed out");
    expect(state.pendingText).toBe("ping");
    expect(state.capReached).toBe(false);
  });

  it("a cap error sets capReached alongside the error message", () => {
    const state = apply([
      { type: "send", text: "one more question" },
      {
        type: "cap",
        message: "This chat has reached its message limit.",
      },
    ]);
    expect(state.status).toBe("error");
    expect(state.capReached).toBe(true);
    expect(state.error).toBe("This chat has reached its message limit.");
    expect(state.pendingText).toBe("one more question");
  });

  it("reset clears everything back to the initial state", () => {
    const busy = apply([
      { type: "send", text: "x" },
      { type: "cap", message: "capped" },
    ]);
    expect(chatReducer(busy, { type: "reset" })).toEqual(initialChatState);
  });

  it("a fresh send after a cap clears the prior cap/error state", () => {
    const capped = apply([
      { type: "send", text: "first" },
      { type: "cap", message: "capped" },
    ]);
    const state = chatReducer(capped, { type: "send", text: "second" });
    expect(state.capReached).toBe(false);
    expect(state.error).toBeNull();
    expect(state.pendingText).toBe("second");
    expect(state.status).toBe("streaming");
  });
});

describe("visibleChatProgress", () => {
  const progress = [{ id: 0, text: "find_games: opponent=marko77" }];

  it("shows progress lines while streaming", () => {
    const state = apply([
      { type: "send", text: "x" },
      {
        type: "event",
        event: { type: "tool", text: "find_games: opponent=marko77" },
      },
    ]);
    expect(visibleChatProgress(state)).toEqual(progress);
  });

  it("keeps progress lines visible after a mid-stream error", () => {
    const state = apply([
      { type: "send", text: "x" },
      {
        type: "event",
        event: { type: "tool", text: "find_games: opponent=marko77" },
      },
      { type: "error", message: "boom" },
    ]);
    expect(visibleChatProgress(state)).toEqual(progress);
  });

  it("hides progress lines once the turn is done", () => {
    const state = apply([
      { type: "send", text: "x" },
      {
        type: "event",
        event: { type: "tool", text: "find_games: opponent=marko77" },
      },
      { type: "done", payload: { text: "reply", provider_state: null } },
    ]);
    expect(visibleChatProgress(state)).toEqual([]);
  });

  it("is empty before the first send", () => {
    expect(visibleChatProgress(initialChatState)).toEqual([]);
  });
});

function thread(
  partial: Partial<ChatThreadSummary> & Pick<ChatThreadSummary, "scope">,
): ChatThreadSummary {
  return {
    id: "t-default",
    game_id: null,
    ply: null,
    since: 0,
    until: 0,
    time_class: "",
    agent_id: "claude",
    title: "a thread",
    messages: 2,
    updated_at: 1_700_000_000,
    ...partial,
  };
}

describe("findMatchingThread", () => {
  it("matches a game thread by (game_id, ply, agent)", () => {
    const threads = [
      thread({
        id: "wrong-ply",
        scope: "game",
        game_id: "g1",
        ply: 10,
        agent_id: "claude",
      }),
      thread({
        id: "right",
        scope: "game",
        game_id: "g1",
        ply: 12,
        agent_id: "claude",
      }),
      thread({
        id: "wrong-game",
        scope: "game",
        game_id: "g2",
        ply: 12,
        agent_id: "claude",
      }),
    ];
    const match = findMatchingThread(threads, {
      scope: "game",
      gameId: "g1",
      ply: 12,
      agentId: "claude",
    });
    expect(match?.id).toBe("right");
  });

  it("returns the first (newest) match when several threads qualify", () => {
    const threads = [
      thread({
        id: "newest",
        scope: "game",
        game_id: "g1",
        ply: 12,
        agent_id: "claude",
        updated_at: 2,
      }),
      thread({
        id: "older",
        scope: "game",
        game_id: "g1",
        ply: 12,
        agent_id: "claude",
        updated_at: 1,
      }),
    ];
    const match = findMatchingThread(threads, {
      scope: "game",
      gameId: "g1",
      ply: 12,
      agentId: "claude",
    });
    expect(match?.id).toBe("newest");
  });

  it("matches a report thread by (since, until, time_class, agent)", () => {
    const threads = [
      thread({
        id: "wrong-window",
        scope: "report",
        since: 1_600_000_000,
        until: 0,
        time_class: "rapid",
        agent_id: "claude",
      }),
      thread({
        id: "right",
        scope: "report",
        since: 1_700_000_000,
        until: 0,
        time_class: "rapid",
        agent_id: "claude",
      }),
      thread({
        id: "wrong-class",
        scope: "report",
        since: 1_700_000_000,
        until: 0,
        time_class: "blitz",
        agent_id: "claude",
      }),
    ];
    const match = findMatchingThread(threads, {
      scope: "report",
      since: 1_700_000_000,
      until: 0,
      timeClass: "rapid",
      agentId: "claude",
    });
    expect(match?.id).toBe("right");
  });

  it("never matches a thread pinned to a different agent", () => {
    const threads = [
      thread({
        id: "other-agent",
        scope: "report",
        since: 0,
        until: 0,
        time_class: "",
        agent_id: "copilot",
      }),
    ];
    const match = findMatchingThread(threads, {
      scope: "report",
      since: 0,
      until: 0,
      timeClass: "",
      agentId: "claude",
    });
    expect(match).toBeNull();
  });

  it("never matches a thread of the other scope", () => {
    const threads = [
      thread({
        id: "report-thread",
        scope: "report",
        since: 0,
        until: 0,
        time_class: "",
        agent_id: "claude",
      }),
    ];
    const match = findMatchingThread(threads, {
      scope: "game",
      gameId: "g1",
      ply: 1,
      agentId: "claude",
    });
    expect(match).toBeNull();
  });

  it("returns null with no threads at all", () => {
    expect(
      findMatchingThread([], {
        scope: "report",
        since: 0,
        until: 0,
        timeClass: "",
        agentId: "claude",
      }),
    ).toBeNull();
  });
});
