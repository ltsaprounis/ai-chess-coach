import { Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard.tsx";
import Games from "./pages/Games.tsx";
import Home from "./pages/Home.tsx";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/players/:username/games" element={<Games />} />
      <Route path="/players/:username/dashboard" element={<Dashboard />} />
    </Routes>
  );
}
