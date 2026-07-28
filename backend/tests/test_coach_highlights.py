"""Dashboard highlights tests (docs/06-coach.md, "Highlights").

`build_highlights` is pure static analysis over stored analyses, so every
scenario here is a real, legal SAN sequence (the replay path is what's
under test) paired with hand-placed `MoveEval` numbers -- the numbers are
what let a test isolate exactly one of the four brilliancy criteria at a
time, independent of what the board's actual static-exchange value would
be for that move.
"""

from collections.abc import Sequence

import chess

from chess_coach.coach import HighlightMove, PlayerHighlights, build_highlights
from chess_coach.domain import (
    AnalyzedGame,
    BrilliantThresholds,
    Color,
    GameAnalysis,
    Judgment,
    MoveEval,
    Opening,
    Result,
    TimeClass,
)
from tests.factories import make_game

_JUDGMENTS: tuple[Judgment, ...] = (
    "best",
    "good",
    "inaccuracy",
    "mistake",
    "blunder",
)

# A textbook Greek-gift bishop sacrifice: 17.Bxh7+ captures a pawn (1) that
# only the king defends (the f6 knight also attacks h7, but whichever piece
# recaptures the exchange is over in one capture either way), so the
# opponent's best exchange nets the bishop (3) -- 3 - 1 = 2, exactly the
# default `sac_points` floor.
BXH7_SAC_MOVES = [
    "e4", "e6", "d4", "d5", "Nc3", "Nf6", "Bd3", "dxe4", "Nxe4", "Nbd7",
    "Nxf6+", "Nxf6", "Nf3", "Be7", "O-O", "O-O", "Bxh7+",
]  # fmt: skip

# 3.b4 offers a pawn for nothing in return (Sicilian Wing Gambit) -- a
# 1-point sacrifice, below the 2-point floor that excludes pure pawn sacs.
PAWN_SAC_MOVES = ["e4", "c5", "b4"]

# 4.Bxc6 dxc6 is an ordinary minor-piece trade: the bishop takes a knight
# (3) and a pawn immediately recaptures the bishop (3) -- net 0.
EVEN_TRADE_MOVES = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Bxc6", "dxc6"]

# 4.Nd5 offers the knight (not a capture itself) onto a square the f6
# knight both attacks and can be recaptured on by the e4 pawn -- Nxd5 exd5
# is a clean knight-for-knight trade, so the "sacrifice" wins its material
# straight back: SEE nets 0, not a sacrifice at all.
DEFENDED_TRADE_MOVES = ["e4", "Nf6", "d3", "g6", "Nc3", "d6", "Nd5"]


def _analyzed_game(
    game_id: str,
    san_moves: Sequence[str],
    *,
    color: Color = "white",
    result: Result = "win",
    time_class: TimeClass = "blitz",
    end_time: int = 1_780_300_000,
    opponent: str = "hikaru",
    opening: Opening | None = None,
    eval_overrides: dict[int, dict[str, object]] | None = None,
) -> AnalyzedGame:
    """An analyzed game that replays `san_moves` for real.

    Every ply gets a flat baseline `MoveEval` (`eval_cp=0`, `cp_loss=0`,
    `judgment="best"`) unless `eval_overrides` (keyed by 1-based ply)
    says otherwise. That lets a test aim the exact number one brilliancy
    criterion checks -- e.g. the before-eval -- without the rest of the
    game's numbers getting in the way, while the SAN sequence itself
    still exercises the real replay and static-exchange evaluation.
    """
    game = make_game(
        id=game_id,
        color=color,
        san_moves=list(san_moves),
        result=result,
        time_class=time_class,
        end_time=end_time,
        opponent=opponent,
    )
    overrides = eval_overrides or {}
    board = chess.Board()
    evals: list[MoveEval] = []
    for idx, san in enumerate(san_moves):
        ply = idx + 1
        move = board.parse_san(san)
        fields: dict[str, object] = {
            "ply": ply,
            "san": san,
            "eval_cp": 0,
            "eval_mate": None,
            "best_move": move.uci(),
            "cp_loss": 0,
            "judgment": "best",
        }
        fields.update(overrides.get(ply, {}))
        evals.append(MoveEval.model_validate(fields))
        board.push(move)

    judgment_counts: dict[Judgment, int] = {j: 0 for j in _JUDGMENTS}
    for move_eval in evals:
        judgment_counts[move_eval.judgment] += 1

    analysis = GameAnalysis(
        game_id=game_id,
        depth=16,
        evals=evals,
        overall_acpl=0.0,
        acpl_by_phase={"opening": 0.0, "middlegame": 0.0, "endgame": 0.0},
        judgment_counts=judgment_counts,
    )
    return AnalyzedGame.model_validate(
        {**game.model_dump(), "opening": opening, "analysis": analysis}
    )


