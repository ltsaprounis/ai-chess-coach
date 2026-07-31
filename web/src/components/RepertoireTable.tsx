import { Link } from "react-router-dom";
import { score } from "../api.ts";
import type { OpeningFamily } from "../openings.ts";
import { formatPawns } from "../units.ts";
import { compareValues, useTableSort } from "../useTableSort.ts";
import SortableTh from "./SortableTh.tsx";

type RepSortKey =
  | "family"
  | "games"
  | "analyzed"
  | "winRate"
  | "openingAcpl"
  | "gameAcpl";

// Numeric columns read best high-to-low first.
const REP_DESC: ReadonlySet<RepSortKey> = new Set(["games", "analyzed"]);

function sortValue(family: OpeningFamily, key: RepSortKey): string | number {
  switch (key) {
    case "family":
      return family.family.toLowerCase();
    case "games":
      return family.games;
    case "analyzed":
      return family.analyzedGames;
    case "winRate":
      return score(family);
    case "openingAcpl":
      // No analysis sorts to the bottom of a low-to-high sort.
      return family.openingAcpl ?? Number.POSITIVE_INFINITY;
    case "gameAcpl":
      return family.avgCpLoss ?? Number.POSITIVE_INFINITY;
  }
}

type Props = {
  /** E.g. "Systems you chose as White" / "What you face as Black". */
  title: string;
  /** This color-and-partition's families, already grouped — chosen by
   *  (color, system), faced by (color, name root) — unfiltered, so the
   *  component can tell "no games this color" from "none meet the
   *  games threshold". */
  families: OpeningFamily[];
  minGames: number;
  familyLink: (family: OpeningFamily) => string;
  /** True for a "what you face" table: the system/line column reads as
   *  "their line (your reply)" and leads with the opponent's line
   *  since `system` there is only the player's most-played *reply*,
   *  not the rollup key. Default false (a "systems you chose" table,
   *  where `system` is the family's own rollup key and leads). */
  faced?: boolean;
};

/**
 * One color-and-partition's repertoire — system played (or, in a
 * faced table, the opponent's line and the player's commonest reply
 * to it), W-L-D and both average-loss figures. The system is its own column
 * and the line is shown as secondary text beneath it
 * (docs/08-frontend.md): an opening name alone cannot show who chose
 * what, but a move sequence can.
 */
export default function RepertoireTable({
  title,
  families,
  minGames,
  familyLink,
  faced = false,
}: Props) {
  const rep = useTableSort<RepSortKey>("winRate", "asc", REP_DESC);

  const filtered = families.filter((family) => family.games >= minGames);
  const sorted = [...filtered].sort((a, b) => {
    const cmp = compareValues(
      sortValue(a, rep.sortKey),
      sortValue(b, rep.sortKey),
    );
    const primary = rep.sortDir === "asc" ? cmp : -cmp;
    return primary !== 0 ? primary : b.games - a.games;
  });

  return (
    <div>
      <h3>{title}</h3>

      {families.length === 0 && (
        <p className="panel-empty">
          No classified games {title.toLowerCase()}.
        </p>
      )}
      {families.length > 0 && sorted.length === 0 && (
        <p className="panel-empty">
          No system has {minGames}+ games — lower the threshold.
        </p>
      )}

      {sorted.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <SortableTh
                  column="family"
                  label="Opening"
                  sortKey={rep.sortKey}
                  sortDir={rep.sortDir}
                  onSort={rep.onSort}
                />
                <th>
                  {faced
                    ? "Their line (your reply)"
                    : "System (line as played)"}
                </th>
                <SortableTh
                  column="games"
                  label="Games"
                  sortKey={rep.sortKey}
                  sortDir={rep.sortDir}
                  onSort={rep.onSort}
                />
                <SortableTh
                  column="analyzed"
                  label="Analyzed"
                  sortKey={rep.sortKey}
                  sortDir={rep.sortDir}
                  onSort={rep.onSort}
                />
                <th>W-L-D</th>
                <SortableTh
                  column="winRate"
                  label="Score"
                  sortKey={rep.sortKey}
                  sortDir={rep.sortDir}
                  onSort={rep.onSort}
                />
                <SortableTh
                  column="openingAcpl"
                  label="Opening avg (pawns)"
                  sortKey={rep.sortKey}
                  sortDir={rep.sortDir}
                  onSort={rep.onSort}
                />
                <SortableTh
                  column="gameAcpl"
                  label="Whole-game avg (pawns)"
                  sortKey={rep.sortKey}
                  sortDir={rep.sortDir}
                  onSort={rep.onSort}
                />
              </tr>
            </thead>
            <tbody>
              {sorted.map((family) => (
                <tr
                  key={
                    family.faced
                      ? `${family.color} faced ${family.family}`
                      : `${family.color} chosen ${family.system}`
                  }
                >
                  <td>
                    <Link
                      to={familyLink(family)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {family.family}
                    </Link>
                  </td>
                  <td>
                    {faced ? (
                      <>
                        <div>{family.firstMoves}</div>
                        <div className="rep-line">{family.system}</div>
                      </>
                    ) : (
                      <>
                        <div>{family.system}</div>
                        <div className="rep-line">{family.firstMoves}</div>
                      </>
                    )}
                  </td>
                  <td>{family.games}</td>
                  <td>{family.analyzedGames}</td>
                  <td>
                    {family.wins}-{family.losses}-{family.draws}
                  </td>
                  <td>{Math.round(score(family) * 100)}%</td>
                  <td>
                    {family.openingAcpl === null
                      ? "—"
                      : formatPawns(family.openingAcpl)}
                  </td>
                  <td>
                    {family.avgCpLoss === null
                      ? "—"
                      : formatPawns(family.avgCpLoss)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
