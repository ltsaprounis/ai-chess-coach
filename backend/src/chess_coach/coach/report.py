"""Aggregate analyzed games into a PlayerReport (docs/06-coach.md)."""

import contextlib
from collections import defaultdict

import chess

from chess_coach.domain import (
    AnalyzedGame,
    CriticalPosition,
    Judgment,
    OpeningStats,
    Phase,
    PlayerReport,
)

_JUDGMENTS: tuple[Judgment, ...] = (
    "best",
    "good",
    "inaccuracy",
    "mistake",
    "blunder",
)
_PHASES: tuple[Phase, ...] = ("opening", "middlegame", "endgame")
_TOP_CRITICAL = 5


def build_report(username: str, games: list[AnalyzedGame]) -> PlayerReport:
    """Pure aggregation over analyzed games; worst openings first."""
    judgment_counts: dict[Judgment, int] = {j: 0 for j in _JUDGMENTS}
    for game in games:
        for judgment, count in game.analysis.judgment_counts.items():
            judgment_counts[judgment] += count

    return PlayerReport(
        username=username,
        games_analyzed=len(games),
        overall_acpl=_mean([g.analysis.overall_acpl for g in games]),
        acpl_by_phase={
            phase: _mean([g.analysis.acpl_by_phase.get(phase, 0.0) for g in games])
            for phase in _PHASES
        },
        judgment_counts=judgment_counts,
        openings=_opening_stats(games),
        critical_positions=_critical_positions(games),
    )


def _opening_stats(games: list[AnalyzedGame]) -> list[OpeningStats]:
    grouped: dict[tuple[str, str], list[AnalyzedGame]] = defaultdict(list)
    for game in games:
        if game.opening is not None:
            grouped[(game.opening.eco, game.opening.name)].append(game)

    stats = [
        OpeningStats(
            eco=eco,
            name=name,
            games=len(members),
            wins=sum(g.result == "win" for g in members),
            losses=sum(g.result == "loss" for g in members),
            draws=sum(g.result == "draw" for g in members),
            analyzed_games=len(members),  # report is built from analyzed games
            avg_cp_loss=_mean([g.analysis.overall_acpl for g in members]),
        )
        for (eco, name), members in grouped.items()
    ]
    stats.sort(key=lambda s: (_score(s), -s.games, s.eco))  # worst first
    return stats


def _critical_positions(games: list[AnalyzedGame]) -> list[CriticalPosition]:
    """The player's costliest moves across all games, biggest loss first."""
    candidates: list[tuple[int, AnalyzedGame, int]] = []
    for game in games:
        player_is_white = game.color == "white"
        for index, move_eval in enumerate(game.analysis.evals):
            mover_is_white = index % 2 == 0
            if mover_is_white == player_is_white and move_eval.cp_loss > 0:
                candidates.append((move_eval.cp_loss, game, index))
    candidates.sort(key=lambda item: (-item[0], item[1].id, item[2]))
    return [_critical(game, index) for _, game, index in candidates[:_TOP_CRITICAL]]


def _critical(game: AnalyzedGame, index: int) -> CriticalPosition:
    board = chess.Board()
    for san in game.san_moves[:index]:
        board.push_san(san)
    move_eval = game.analysis.evals[index]
    best = move_eval.best_move
    # Keep the raw UCI if it does not parse in this position.
    with contextlib.suppress(ValueError):
        best = board.san(chess.Move.from_uci(move_eval.best_move))
    return CriticalPosition(
        fen=board.fen(),
        played=move_eval.san,
        best=best,
        cp_loss=move_eval.cp_loss,
        game_id=game.id,
    )


def _score(stats: OpeningStats) -> float:
    return (stats.wins + stats.draws / 2) / stats.games if stats.games else 0.0


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0
