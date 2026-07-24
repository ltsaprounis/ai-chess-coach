import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api.ts";
import { setStoredPlayer } from "../currentPlayer.ts";

/** Sync a new chess.com player, then jump to their dashboard. Used by
 *  the "/" onboarding and the Settings page. */
export default function AddPlayerForm() {
  const [username, setUsername] = useState("");
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const sync = useMutation({
    mutationFn: api.sync,
    onSuccess: (_result, user) => {
      setStoredPlayer(user);
      void queryClient.invalidateQueries({ queryKey: ["players"] });
      navigate(`/players/${user}/dashboard`);
    },
  });

  return (
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
      <button type="submit" className="btn-primary" disabled={sync.isPending}>
        {sync.isPending ? "Syncing…" : "Add player"}
      </button>
      {sync.isError && <span role="alert">{sync.error.message}</span>}
    </form>
  );
}
