import { describe, expect, it } from "vitest";
import {
  type ExplainState,
  explainReducer,
  initialExplainState,
} from "./explain";

function apply(
  actions: Parameters<typeof explainReducer>[1][],
  start: ExplainState = initialExplainState,
): ExplainState {
  return actions.reduce(explainReducer, start);
}

describe("explainReducer", () => {
  it("starts idle", () => {
    expect(initialExplainState).toEqual({
      status: "idle",
      ply: null,
      progress: [],
      text: "",
      error: null,
    });
  });

  it("start resets to streaming for the given ply", () => {
    const state = apply([{ type: "start", ply: 12 }]);
    expect(state).toEqual({
      status: "streaming",
      ply: 12,
      progress: [],
      text: "",
      error: null,
    });
  });

  it("accumulates text chunks in order", () => {
    const state = apply([
      { type: "start", ply: 3 },
      { type: "event", event: { type: "text", text: "The knight " } },
      { type: "event", event: { type: "text", text: "move is best." } },
    ]);
    expect(state.text).toBe("The knight move is best.");
    expect(state.status).toBe("streaming");
  });

  it("collects tool events as progress lines, not text", () => {
    const state = apply([
      { type: "start", ply: 3 },
      {
        type: "event",
        event: { type: "tool", text: "engine: analyzing fen1" },
      },
      { type: "event", event: { type: "text", text: "Good move." } },
      {
        type: "event",
        event: { type: "tool", text: "engine: analyzing fen2" },
      },
    ]);
    expect(state.progress).toEqual([
      { id: 0, text: "engine: analyzing fen1" },
      { id: 1, text: "engine: analyzing fen2" },
    ]);
    expect(state.text).toBe("Good move.");
  });

  it("done replaces the accumulated text with the full canonical text", () => {
    const state = apply([
      { type: "start", ply: 3 },
      { type: "event", event: { type: "text", text: "partial" } },
      { type: "done", payload: { text: "full markdown" } },
    ]);
    expect(state.status).toBe("done");
    expect(state.text).toBe("full markdown");
    expect(state.error).toBeNull();
  });

  it("a cached hit goes straight from streaming to done with no events", () => {
    const state = apply([
      { type: "start", ply: 7 },
      { type: "done", payload: { text: "cached markdown" } },
    ]);
    expect(state.status).toBe("done");
    expect(state.ply).toBe(7);
    expect(state.text).toBe("cached markdown");
  });

  it("error marks the stream failed and keeps the message", () => {
    const state = apply([
      { type: "start", ply: 5 },
      { type: "event", event: { type: "text", text: "partial" } },
      { type: "error", message: "provider timed out" },
    ]);
    expect(state.status).toBe("error");
    expect(state.error).toBe("provider timed out");
  });

  it("a fresh start for a new ply clears prior progress and text", () => {
    const first = apply([
      { type: "start", ply: 3 },
      { type: "event", event: { type: "tool", text: "engine: analyzing" } },
      { type: "done", payload: { text: "explanation for ply 3" } },
    ]);
    const second = explainReducer(first, { type: "start", ply: 9 });
    expect(second).toEqual({
      status: "streaming",
      ply: 9,
      progress: [],
      text: "",
      error: null,
    });
  });
});
