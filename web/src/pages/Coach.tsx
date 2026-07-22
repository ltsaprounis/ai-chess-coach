import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import Markdown from "react-markdown";
import { Link, useParams } from "react-router-dom";
import { api } from "../api.ts";

export default function Coach() {
  const { username = "" } = useParams();
  const [copied, setCopied] = useState(false);

  const coach = useMutation({ mutationFn: () => api.coach(username) });

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
        Builds a report from the analyzed games and asks Claude for prioritized
        training advice (via your local Claude Code login).
      </p>

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
