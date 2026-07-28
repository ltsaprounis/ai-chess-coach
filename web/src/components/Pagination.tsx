import { clampPage, paginationItems } from "../pagination.ts";

type Props = {
  /** 1-based current page; out-of-range values are clamped. */
  page: number;
  pages: number;
  onPage: (page: number) => void;
  /** Accessible name telling this pager apart from its sibling
   *  (e.g. "blunder pages"). */
  label: string;
};

/**
 * Classic numbered pager — ‹ 1 … 4 5 6 … 12 › — for the highlight
 * tables. Renders nothing when one page fits, the usual case for
 * brilliancies.
 */
export default function Pagination({ page, pages, onPage, label }: Props) {
  if (pages <= 1) {
    return null;
  }
  const current = clampPage(page, pages);
  const items = paginationItems(current, pages);
  return (
    <nav className="pagination" aria-label={label}>
      <button
        type="button"
        aria-label="previous page"
        disabled={current === 1}
        onClick={() => onPage(current - 1)}
      >
        ‹
      </button>
      {items.map((item, index) =>
        item === "gap" ? (
          // The first item is always page 1, so a gap always has a
          // number before it to key on.
          <span
            key={`gap-${items[index - 1]}`}
            className="pagination-gap"
            aria-hidden="true"
          >
            …
          </span>
        ) : (
          <button
            key={item}
            type="button"
            aria-current={item === current ? "page" : undefined}
            onClick={() => onPage(item)}
          >
            {item}
          </button>
        ),
      )}
      <button
        type="button"
        aria-label="next page"
        disabled={current === pages}
        onClick={() => onPage(current + 1)}
      >
        ›
      </button>
    </nav>
  );
}
