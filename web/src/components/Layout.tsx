import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useEffect } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api.ts";
import { getStoredPlayer, setStoredPlayer } from "../currentPlayer.ts";

type Props = {
  /** The player in context; omitted on "/" and the Settings page, where
   *  the header falls back to the last-viewed player. */
  username?: string;
  children: ReactNode;
};

type Section = "games" | "dashboard" | "coach";

/** The player section we're on, so switching players keeps the section. */
function sectionFromPath(pathname: string): Section {
  const match = pathname.match(/\/players\/[^/]+\/(games|dashboard|coach)/);
  return (match?.[1] as Section | undefined) ?? "dashboard";
}

const navClass = ({ isActive }: { isActive: boolean }): string =>
  isActive ? "app-nav-link active" : "app-nav-link";

/**
 * App shell: a sticky header with the brand, a saved-players switcher,
 * the always-available section tabs (pointing at the current player),
 * and a Settings link. Every page renders through this.
 */
export default function Layout({ username, children }: Props) {
  const location = useLocation();
  const navigate = useNavigate();

  // Remember the player we're viewing so the switcher and "/" default
  // to them across pages that don't carry a username (Settings).
  useEffect(() => {
    if (username) {
      setStoredPlayer(username);
    }
  }, [username]);

  const currentPlayer = username ?? getStoredPlayer() ?? "";
  const section = sectionFromPath(location.pathname);
  const players = useQuery({ queryKey: ["players"], queryFn: api.players });
  const saved = players.data ?? [];
  const knownCurrent = saved.some((p) => p.username === currentPlayer);

  const switchPlayer = (value: string): void => {
    setStoredPlayer(value);
    navigate(`/players/${value}/${section}`);
  };

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-inner">
          <Link to="/" className="brand">
            <span className="brand-mark" aria-hidden="true">
              ♞
            </span>
            <span className="brand-name">AI Chess Coach</span>
          </Link>

          {currentPlayer !== "" && (
            <>
              {saved.length > 0 && (
                <select
                  className="player-select"
                  aria-label="player"
                  value={currentPlayer}
                  onChange={(event) => switchPlayer(event.target.value)}
                >
                  {!knownCurrent && (
                    <option value={currentPlayer}>{currentPlayer}</option>
                  )}
                  {saved.map((player) => (
                    <option key={player.username} value={player.username}>
                      {player.username} ({player.games})
                    </option>
                  ))}
                </select>
              )}
              <nav className="app-nav" aria-label="Player sections">
                <NavLink
                  to={`/players/${currentPlayer}/games`}
                  className={navClass}
                >
                  Games
                </NavLink>
                <NavLink
                  to={`/players/${currentPlayer}/dashboard`}
                  className={navClass}
                >
                  Dashboard
                </NavLink>
                <NavLink
                  to={`/players/${currentPlayer}/coach`}
                  className={navClass}
                >
                  Coach
                </NavLink>
              </nav>
            </>
          )}

          <NavLink to="/settings" className="app-settings-link">
            ⚙ Settings
          </NavLink>
        </div>
      </header>

      <main className="page">{children}</main>
    </div>
  );
}
