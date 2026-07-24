import type { SortDir } from "../useTableSort.ts";

type Props<K extends string> = {
  column: K;
  label: string;
  sortKey: K;
  sortDir: SortDir;
  onSort: (key: K) => void;
};

/** A clickable table header that sorts by `column`, with an arrow and
 *  `aria-sort` reflecting the current sort. */
export default function SortableTh<K extends string>({
  column,
  label,
  sortKey,
  sortDir,
  onSort,
}: Props<K>) {
  const active = sortKey === column;
  return (
    <th
      aria-sort={
        active ? (sortDir === "asc" ? "ascending" : "descending") : "none"
      }
    >
      <button type="button" className="th-sort" onClick={() => onSort(column)}>
        {label}
        <span className="sort-arrow">
          {active ? (sortDir === "asc" ? "▲" : "▼") : ""}
        </span>
      </button>
    </th>
  );
}
