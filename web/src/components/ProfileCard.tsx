import Markdown from "react-markdown";
import { Link } from "react-router-dom";
import type { Color, PlayerProfile, ProfileNarrative } from "../api.ts";
import {
  blunderShare,
  errorExampleHref,
  errorExampleLabel,
  formatPawns,
  isProfileStale,
  openingsFor,
} from "../playerProfile.ts";

type Props = {
  profile: PlayerProfile;
  /** The stored narrative's metadata, or `null` when nothing has been
   *  generated yet. */
  narrative: ProfileNarrative | null;
  /** Resolves an agent id to its roster label — the same helper the
   *  Coach page already applies to advice (falls back to the raw id
   *  when the roster hasn't loaded or no longer lists it). */
  agentLabel: (id: string) => string;
  /** True while a (re)generate POST is in flight. */
  generating: boolean;
  /** The last generate attempt's error message, or `null`. */
  generateError: string | null;
  onGenerate: () => void;
};

const COLORS: readonly Color[] = ["white", "black"];
const COLOR_LABEL: Record<Color, string> = {
  white: "As White",
  black: "As Black",
};

/** One (color, chosen|faced) partition's rows — a plain table, no
 *  sorting or paging: the list is already small and ranked by the
 *  backend (docs/06-coach.md, "Player profile"). */
function OpeningRows({
  rows,
  movesHeader,
  emptyLabel,
}: {
  rows: PlayerProfile["openings"];
  movesHeader: string;
  emptyLabel: string;
}) {
  if (rows.length === 0) {
    return <p className="panel-empty">{emptyLabel}</p>;
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Opening</th>
            <th>{movesHeader}</th>
            <th>Score</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.faced ? "faced" : "chosen"}-${row.name}`}>
              <td>{row.name}</td>
              <td>{row.moves}</td>
              <td>{Math.round(row.score * 100)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The Coach page's player-profile card (docs/08-frontend.md): the
 * always-free facts distilled by `build_profile` plus, once
 * generated, the stored LLM narrative — a modest first surface per
 * docs/future-improvements/player-profile.md. Purely presentational,
 * mirroring how `ExplainPanel`/`ChatPanel` sit apart from their data
 * hooks: the Coach page owns the `GET`/`POST /profile` queries and
 * only hands this component the resolved data plus the generate
 * callback.
 */
export default function ProfileCard({
  profile,
  narrative,
  agentLabel,
  generating,
  generateError,
  onGenerate,
}: Props) {
  if (profile.games_covered === 0) {
    return (
      <section className="panel profile-card">
        <h2>Player profile</h2>
        <p className="panel-empty">
          No analyzed games yet —{" "}
          <Link to={`/players/${profile.username}/games`}>
            analyze some games
          </Link>{" "}
          to build a profile.
        </p>
      </section>
    );
  }

  const narrativeText = profile.narrative ?? null;
  const stale = isProfileStale(profile, narrative);

  return (
    <section className="panel profile-card">
      <h2>Player profile</h2>

      <div className="tiles">
        {profile.time_classes.map((entry) => (
          <div className="tile" key={entry.time_class}>
            <div className="tile-value">{entry.rating_end}</div>
            <div className="tile-label">
              {entry.time_class} rating ({entry.record.games} game
              {entry.record.games === 1 ? "" : "s"})
            </div>
          </div>
        ))}
      </div>

      <p>
        Overall quality: {formatPawns(profile.overall_acpl)} pawns average loss
        per move · {Math.round(blunderShare(profile) * 100)}% of moves are
        blunders ({profile.games_covered} analyzed game
        {profile.games_covered === 1 ? "" : "s"}).
      </p>

      <div className="chart-row">
        {COLORS.map((color) => (
          <div key={color}>
            <h3>{COLOR_LABEL[color]}</h3>
            <p className="agent-note">Systems you chose</p>
            <OpeningRows
              rows={openingsFor(profile.openings, color, false)}
              movesHeader="System"
              emptyLabel="No chosen systems with enough games yet."
            />
            <p className="agent-note">Problem lines you face</p>
            <OpeningRows
              rows={openingsFor(profile.openings, color, true)}
              movesHeader="Line faced"
              emptyLabel="No recurring problem lines yet."
            />
          </div>
        ))}
      </div>

      <h3>Recurring mistakes</h3>
      {profile.error_patterns.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Pattern</th>
                <th>Count</th>
                <th>% of blunders</th>
                <th>Example</th>
              </tr>
            </thead>
            <tbody>
              {profile.error_patterns.map((pattern) => {
                const href = errorExampleHref(pattern);
                return (
                  <tr key={pattern.pattern}>
                    <td>{pattern.label}</td>
                    <td>{pattern.count}</td>
                    <td>{Math.round(pattern.share_of_blunders * 100)}%</td>
                    <td>
                      {href !== null ? (
                        <Link to={href} target="_blank" rel="noreferrer">
                          {errorExampleLabel(pattern)}
                        </Link>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="panel-empty">No tagged error patterns yet.</p>
      )}

      <h3>The coach's read on {profile.username}</h3>

      {narrative !== null && narrativeText !== null ? (
        <>
          <p className="agent-note">
            {agentLabel(narrative.agent_id)} ({narrative.agent_id}) — generated{" "}
            {new Date(narrative.generated_at * 1000).toLocaleString()}{" "}
            <button
              type="button"
              className="explain-regenerate"
              disabled={generating}
              onClick={onGenerate}
            >
              {generating ? "Regenerating…" : "Regenerate"}
            </button>
          </p>
          {stale && (
            <p role="alert">
              This profile was generated over {narrative.games_covered} analyzed
              game{narrative.games_covered === 1 ? "" : "s"} — you now have{" "}
              {profile.games_covered}. Regenerate for a profile that covers your
              latest games.
            </p>
          )}
          {generateError !== null && <p role="alert">{generateError}</p>}
          <article className="advice">
            <Markdown>{narrativeText}</Markdown>
          </article>
        </>
      ) : (
        <>
          <p className="panel-empty">
            No narrative yet — generate the coach's read on {profile.username}'s
            tendencies from the facts above.
          </p>
          {generateError !== null && <p role="alert">{generateError}</p>}
          <button
            type="button"
            className="btn-primary"
            disabled={generating}
            onClick={onGenerate}
          >
            {generating ? "Generating…" : "Generate"}
          </button>
        </>
      )}
    </section>
  );
}