# --- empty input -----------------------------------------------------------


def test_build_highlights_empty_games() -> None:
    result = build_highlights([], thresholds=BrilliantThresholds())
    assert result == PlayerHighlights(blunders=[], brilliancies=[])


# --- blunders ----------------------------------------------------------


def test_blunders_use_stored_judgment_never_rederived() -> None:
    """The stored judgment is the source of truth (docs/06-coach.md).

    A tiny `cp_loss` tagged `"blunder"` is still a blunder; a huge
    `cp_loss` tagged `"mistake"` is not -- proving the list is built off
    `MoveEval.judgment` alone, not a cp_loss threshold recomputed here.
    """
    game = _analyzed_game(
        "g1",
        ["e4", "e5", "Nf3", "Nc6", "Bb5"],
        end_time=2_000_000,
        opening=Opening(eco="C60", name="Ruy Lopez", ply=3),
        eval_overrides={
            1: {"cp_loss": 5, "judgment": "blunder"},
            5: {"cp_loss": 500, "judgment": "mistake"},
        },
    )

    result = build_highlights([game], thresholds=BrilliantThresholds())

    assert len(result.blunders) == 1
    blunder: HighlightMove = result.blunders[0]
    assert blunder.cp_loss == 5
    assert blunder.game_id == "g1"
    assert blunder.end_time == 2_000_000
    assert blunder.time_class == "blitz"
    assert blunder.color == "white"
    assert blunder.result == "win"
    assert blunder.opponent == "hikaru"
    assert blunder.opening_name == "Ruy Lopez"
    assert blunder.ply == 1
    assert blunder.move_number == 1
    assert blunder.san == "e4"
    assert blunder.eval_after_cp == 0
    assert blunder.eval_after_mate is None


def test_blunders_sort_newest_game_first_then_ply() -> None:
    """Newest game first (`end_time` desc, ties by `game_id` asc), then
    ascending `ply` within a game (docs/06-coach.md)."""
    newest = _analyzed_game(
        "new",
        ["e4", "e5", "Qh5", "Nc6", "Bc4"],
        end_time=3000,
        eval_overrides={1: {"judgment": "blunder"}, 5: {"judgment": "blunder"}},
    )
    oldest = _analyzed_game(
        "old", ["e4"], end_time=1000, eval_overrides={1: {"judgment": "blunder"}}
    )
    tie_b = _analyzed_game(
        "tie-b", ["e4"], end_time=2000, eval_overrides={1: {"judgment": "blunder"}}
    )
    tie_a = _analyzed_game(
        "tie-a", ["e4"], end_time=2000, eval_overrides={1: {"judgment": "blunder"}}
    )

    result = build_highlights(
        [oldest, tie_b, newest, tie_a], thresholds=BrilliantThresholds()
    )

    assert [(h.game_id, h.ply) for h in result.blunders] == [
        ("new", 1),
        ("new", 5),
        ("tie-a", 1),
        ("tie-b", 1),
        ("old", 1),
    ]


