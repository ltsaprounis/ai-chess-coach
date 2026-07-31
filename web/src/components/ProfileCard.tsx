import type { ReactNode } from "react";
import Markdown from "react-markdown";
import { Link } from "react-router-dom";
import type { Color, PlayerProfile, ProfileNarrative } from "../api.ts";
import {
  blunderShare,
  comparisonVerdict,
  deltaOver,
  errorExampleHref,
  errorExampleLabel,
  formatGameDate,
  hasPartialCoverage,
  isImproving,
  isProfileStale,
  measurableComparisons,
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
            {/* Score alone cannot tell a system won on even positions
                from one survived out of the book — 48% at 0.32 pawns a
                move is a different problem from 48% at 0.21. */}
            <th>Opening loss</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.faced ? "faced" : "chosen"}-${row.name}`}>
              <td>{row.name}</td>
              <td>{row.moves}</td>
              <td>{Math.round(row.score * 100)}%</td>
              <td>
                {row.opening_acpl === null || row.opening_acpl === undefined
                  ? "—"
                  : `${formatPawns(row.opening_acpl)} pawns`}
              </td>
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
    // Gap first: the gap is the achievement. chess.com pairs by rating,
    // so "beat a 1559" only says the student was once rated about that
    // themselves (docs/06-coach.md, "Trajectory").
    rows.push({
      label: "Biggest upset",
      value: (
        <>
          <Link to={`/games/${win.game_id}`}>
            beat {win.opponent} ({win.opponent_rating})
          </Link>
          , {win.opponent_rating - win.player_rating} points above them, on{" "}
          {formatGameDate(win.end_time)} while rated {win.player_rating}
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
  }

  // The after-a-loss and by-color rows are deliberately absent: the
  // Splits table below states both against a *matched* baseline and
  // with a verdict. Showing the raw gap here too would put the number
  // above the judgement of it (docs/06-coach.md, "Reading a
  // comparison") — which is how "48% after a loss" became a finding.

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
  const trajectory = profile.trajectory ?? null;
  const improving = isImproving(trajectory);
  const splits = measurableComparisons(profile.comparisons);

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
            {/* The peak gap only reads as a shortfall for a student
                who is not improving. "95 below peak" beside a
                trajectory saying "+443 over the year" is the misread
                the whole rework exists to stop (docs/06-coach.md,
                "Trajectory"). */}
            <div className="tile-label">
              peak {peakLabel(entry)}
              {peakGap(entry) < 0 && !improving ? ` (${peakGap(entry)})` : ""}
            </div>
          </div>
        ))}
      </div>

      {/* Direction, over the whole archive rather than the level
          window every other figure here covers — a coach's first
          question, and the one thing a level-scoped window cannot
          answer (docs/06-coach.md, "Trajectory"). */}
      {trajectory !== null && (
        <>
          <h3>Trajectory</h3>
          <p className="agent-note">
            Over all {trajectory.games} games in this time control, not the
            window above.
          </p>
          <p>
            {[365, 180, 90, 30]
              .map((days) => {
                const d = deltaOver(trajectory, days);
                return d === null
                  ? null
                  : `${d.delta >= 0 ? "+" : ""}${d.delta} over ${days} days`;
              })
              .filter((part) => part !== null)
              .join(" · ")}
          </p>
          {trajectory.drawdown !== null &&
            trajectory.drawdown !== undefined && (
              <p className="agent-note">
                Largest setback:{" "}
                {trajectory.drawdown.trough - trajectory.drawdown.peak} points,{" "}
                {trajectory.drawdown.peak} on{" "}
                {formatGameDate(trajectory.drawdown.peak_at)} down to{" "}
                {trajectory.drawdown.trough} on{" "}
                {formatGameDate(trajectory.drawdown.trough_at)} ·{" "}
                {scorePercent(trajectory.drawdown.record) ?? "—"} through the
                fall ·{" "}
                {trajectory.drawdown.recovered
                  ? "recovered since"
                  : `${scorePercent(trajectory.drawdown.since_record) ?? "—"} since`}
              </p>
            )}
        </>
      )}

      <p>
        Overall quality: {formatPawns(profile.overall_acpl)} pawns average loss
        per move · {Math.round(blunderShare(profile) * 100)}% of moves are
        blunders.
      </p>

      {/* The window holds the student's level roughly constant. When it
          could not, say so rather than leaving a reader to infer it. */}
      {profile.window_spans_level_change && (
        <p className="agent-note" role="note">
          These figures span a change in your level — there were too few games
          at your current one to describe it alone, so the rates below average
          across more than one player.
        </p>
      )}

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

      {/* Two groups of the student's own games, compared. The verdict
          is shown and the arithmetic is not: a sigma the reader cannot
          calibrate invites exactly the false confidence this guard
          removes (docs/06-coach.md, "Reading a comparison"). */}
      {splits.length > 0 && (
        <>
          <h3>Splits</h3>
          <p className="agent-note">
            A split “within noise” is a difference this many games cannot tell
            apart from chance — not a tendency.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Split</th>
                  <th>This group</th>
                  <th>The rest</th>
                  <th>Verdict</th>
                </tr>
              </thead>
              <tbody>
                {splits.map((split) => (
                  <tr key={split.label}>
                    <td>{split.label}</td>
                    <td>
                      {scorePercent(split.left) ?? "—"} ({split.left.games}){" "}
                      {split.left_label}
                    </td>
                    <td>
                      {scorePercent(split.right) ?? "—"} ({split.right.games}){" "}
                      {split.right_label}
                    </td>
                    <td>{comparisonVerdict(split)}</td>
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
