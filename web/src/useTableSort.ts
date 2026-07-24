import { useState } from "react";

export type SortDir = "asc" | "desc";

/**
 * Click-to-sort state shared by the Games and repertoire tables:
 * clicking the active column flips direction, a new column starts in
 * its natural direction (`descFirst` lists the columns — numbers and
 * dates — that read best high-to-low).
 */
export function useTableSort<K extends string>(
  defaultKey: K,
  defaultDir: SortDir,
  descFirst: ReadonlySet<K> = new Set<K>(),
) {
  const [sortKey, setSortKey] = useState<K>(defaultKey);
  const [sortDir, setSortDir] = useState<SortDir>(defaultDir);

  const onSort = (key: K): void => {
    if (key === sortKey) {
      setSortDir((dir) => (dir === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(descFirst.has(key) ? "desc" : "asc");
    }
  };

  return { sortKey, sortDir, onSort };
}

/** Compare two sort values of the same kind (numeric or textual). */
export function compareValues(a: string | number, b: string | number): number {
  return typeof a === "number" && typeof b === "number"
    ? a - b
    : String(a).localeCompare(String(b));
}
