import type { ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";

type Props = {
  /** The player in context; omitted on the home page (no player yet). */
  username?: string;
  children: ReactNode;
};

const navClass = ({ isActive }: { isActive: boolean }): string =>
  isActive ? "app-nav-link active" : "app-nav-link";

/**
 * App shell: a sticky header with the brand, the player-scoped section
 * tabs, and a player switcher, above the routed page content. Every
 * page renders through this — it replaces the per-page inline link nav.
 * When `username` is absent (home) only the brand shows.
 */
export default function Layout({ username, children }: Props) {
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

          {username !== undefined && username !== "" && (
            <>
              <nav className="app-nav" aria-label="Player sections">
                <NavLink to={`/players/${username}/games`} className={navClass}>
                  Games
                </NavLink>
                <NavLink
                  to={`/players/${username}/dashboard`}
                  className={navClass}
                >
                  Dashboard
                </NavLink>
                <NavLink to={`/players/${username}/coach`} className={navClass}>
                  Coach
                </NavLink>
              </nav>
              <div className="app-header-player">
                <span className="app-player-name">{username}</span>
                <Link to="/" className="app-change-player">
                  Change
                </Link>
              </div>
            </>
          )}
        </div>
      </header>

      <main className="page">{children}</main>
    </div>
  );
}
