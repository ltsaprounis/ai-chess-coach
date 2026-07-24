import { useQuery } from "@tanstack/react-query";
import { Navigate } from "react-router-dom";
import { api } from "../api.ts";
import AddPlayerForm from "../components/AddPlayerForm.tsx";
import Layout from "../components/Layout.tsx";
import { getStoredPlayer } from "../currentPlayer.ts";

// "/" is a redirect to the last-viewed player's dashboard (falling back
// to the most-played stored player); with no players at all it's the
// onboarding to add the first one.
export default function Home() {
  const stored = getStoredPlayer();
  const players = useQuery({ queryKey: ["players"], queryFn: api.players });

  if (stored !== null) {
    return <Navigate to={`/players/${stored}/dashboard`} replace />;
  }
  if (players.isPending) {
    return (
      <Layout>
        <p>Loading…</p>
      </Layout>
    );
  }
  const first = players.data?.[0]?.username;
  if (first !== undefined) {
    return <Navigate to={`/players/${first}/dashboard`} replace />;
  }

  return (
    <Layout>
      <h1>Analyze your chess.com games</h1>
      <p>
        Add a chess.com player to pull their games, run engine analysis, and get
        coaching advice.
      </p>
      <AddPlayerForm />
    </Layout>
  );
}
