import { Route, Routes } from "react-router-dom";
import Coach from "./pages/Coach.tsx";
import Dashboard from "./pages/Dashboard.tsx";
import Game from "./pages/Game.tsx";
import Games from "./pages/Games.tsx";
import Home from "./pages/Home.tsx";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/players/:username/games" element={<Games />} />
      <Route path="/players/:username/dashboard" element={<Dashboard />} />
      <Route path="/players/:username/coach" element={<Coach />} />
      <Route path="/games/:id" element={<Game />} />
    </Routes>
  );
}