def test_illegal_san_stops_that_games_replay_but_keeps_earlier_moves() -> None:
    """A malformed continuation stops the walk (same tolerance
    `openings/book.py` applies), keeping whatever was already collected."""
    game = _analyzed_game(
        "g1",
        ["e4", "e5", "Nf3"],
        eval_overrides={1: {"judgment": "blunder"}},
    )
    # Corrupt the third SAN so the replay hits an illegal move at idx 2;
    # the first move's blunder, collected before that point, must survive.
    corrupted = game.model_copy(update={"san_moves": ["e4", "e5", "Zz9"]})

    result = build_highlights([corrupted], thresholds=BrilliantThresholds())

    assert [h.ply for h in result.blunders] == [1]


# --- brilliancies: the real sacrifice ---------------------------------


def test_brilliancy_detects_a_sound_sacrifice() -> None:
    """A textbook sound sacrifice: engine-best, a real 2-point exchange
    sacrifice, from a roughly equal position, still sound afterwards."""
    game = _analyzed_game("brill", BXH7_SAC_MOVES)

    result = build_highlights([game], thresholds=BrilliantThresholds())

    assert len(result.brilliancies) == 1
    move = result.brilliancies[0]
    assert move.ply == 17
    assert move.san == "Bxh7+"
    assert move.game_id == "brill"
    assert result.blunders == []


def test_brilliancy_excludes_pure_pawn_sac() -> None:
    """A pure pawn offer (net 1 point) stays below the 2-point floor."""
    game = _analyzed_game("pawnsac", PAWN_SAC_MOVES)

    result = build_highlights([game], thresholds=BrilliantThresholds())

    assert result.brilliancies == []


def test_brilliancy_excludes_even_trade() -> None:
    """Capturing a knight where a pawn recaptures the bishop nets 0 --
    an even trade is not a sacrifice."""
    game = _analyzed_game("eventrade", EVEN_TRADE_MOVES)

    result = build_highlights([game], thresholds=BrilliantThresholds())

    assert result.brilliancies == []


def test_brilliancy_excludes_defended_piece_that_wins_material_back() -> None:
    """Offering a knight onto a square where the recapture chain evens
    out (Nxd5 exd5) is not a sacrifice -- the recursive SEE has to walk
    the full exchange, not just credit the first capture, to see that."""
    game = _analyzed_game("defended", DEFENDED_TRADE_MOVES)

    result = build_highlights([game], thresholds=BrilliantThresholds())

    assert result.brilliancies == []


def test_brilliancy_excludes_non_engine_best() -> None:
    """A real, sound sacrifice that nonetheless cost some cp_loss is not
    the engine's top choice, so it fails criterion 1."""
    game = _analyzed_game(
        "notbest", BXH7_SAC_MOVES, eval_overrides={17: {"cp_loss": 50}}
    )

    result = build_highlights([game], thresholds=BrilliantThresholds())

    assert result.brilliancies == []


def test_brilliancy_excludes_sacrifice_while_already_winning() -> None:
    """The same sound sacrifice, but the position was already winning
    beforehand (before-eval above `winning_cap_cp`) -- a flashy sac in a
    decided game doesn't count."""
    game = _analyzed_game(
        "winning", BXH7_SAC_MOVES, eval_overrides={16: {"eval_cp": 500}}
    )

    result = build_highlights([game], thresholds=BrilliantThresholds())

    assert result.brilliancies == []


def test_brilliancy_excludes_unsound_sacrifice() -> None:
    """The same sacrifice, but the position is bad afterwards (after-eval
    below `sound_floor_cp`) -- an unsound sac is not brilliant."""
    game = _analyzed_game(
        "unsound", BXH7_SAC_MOVES, eval_overrides={17: {"eval_cp": -50}}
    )

    result = build_highlights([game], thresholds=BrilliantThresholds())

    assert result.brilliancies == []


def test_brilliancy_thresholds_come_from_injected_config() -> None:
    """Raising `sac_points` past the sacrifice's actual net excludes a
    move that the default thresholds award -- proving the cutoffs are
    read from the injected `BrilliantThresholds`, not hardcoded."""
    game = _analyzed_game("brill", BXH7_SAC_MOVES)

    result = build_highlights([game], thresholds=BrilliantThresholds(sac_points=5))

    assert result.brilliancies == []
