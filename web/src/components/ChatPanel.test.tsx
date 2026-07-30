import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ChatState } from "../chat";
import { initialChatState } from "../chat";
import ChatPanel from "./ChatPanel";

// The test environment runs without a DOM (vite.config.ts's Vitest
// `environment: "node"`, matching Coach.test.tsx's approach), so these
// assert on rendered markup for a given `ChatState` rather than
// simulating real typing/clicks — the same "no jsdom" constraint
// Coach.test.tsx already works within.

function render(state: ChatState, loading = false): string {
  return renderToStaticMarkup(
    <ChatPanel
      state={state}
      loading={loading}
      onSend={() => {}}
      onNewChat={() => {}}
    />,
  );
}

describe("ChatPanel", () => {
  it("shows a loading note before any transcript exists", () => {
    const html = render(initialChatState, true);
    expect(html).toContain("Loading chat…");
  });

  it("suppresses the loading note once a transcript is on screen", () => {
    const state: ChatState = {
      ...initialChatState,
      messages: [{ role: "user", content: "hi", created_at: 1 }],
    };
    const html = render(state, true);
    expect(html).not.toContain("Loading chat…");
  });

  it("renders user and assistant turns with role labels", () => {
    const state: ChatState = {
      ...initialChatState,
      messages: [
        { role: "user", content: "What about Nxe5?", created_at: 1 },
        { role: "assistant", content: "That drops a pawn.", created_at: 2 },
      ],
    };
    const html = render(state);
    expect(html).toContain("What about Nxe5?");
    expect(html).toContain("That drops a pawn.");
    expect(html).toContain(">You<");
    expect(html).toContain(">Coach<");
  });

  it("opens an assistant reply's game-citation link in a new tab", () => {
    const state: ChatState = {
      ...initialChatState,
      messages: [
        {
          role: "assistant",
          content:
            "See [that blunder][g1] for the pattern.\n\n[g1]: /games/abc-123:leo?ply=25\n",
          created_at: 1,
        },
      ],
    };
    const html = render(state);
    const anchor = html.match(/<a\b[^>]*>[^<]*<\/a>/)?.[0];
    expect(anchor).toBeDefined();
    expect(anchor).toContain('href="/games/abc-123:leo?ply=25"');
    expect(anchor).toContain('target="_blank"');
    expect(anchor).toContain('rel="noreferrer"');
  });

  it("shows the pending user turn and tool progress while streaming", () => {
    const state: ChatState = {
      ...initialChatState,
      status: "streaming",
      pendingText: "and if I take the knight?",
      progress: [{ id: 0, text: "find_games: opponent=marko77" }],
    };
    const html = render(state);
    expect(html).toContain("and if I take the knight?");
    expect(html).toContain("find_games: opponent=marko77");
    expect(html).toContain("thinking…");
    // Streaming disables the input and relabels Send.
    expect(html).toMatch(/<textarea[^>]*disabled(=""|\/?>| )/);
    expect(html).toContain("Sending…");
  });

  it("renders accumulated streaming text as it arrives", () => {
    const state: ChatState = {
      ...initialChatState,
      status: "streaming",
      pendingText: "why?",
      streamingText: "Because the knight is pinned.",
    };
    const html = render(state);
    expect(html).toContain("Because the knight is pinned.");
    expect(html).not.toContain("thinking…");
  });

  it("shows the error banner for a plain mid-stream failure", () => {
    const state: ChatState = {
      ...initialChatState,
      status: "error",
      error: "provider timed out",
      pendingText: "ping",
    };
    const html = render(state);
    expect(html).toContain('role="alert"');
    expect(html).toContain("provider timed out");
    expect(html).not.toContain("message limit");
  });

  it("shows the message-cap notice instead of the plain error when capped", () => {
    const state: ChatState = {
      ...initialChatState,
      status: "error",
      error:
        "This chat has reached its message limit. Start a new chat to continue.",
      capReached: true,
      pendingText: "one more question",
    };
    const html = render(state);
    expect(html).toContain('role="alert"');
    expect(html).toContain("This chat has reached its message limit.");
    expect(html).toContain("Start a new chat");
    // The raw hook error string isn't duplicated — the panel renders
    // its own canned cap message instead.
    expect(html).not.toContain("Start a new chat to continue.");
  });

  it("disables sending once capped even outside an active stream", () => {
    const state: ChatState = {
      ...initialChatState,
      status: "error",
      capReached: true,
      error: "capped",
    };
    const html = render(state);
    expect(html).toMatch(/<textarea[^>]*disabled(=""|\/?>| )/);
  });

  it("always offers a New chat action", () => {
    const html = render(initialChatState);
    expect(html).toContain("New chat");
  });
});
