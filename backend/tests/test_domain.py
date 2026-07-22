"""Smoke tests: the domain contract validates and round-trips."""

from chess_coach.domain import Game, Thresholds


def make_game(**overrides: object) -> Game:
    base: dict[str, object] = {
        "id": "9f3c1a2b",
        "username": "magnus",
        "color": "white",
        "pgn": "1. e4 e5 *",
        "san_moves": ["e4", "e5"],
        "time_control": "600",
        "time_class": "rapid",
        "result": "win",
        "end_time": 1_753_000_000,
        "opponent": "hikaru",
        "player_rating": 2850,
        "opponent_rating": 2800,
    }
    return Game.model_validate({**base, **overrides})


def test_game_round_trips_through_json() -> None:
    game = make_game()
    assert Game.model_validate_json(game.model_dump_json()) == game


def test_accuracy_defaults_to_none() -> None:
    assert make_game().accuracy is None


def test_thresholds_defaults() -> None:
    t = Thresholds()
    assert (t.inaccuracy, t.mistake, t.blunder) == (50, 100, 200)
