"""Repertoire move tree (docs/archive/openings-explorer.md,
migrating into docs/05-openings.md alongside this code).

A node is a move path from the start position: two games share a node
iff they played the same moves so far (no transposition merging of
paths). Book knowledge stays position-keyed (EPD), exactly like
`classify` — a node's name is the deepest book entry on its path, and
`in_book`/`book_moves` are looked up by the node's own EPD, so a game
that transposes into book is still named even though its path is its
own.

Two passes, per the performance contract:

1. Counting — walk each game's `san_moves` as plain strings into a
   trie (`_CountNode`), accumulating record/analyzed/eval/loss sums
   and raw (pre-pruning) child edges. No `chess.Board`.
2. Annotation — prune to `min_games`, then DFS the surviving trie with
   one `chess.Board` (push/pop per node) to compute the EPD-derived
   fields: name, `in_book`, `book_moves`, `exits`. The chess work
   happens only at surviving nodes (and the one-ply probe into each of
   their raw, possibly-pruned, children needed for `exits`).
"""

from __future__ import annotations

import chess
from pydantic import BaseModel

from chess_coach.domain import MATE_SCORE, Color, MoveEval, Record, RepertoireGame
from chess_coach.openings.book import BookMove, OpeningBook

_EVAL_CLAMP = 1000  # +9 and mate-in-3 both mean "winning" for averaging


class RepertoireNode(BaseModel):
    san: str  # move that reached this node; "" for the root
    ply: int  # 0 for the root
    record: Record  # every game through this node, analyzed or not
    analyzed: int  # of those, games with a stored analysis
    eco: str | None  # deepest book entry on the path so far
    name: str | None
    in_book: bool  # EPD lies on a book line (position test)
    avg_eval_cp: float | None  # after this move; player POV; analyzed only
    avg_cp_loss: float | None  # cost of the arriving move (mover POV)
    exits: int  # games whose next move left book here
    book_moves: list[BookMove]  # continuations from this position
    children: list[RepertoireNode]


class _CountNode:
    """Pass-1 accumulator: one node of the string trie.

    A plain class, not a dataclass: `raw_children` self-references
    `_CountNode`, and a dataclass's `field(default_factory=dict)` loses
    the element type under pyright strict for a still-being-defined
    class. An explicit `__init__` sidesteps that with no loss of
    typing — this is an internal accumulator, not public surface.
    """

    def __init__(self) -> None:
        self.games = 0
        self.wins = 0
        self.losses = 0
        self.draws = 0
        self.analyzed = 0
        self.eval_sum = 0.0
        self.eval_n = 0
        self.loss_sum = 0.0
        self.loss_n = 0
        self.raw_children: dict[str, _CountNode] = {}


def build_repertoire(
    book: OpeningBook,
    games: list[RepertoireGame],
    *,
    color: Color,
    min_games: int = 2,
    max_plies: int = 30,
) -> RepertoireNode:
    """The per-color repertoire tree for `games` (pure; data in, tree out)."""
    filtered = [g for g in games if g.color == color]
    root_count = _count(filtered, color, max_plies)
    board = chess.Board()
    return _annotate(root_count, "", 0, board, None, None, book, min_games)


def _count(games: list[RepertoireGame], color: Color, max_plies: int) -> _CountNode:
    root = _CountNode()
    for game in games:
        _accumulate(root, game)
        node = root
        limit = min(len(game.san_moves), max_plies)
        for ply in range(1, limit + 1):
            san = game.san_moves[ply - 1]
            child = node.raw_children.setdefault(san, _CountNode())
            _accumulate(child, game)
            if game.evals is not None and len(game.evals) >= ply:
                mv = game.evals[ply - 1]
                child.eval_sum += _signed_clamped_eval(mv, color)
                child.eval_n += 1
                child.loss_sum += mv.cp_loss
                child.loss_n += 1
            node = child
    return root


