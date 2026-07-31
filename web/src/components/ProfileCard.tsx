import type { ReactNode } from "react";
import Markdown from "react-markdown";
import { Link } from "react-router-dom";
import type { Color, PlayerProfile, ProfileNarrative } from "../api.ts";
import {
  blunderShare,
  errorExampleHref,
  errorExampleLabel,
  formatGameDate,
  hasPartialCoverage,
  isProfileStale,
  openingsFor,
  peakGap,
  peakLabel,
  scopeLabel,
  scorePercent,
  streakLabel,
  terminationShares,
} from "../playerProfile.ts";
import { formatPawns } from "../units.ts";

type Props = {
  profile: PlayerProfile;
  /** The stored narrative's metadata, or `null` when nothing has been
   *  generated yet. */
  narrative: ProfileNarrative | null;
  /** Live analyzed-game count for the *narrative's* scope, which is
   *  wider than the card's whenever a window filter is applied — the
   *  staleness hint's only honest basis. */
  narrativeGamesNow: number | null;
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
// The bare side, for inline prose where "As White 52%" would not read.
const SIDE_LABEL: Record<Color, string> = { white: "White", black: "Black" };

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

/** One labelled milestone row. Rows are built individually and the
 *  empty ones dropped, so a student with no win yet simply has no
 *  "Best win" row rather than a dash where a milestone should be. */
type Milestone = { label: string; value: ReactNode };

/** "weaker 80% (5)" — a split's score with its own denominator, which
 *  every one of these needs: the bands and the colors can rest on
 *  wildly different sample sizes within the same row. */
function scored(label: string, record: PlayerProfile["record"]): string {
  return `${label} ${scorePercent(record) ?? "—"} (${record.games})`;
}

/** The volume-layer milestones (docs/06-coach.md, "Milestones"): what
 *  the student has managed and how they win and lose, none of which
 *  needs engine analysis — so these cover every game in scope, which
 *  the section says out loud rather than leaving to the coverage line
 *  above it. */
function milestoneRows(profile: PlayerProfile): Milestone[] {
  const rows: Milestone[] = [];
  const win = profile.best_win;
  if (win) {
    rows.push({
      label: "Best win",
      value: (
        <>
          <Link to={`/games/${win.game_id}`}>
            beat {win.opponent} ({win.opponent_rating})
          </Link>{" "}
          on {formatGameDate(win.end_time)}, rated {win.player_rating} at the
          time
        </>
      ),
    });
  }

  const streaks = profile.streaks;
  if (streaks) {
    rows.push({
      label: "Current run",
      value: `${streakLabel(streaks)} · longest ${streaks.longest_win} wins, ${
        streaks.longest_loss
      } losses`,
    });
    // Tilt, stated as a comparison — a 39% score is only bad against a
    // better overall one. Omitted when no game in scope was played
    // straight after a loss: that is an absent sample, and 0% would
    // read as a catastrophic one.
    const afterLoss = scorePercent(streaks.after_loss);
    const overall = scorePercent(profile.record);
    if (afterLoss !== null) {
      rows.push({
        label: "After a loss",
        value: `${afterLoss} over ${streaks.after_loss.games} game${
          streaks.after_loss.games === 1 ? "" : "s"
        } played straight after one${overall === null ? "" : ` · ${overall} overall`}`,
      });
    }
  }

  const colorParts = COLORS.map((color) => {
    const record = profile.color_records[color];
    return record === undefined || record.games === 0
      ? null
      : scored(SIDE_LABEL[color], record);
  }).filter((part) => part !== null);
  if (colorParts.length > 0) {
    rows.push({ label: "By color", value: colorParts.join(" · ") });
  }

  const opponents = profile.opponents;
  if (opponents) {
    // Counts, not bare percentages: matchmaking keeps nearly every
    // game inside the "similar" band, so the other two rest on a
    // handful of games and "0% against stronger players" would read as
    // a verdict rather than as the six-game footnote it is.
    rows.push({
      label: "Opposition",
      value: [
        scored("stronger", opponents.vs_stronger),
        scored("similar", opponents.vs_similar),
        scored("weaker", opponents.vs_weaker),
      ].join(" · "),
    });
  }

  const losses = terminationShares(profile.terminations, "loss");
  if (losses.length > 1) {
    rows.push({
      label: "How you lose",
      value: losses
        .map((row) => `${row.termination} ${Math.round(row.share * 100)}%`)
        .join(" · "),
    });
  }
  return rows;
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
  narrativeGamesNow,
  agentLabel,
  generating,
  generateError,
  onGenerate,
}: Props) {
  const scope = scopeLabel(profile);

  if (profile.games_covered === 0) {
    return (
      <section className="panel profile-card">
        <h2>Player profile — {scope}</h2>
        <p className="panel-empty">
          No analyzed {profile.time_class ?? ""} games in this window —{" "}
          <Link to={`/players/${profile.username}/games`}>
            analyze some games
          </Link>{" "}
          or widen the filters above to build a profile.
        </p>
      </section>
    );
  }

  const narrativeText = profile.narrative ?? null;
  const stale = isProfileStale(profile, narrative, narrativeGamesNow);
  const milestones = milestoneRows(profile);

  return (
    <section className="panel profile-card">
      <h2>Player profile — {scope}</h2>

      <div className="tiles">
        {profile.time_classes.map((entry) => (
          <div className="tile" key={entry.time_class}>
            <div className="tile-value">{entry.rating_end}</div>
            <div className="tile-label">
              {entry.time_class} rating ({entry.record.games} game
              {entry.record.games === 1 ? "" : "s"})
            </div>
            {/* The peak with the date it was first reached: how far the
                student has ever been is only coaching signal next to
                when they were there and how far below it they sit now
                (docs/06-coach.md, "Milestones"). */}
            <div className="tile-label">
              peak {peakLabel(entry)}
              {peakGap(entry) < 0 ? ` (${peakGap(entry)})` : ""}
            </div>
          </div>
        ))}
      </div>

      <p>
        Overall quality: {formatPawns(profile.overall_acpl)} pawns average loss
        per move · {Math.round(blunderShare(profile) * 100)}% of moves are
        blunders.
      </p>

      {/* The two denominators, stated (docs/06-coach.md, "Volume and
          quality"). Ratings and records above come from every game;
          the quality figures come from the analyzed ones, and saying
          so is the difference between an honest profile and one that
          presents whatever the engine happened to reach as the whole
          player. */}
      <p className="agent-note">
        {hasPartialCoverage(profile) ? (
          <>
            Ratings, records and repertoire cover all {profile.games_in_scope}{" "}
            games; quality figures cover the {profile.games_covered} analyzed so
            far.
          </>
        ) : (
          <>All {profile.games_in_scope} games in scope are analyzed.</>
        )}
      </p>

      {profile.periods.length > 1 && (
        <>
          <h3>Recent form</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Window</th>
                  <th>Games</th>
                  <th>Score</th>
                  <th>Rating</th>
                  <th>Avg loss (pawns)</th>
                  <th>Blunders</th>
                </tr>
              </thead>
              <tbody>
                {profile.periods.map((period) => (
                  <tr key={period.label}>
                    <td>{period.label}</td>
                    <td>{period.games}</td>
                    <td>{scorePercent(period.record) ?? "—"}</td>
                    <td>{period.rating_end ?? "—"}</td>
                    <td>
                      {period.acpl !== null ? formatPawns(period.acpl) : "—"}
                    </td>
                    <td>
                      {period.blunder_rate !== null
                        ? `${(period.blunder_rate * 100).toFixed(1)}%`
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {milestones.length > 0 && (
        <>
          <h3>Milestones</h3>
          {/* None of these needs an engine, so all of them cover every
              game in scope — worth saying under a card whose quality
              figures cover only the analyzed ones. */}
          <p className="agent-note">
            Over all {profile.games_in_scope} games in scope, analyzed or not.
          </p>
          <div className="table-wrap">
            <table>
              <tbody>
                {milestones.map((row) => (
                  <tr key={row.label}>
                    <th scope="row">{row.label}</th>
                    <td>{row.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

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

      <h3>
        The coach's read on {profile.username} in {scope}
      </h3>

      {/* The narrative covers the time control's whole history, never
          the selected window — so when a window is applied the facts
          above and this text describe different spans, and saying so
          is cheaper than a reader assuming they match. */}
      <p className="agent-note">
        Written for the coach, covering {scope} across all time — the window
        filter above re-scopes the figures, not this.
      </p>

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
              This profile was generated over {narrative.games_covered} analyzed{" "}
              {scope} game{narrative.games_covered === 1 ? "" : "s"} — you now
              have {narrativeGamesNow ?? profile.games_covered}. Regenerate for
              a profile that covers your latest games.
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
            No {scope} narrative yet — generate the coach's read on{" "}
            {profile.username}'s tendencies in {scope}.
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
