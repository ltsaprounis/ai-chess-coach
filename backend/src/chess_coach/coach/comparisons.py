"""Matched bucket comparisons with false-discovery control
(docs/06-coach.md, "Reading a comparison").

Several profile figures are differences between two disjoint buckets of
the same games -- after a loss against not, White against Black, one
opening family against the rest. They are noisy, a profile makes up to
fourteen of them, and a prompt that hands a model a difference and
calls it a coaching problem gets a coin flip narrated as a tendency.

This module is the guard. Records in, gaps and verdicts out; the
verdicts are decided over the whole family at once, never per row,
because that is the entire point.
"""

import math

from chess_coach.coach.level import score_of, score_variance
from chess_coach.domain import Comparison, ComparisonInput

# The Benjamini-Hochberg level: the share of *reported* tendencies we
# accept being spurious. FDR rather than family-wise error because the
# costs here are lopsided but not catastrophic -- a missed tendency
# costs one bullet, a fabricated one is pasted into every later prompt.
# Bonferroni over fourteen comparisons would demand a roughly 9-point
# colour split before saying anything, which reads as a profile that
# has nothing to say rather than one being careful.
COMPARISON_FDR = 0.05

# The resolution both renderers state is +/- 2 standard errors, i.e. the
# ordinary 95% interval, quoted as a precision rather than as a test:
# "this resolves to about +/-6 points" is a statement a coach can act on
# where "1.58 sigma" is not.
_RESOLUTION_SIGMAS = 2.0


def build_comparisons(pairs: list[ComparisonInput]) -> list[Comparison]:
    """Gaps, resolutions and BH-adjusted verdicts, in the order given.

    A pair whose buckets are too thin to have a variance keeps its gap
    (the arithmetic is still true) and is never significant -- it takes
    no slot in the BH family either, since a comparison that cannot be
    made is not a comparison that was tried.
    """
    rows = [_measure(pair) for pair in pairs]
    testable = [(i, p) for i, (_, p) in enumerate(rows) if p is not None]
    survivors = _benjamini_hochberg([p for _, p in testable])
    for rank, (index, _) in enumerate(testable):
        rows[index][0].significant = survivors[rank]
    return [row for row, _ in rows]


def _measure(pair: ComparisonInput) -> tuple[Comparison, float | None]:
    """One row plus its two-sided p-value, or None when either bucket is
    too thin to carry a variance."""
    left_score = score_of(pair.left)
    right_score = score_of(pair.right)
    left_var = score_variance(pair.left)
    right_var = score_variance(pair.right)

    gap = 0.0
    if left_score is not None and right_score is not None:
        gap = (left_score - right_score) * 100

    se = None
    if left_var is not None and right_var is not None:
        # Welch: the buckets have different sizes and, on a real
        # archive, different draw rates, so pooling their variances
        # would understate the error on whichever is smaller.
        variance = left_var / pair.left.games + right_var / pair.right.games
        se = math.sqrt(variance) * 100

    row = Comparison(
        label=pair.label,
        left_label=pair.left_label,
        left=pair.left,
        right_label=pair.right_label,
        right=pair.right,
        gap=round(gap, 1),
        resolution=round(_RESOLUTION_SIGMAS * se, 1) if se else 0.0,
        significant=False,
    )
    if se is None or se == 0.0:
        return row, None
    return row, _two_sided_p(abs(gap) / se)


def _two_sided_p(z: float) -> float:
    """P(|Z| > z) for a standard normal, via the error function.

    A normal approximation is what the sample sizes here support: the
    buckets run to hundreds of games, and the score's own variance is
    already measured rather than assumed (see `score_variance`).
    """
    return math.erfc(z / math.sqrt(2))


def _benjamini_hochberg(pvalues: list[float]) -> list[bool]:
    """Step-up procedure at `COMPARISON_FDR`, returned in input order.

    Sort ascending, find the largest k whose p-value is at most
    `k/m * FDR`, and reject everything up to it -- including any row
    whose own p-value failed that bound, which is what makes this a
    step-*up* and not a per-row threshold.
    """
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    cutoff = -1
    for rank, index in enumerate(order, start=1):
        if pvalues[index] <= rank / m * COMPARISON_FDR:
            cutoff = rank
    out = [False] * m
    for rank, index in enumerate(order, start=1):
        if rank <= cutoff:
            out[index] = True
    return out