def _accumulate(node: _CountNode, game: RepertoireGame) -> None:
    node.games += 1
    if game.result == "win":
        node.wins += 1
    elif game.result == "loss":
        node.losses += 1
    else:
        node.draws += 1
    if game.evals is not None:
        node.analyzed += 1


def _signed_clamped_eval(mv: MoveEval, color: Color) -> float:
    """White-POV eval after the arriving move, player-POV, clamped.

    Mate folds to ±MATE_SCORE (sign of `eval_mate`) before the clamp;
    the clamp applies before the color sign-flip, but the bound is
    symmetric so the order does not change the result.
    """
    if mv.eval_cp is not None:
        raw = float(mv.eval_cp)
    else:
        assert mv.eval_mate is not None, "MoveEval always carries cp or mate"
        raw = float(MATE_SCORE) if mv.eval_mate > 0 else float(-MATE_SCORE)
    clamped = max(-float(_EVAL_CLAMP), min(float(_EVAL_CLAMP), raw))
    return -clamped if color == "black" else clamped


def _annotate(
    count_node: _CountNode,
    san: str,
    ply: int,
    board: chess.Board,
    parent_eco: str | None,
    parent_name: str | None,
    book: OpeningBook,
    min_games: int,
) -> RepertoireNode:
    epd = board.epd()
    # Same-component internal helper (deliberately not part of the public
    # is_book/continuations surface); pyright flags cross-module access to
    # a leading-underscore attribute regardless of package, hence the ignore.
    entry = book._entry_at(epd)  # pyright: ignore[reportPrivateUsage]
    eco, name = entry if entry is not None else (parent_eco, parent_name)
    in_book = book.is_book(epd)
    book_moves = [
        bm.model_copy(update={"played": bm.san in count_node.raw_children})
        for bm in book.continuations(epd)
    ]
    children, exits_ = _process_children(
        count_node, board, ply, eco, name, in_book, book, min_games
    )
    return RepertoireNode(
        san=san,
        ply=ply,
        record=Record(
            games=count_node.games,
            wins=count_node.wins,
            losses=count_node.losses,
            draws=count_node.draws,
        ),
        analyzed=count_node.analyzed,
        eco=eco,
        name=name,
        in_book=in_book,
        avg_eval_cp=count_node.eval_sum / count_node.eval_n
        if count_node.eval_n
        else None,
        avg_cp_loss=count_node.loss_sum / count_node.loss_n
        if count_node.loss_n
        else None,
        exits=exits_,
        book_moves=book_moves,
        children=children,
    )


def _process_children(
    count_node: _CountNode,
    board: chess.Board,
    ply: int,
    node_eco: str | None,
    node_name: str | None,
    node_in_book: bool,
    book: OpeningBook,
    min_games: int,
) -> tuple[list[RepertoireNode], int]:
    """Recurse into surviving children; tally `exits` over every raw edge.

    One push/pop per raw child edge, regardless of whether it survives
    pruning: `exits` must see all of them, but only surviving edges get
    a full `_annotate` (and thus recurse further). A SAN that does not
    even parse here (a node reached only through an earlier malformed
    move) ends that branch: it counts toward `exits` (it plainly is not
    a book continuation) and is otherwise dropped, never raising.
    """
    survivors: list[RepertoireNode] = []
    exits_ = 0
    for child_san, child_count in count_node.raw_children.items():
        try:
            board.push_san(child_san)
        except ValueError:
            if node_in_book:
                exits_ += child_count.games
            continue
        try:
            if node_in_book and not book.is_book(board.epd()):
                exits_ += child_count.games
            if child_count.games >= min_games:
                survivors.append(
                    _annotate(
                        child_count,
                        child_san,
                        ply + 1,
                        board,
                        node_eco,
                        node_name,
                        book,
                        min_games,
                    )
                )
        finally:
            board.pop()
    survivors.sort(key=lambda n: (-n.record.games, n.san))
    return survivors, exits_
