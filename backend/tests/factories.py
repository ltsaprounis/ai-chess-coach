"""Shared test object factories."""

from chess_coach.domain import Game, GameAnalysis, MoveEval


def make_game(**overrides: object) -> Game:
    base: dict[str, object] = {
        "id": "game-1",
        "username": "testuser",
        "color": "white",
        "pgn": "1. e4 e5 *",
        "san_moves": ["e4", "e5"],
        "time_control": "600",
        "time_class": "rapid",
        "result": "win",
        "end_time": 1_780_300_000,
        "opponent": "hikaru",
        "player_rating": 1500,
        "opponent_rating": 1490,
    }
    return Game.model_validate({**base, **overrides})


def make_analysis(game_id: str = "game-1", depth: int = 16) -> GameAnalysis:
    return GameAnalysis(
        game_id=game_id,
        depth=depth,
        evals=[
            MoveEval(
                ply=1,
                san="e4",
                eval_cp=30,
                eval_mate=None,
                best_move="e2e4",
                cp_loss=0,
                judgment="best",
            ),
            MoveEval(
                ply=2,
                san="e5",
                eval_cp=25,
                eval_mate=None,
                best_move="c7c5",
                cp_loss=5,
                judgment="good",
            ),
        ],
        acpl_by_phase={"opening": 2.5, "middlegame": 0.0, "endgame": 0.0},
        judgment_counts={
            "best": 1,
            "good": 1,
            "inaccuracy": 0,
            "mistake": 0,
            "blunder": 0,
        },
    )
