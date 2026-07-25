"""Normalize raw chess.com games into domain Games.

Rules documented in docs/02-ingestion.md (informed by the
chess-guess repo): standard chess only, per-player result codes
mapped to win/draw/loss, usernames lowercased, unknown shapes
skipped with a warning rather than raised. The raw per-player result
code is also kept verbatim as `Game.termination` — the win/draw/loss
collapse discards how a game actually ended (timeout vs. resigned vs.
checkmated), which is coaching signal in its own right.
"""

import io
import logging

import chess.pgn

from chess_coach.domain import Color, Game, Result
from chess_coach.ingestion.models import RawGame, RawPlayer

logger = logging.getLogger(__name__)

_WIN = frozenset(("win",))
_DRAW = frozenset(
    (
        "agreed",
        "repetition",
        "stalemate",
        "insufficient",
        "50move",
        "timevsinsufficient",
    )
)
_LOSS = frozenset(("checkmated", "timeout", "resigned", "lose", "abandoned"))
_TIME_CLASSES = frozenset(("bullet", "blitz", "rapid", "daily"))


def normalize_game(raw: RawGame, username: str) -> Game | None:
    """Map one raw archive game to the domain; None means skip."""
    if raw.rules != "chess" or raw.pgn is None:
        return None
    if raw.time_class not in _TIME_CLASSES:
        return None

    user = username.lower()
    color: Color
    me: RawPlayer
    opponent: RawPlayer
    if raw.white.username.lower() == user:
        color, me, opponent = "white", raw.white, raw.black
    elif raw.black.username.lower() == user:
        color, me, opponent = "black", raw.black, raw.white
    else:
        logger.warning("game %s does not involve %s; skipping", raw.uuid, user)
        return None

    result = _classify_result(me.result)
    if result is None:
        logger.warning(
            "unknown result code %r in game %s; skipping", me.result, raw.uuid
        )
        return None

    san_moves = _san_moves(raw.pgn)
    if san_moves is None:
        logger.warning("unparseable PGN in game %s; skipping", raw.uuid)
        return None

    accuracy: float | None = None
    if raw.accuracies is not None:
        accuracy = raw.accuracies.white if color == "white" else raw.accuracies.black

    return Game(
        id=raw.uuid,
        username=user,
        color=color,
        pgn=raw.pgn,
        san_moves=san_moves,
        time_control=raw.time_control,
        time_class=raw.time_class,
        result=result,
        end_time=raw.end_time,
        opponent=opponent.username.lower(),
        player_rating=me.rating,
        opponent_rating=opponent.rating,
        accuracy=accuracy,
        termination=me.result,
    )


def _classify_result(code: str) -> Result | None:
    if code in _WIN:
        return "win"
    if code in _DRAW:
        return "draw"
    if code in _LOSS:
        return "loss"
    return None


def _san_moves(pgn: str) -> list[str] | None:
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None or game.errors:
        return None
    moves: list[str] = []
    board = game.board()
    for move in game.mainline_moves():
        moves.append(board.san(move))
        board.push(move)
    return moves
