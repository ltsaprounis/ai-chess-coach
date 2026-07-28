// Classic numbered-pager logic ("1 … 4 5 6 … 12") for the Dashboard
// highlight tables (docs/08-frontend.md): first and last page always
// reachable, the current page's neighbours shown, skipped runs
// collapsed to an ellipsis. Pure and unit-tested in
// pagination.test.ts; the markup lives in components/Pagination.tsx.

/** A page number to link, or a run of skipped pages ("gap"). */
export type PageItem = number | "gap";

/** Every page renders as its own button up to this many; beyond it
 *  the ellipsis form takes over. Seven keeps the pager narrower than
 *  the tables it sits under at its widest (1 … n-1 n n+1 … N is
 *  seven items too). */
const FULL_RUN_MAX = 7;

/** Total pages for `total` rows at `pageSize` per page — at least 1,
 *  so "page 1 of 1" stays well-defined for an empty list. */
export function pageCount(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(total / pageSize));
}

/** `page` forced into [1, pages]: a remembered page can outlive the
 *  list it paged (a filter change shrinks the blunder list). */
export function clampPage(page: number, pages: number): number {
  return Math.min(Math.max(page, 1), pages);
}

/** The pager items for `page` of `pages`; short runs render in full. */
export function paginationItems(page: number, pages: number): PageItem[] {
  if (pages <= FULL_RUN_MAX) {
    return Array.from({ length: pages }, (_, index) => index + 1);
  }
  const shown = [...new Set([1, page - 1, page, page + 1, pages])]
    .filter((candidate) => candidate >= 1 && candidate <= pages)
    .sort((a, b) => a - b);
  const items: PageItem[] = [];
  let previous = 0;
  for (const candidate of shown) {
    if (previous !== 0 && candidate - previous > 1) {
      items.push("gap");
    }
    items.push(candidate);
    previous = candidate;
  }
  return items;
}
