import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api.ts";
import AddPlayerForm from "../components/AddPlayerForm.tsx";
import AgentSelect from "../components/AgentSelect.tsx";
import Layout from "../components/Layout.tsx";
import { getStoredPlayer } from "../currentPlayer.ts";

export default function Settings() {
  const players = useQuery({ queryKey: ["players"], queryFn: api.players });
  const current = getStoredPlayer();

  return (
    <Layout>
      <h1>Settings</h1>

      <section>
        <h2>Players</h2>
        {players.isPending && <p>Loading…</p>}
        {players.isError && <p role="alert">{players.error.message}</p>}
        {players.isSuccess && players.data.length > 0 && (
          <ul className="player-list">
            {players.data.map((player) => (
              <li key={player.username}>
                <Link to={`/players/${player.username}/dashboard`}>
                  {player.username}
                </Link>{" "}
                <span className="agent-note">
                  {player.games} game{player.games === 1 ? "" : "s"}
                  {player.username === current ? " · current" : ""}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p>Add a chess.com player:</p>
        <AddPlayerForm />
      </section>

      <section>
        <h2>Coach</h2>
        <p>
          The LLM used for coaching advice and move explanations. Calls only
          fire when you ask for them.
        </p>
        <p className="agent-row">
          <AgentSelect />
        </p>
      </section>
    </Layout>
  );
}
