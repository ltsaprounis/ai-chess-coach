import { useState } from "react";
import Markdown from "react-markdown";
import { type ChatState, visibleChatProgress } from "../chat.ts";
import { gameLinkMarkdownComponents } from "./markdownLinks.tsx";

type Props = {
  state: ChatState;
  /** True while the thread is being resolved/hydrated (cheap GETs,
   *  never an LLM call) — shown only before any transcript exists. */
  loading: boolean;
  onSend: (text: string) => void;
  onNewChat: () => void;
};

/**
 * Shared follow-up chat panel, mounted on both the Game page ("Ask a
 * follow-up", game-scoped) and the Coach page (report-scoped) per
 * docs/08-frontend.md. Purely presentational — `useChat` owns thread
 * resolution and the SSE stream; this component only renders
 * `ChatState` and forwards `onSend`/`onNewChat`, mirroring how
 * `ExplainPanel` sits apart from `useExplain`.
 *
 * Assistant replies render as markdown with the same new-tab
 * game-link behavior as the Coach advice (`gameLinkMarkdownComponents`)
 * since their citations are the same app-relative `/games/{id}?ply=`
 * links, this time minted by the model from tool results. Tool events
 * show as transient progress lines while streaming, cleared on
 * completion — the same pattern `ExplainPanel` uses. Sending is
 * explicit only: nothing here fires a request until the student
 * presses Send.
 */
export default function ChatPanel({
  state,
  loading,
  onSend,
  onNewChat,
}: Props) {
  const [draft, setDraft] = useState("");
  const streaming = state.status === "streaming";
  const inputDisabled = streaming || state.capReached;

  const submit = () => {
    const text = draft.trim();
    if (text === "" || inputDisabled) {
      return;
    }
    setDraft("");
    onSend(text);
  };

  const hasTranscript = state.messages.length > 0 || state.pendingText !== null;

  return (
    <div className="chat-panel">
      <div className="explain-header">
        <h3 className="explain-title">Chat</h3>
        <button
          type="button"
          className="explain-regenerate"
          onClick={onNewChat}
        >
          New chat
        </button>
      </div>

      {loading && !hasTranscript && (
        <p className="explain-progress">Loading chat…</p>
      )}

      {hasTranscript && (
        <ul className="chat-transcript">
          {state.messages.map((message) => (
            <li
              key={`${message.role}-${message.created_at}-${message.content}`}
              className={`chat-message chat-message-${message.role}`}
            >
              <span className="chat-message-role">
                {message.role === "user" ? "You" : "Coach"}
              </span>
              {message.role === "assistant" ? (
                <Markdown components={gameLinkMarkdownComponents}>
                  {message.content}
                </Markdown>
              ) : (
                <p>{message.content}</p>
              )}
            </li>
          ))}

          {state.pendingText !== null && (
            <li className="chat-message chat-message-user">
              <span className="chat-message-role">You</span>
              <p>{state.pendingText}</p>
            </li>
          )}
        </ul>
      )}

      {visibleChatProgress(state).map((line) => (
        <p key={line.id} className="explain-progress">
          {line.text}
        </p>
      ))}

      {streaming && state.streamingText === "" && (
        <p className="explain-progress">thinking…</p>
      )}

      {streaming && state.streamingText !== "" && (
        <article className="advice">
          <Markdown components={gameLinkMarkdownComponents}>
            {state.streamingText}
          </Markdown>
        </article>
      )}

      {state.capReached ? (
        <p role="alert">
          This chat has reached its message limit.{" "}
          <button
            type="button"
            className="btn-low-emphasis"
            onClick={onNewChat}
          >
            Start a new chat
          </button>{" "}
          to keep going.
        </p>
      ) : (
        state.status === "error" &&
        state.error !== null && <p role="alert">{state.error}</p>
      )}

      <div className="chat-input-row">
        <textarea
          className="chat-input"
          value={draft}
          placeholder="Ask a follow-up…"
          disabled={inputDisabled}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <button
          type="button"
          disabled={inputDisabled || draft.trim() === ""}
          onClick={submit}
        >
          {streaming ? "Sending…" : "Send"}
        </button>
      </div>
    </div>
  );
}
