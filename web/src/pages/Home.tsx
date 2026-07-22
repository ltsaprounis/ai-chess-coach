import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api.ts";

export default function Home() {
  const [username, setUsername] = useState("");
  const navigate = useNavigate();
  const sync = useMutation({
    mutationFn: api.sync,
    onSuccess: (_result, user) => navigate(`/players/${user}/games`),
  });

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
      {sync.isError && <p role="alert">{sync.error.message}</p>}
    </main>
  );
}
