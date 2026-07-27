import {
  ALL_CLASSES,
  type StatsFiltersState,
  WINDOWS,
} from "../useStatsFilters.ts";

type Props = Pick<
  StatsFiltersState,
  | "windowDays"
  | "setWindowDays"
  | "timeClass"
  | "setPickedClass"
  | "classOptions"
>;

/**
 * Time-window + time-control selects shared by the Dashboard and Coach
 * pages — the state lives in `useStatsFilters`, this is just the UI.
 */
export default function StatsFilters({
  windowDays,
  setWindowDays,
  timeClass,
  setPickedClass,
  classOptions,
}: Props) {
  return (
    <div className="filters">
      <label>
        Time window{" "}
        <select
          aria-label="time window"
          value={windowDays ?? ""}
          onChange={(event) =>
            setWindowDays(
              event.target.value === "" ? null : Number(event.target.value),
            )
          }
        >
          {WINDOWS.map((window) => (
            <option key={window.label} value={window.days ?? ""}>
              {window.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Time control{" "}
        <select
          aria-label="time control"
          value={timeClass}
          onChange={(event) => setPickedClass(event.target.value)}
        >
          <option value={ALL_CLASSES}>All classes</option>
          {classOptions.map((entry) => (
            <option key={entry.timeClass} value={entry.timeClass}>
              {entry.timeClass} ({entry.games})
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}
