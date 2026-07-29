import { type ReactNode, useEffect } from "react";
import { Link, NavLink } from "react-router-dom";
import { getStoredPlayer, setStoredPlayer } from "../currentPlayer.ts";

type Props = {
  /** The player in context; omitted on "/" and the Settings page, where
   *  the tabs fall back to the last-viewed player. */
  username?: string;
  children: ReactNode;
};

const navClass = ({ isActive }: { isActive: boolean }): string =>
  isActive ? "app-nav-link active" : "app-nav-link";

/**
 * App shell: a sticky header with the brand, the always-available
 * section tabs (pointing at the current player, remembered in
 * localStorage), and a Settings link — where players are switched.
 */
export default function Layout({ username, children }: Props) {
  // Remember the player we're viewing so the tabs and "/" default to
  // them across pages that don't carry a username (Settings).
  useEffect(() => {
    if (username) {
      setStoredPlayer(username);
    }
  }, [username]);

  const currentPlayer = username ?? getStoredPlayer() ?? "";

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
                to={`/players/${currentPlayer}/openings`}
                className={navClass}
              >
                Openings
              </NavLink>
              <NavLink
                to={`/players/${currentPlayer}/coach`}
                className={navClass}
              >
                Coach
              </NavLink>
            </nav>
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
