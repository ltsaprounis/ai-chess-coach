"""Game repository (docs/03-storage.md)."""

import json
import sqlite3

from pydantic import BaseModel

from chess_coach.domain import (
    AnalyzedGame,
    Game,
    GameAnalysis,
    GameDetail,
    GameSummary,
    Opening,
    OpeningStats,
    Result,
    TimeClass,
)
from chess_coach.storage.analyses import analysis_from_json
from chess_coach.storage.db import Db


class GameFilters(BaseModel):
    """Optional filters for list_games."""

    opening_eco: str | None = None
    result: Result | None = None
    time_class: TimeClass | None = None
    analyzed: bool | None = None
    limit: int = 100
    offset: int = 0


_INSERT_COLUMNS = (
    "id, username, color, pgn, san_moves, time_control, time_class, "
    "result, end_time, opponent, player_rating, opponent_rating, accuracy"
)


def upsert_games(db: Db, games: list[Game]) -> None:
    """Insert new games; refresh the mutable fields on conflict."""
    rows = [
        (
            game.id,
            game.username,
            game.color,
            game.pgn,
            json.dumps(game.san_moves),
            game.time_control,
            game.time_class,
            game.result,
            game.end_time,
            game.opponent,
            game.player_rating,
            game.opponent_rating,
            game.accuracy,
        )
        for game in games
    ]
    with db:
        db.executemany(
            f"""
            INSERT INTO games ({_INSERT_COLUMNS})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                pgn = excluded.pgn,
                san_moves = excluded.san_moves,
                accuracy = excluded.accuracy
            """,
            rows,
        )


def list_games(db: Db, username: str, filters: GameFilters) -> list[GameSummary]:
    clauses = ["g.username = ?"]
    params: list[object] = [username]
    if filters.opening_eco is not None:
        clauses.append("g.opening_eco = ?")
        params.append(filters.opening_eco)
    if filters.result is not None:
        clauses.append("g.result = ?")
        params.append(filters.result)
    if filters.time_class is not None:
        clauses.append("g.time_class = ?")
        params.append(filters.time_class)
    if filters.analyzed is not None:
        clauses.append(
            "a.game_id IS NOT NULL" if filters.analyzed else "a.game_id IS NULL"
        )

    rows = db.execute(
        f"""
        SELECT g.*, a.game_id IS NOT NULL AS analyzed
        FROM games AS g LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE {" AND ".join(clauses)}
        ORDER BY g.end_time DESC
        LIMIT ? OFFSET ?
        """,
        [*params, filters.limit, filters.offset],
    ).fetchall()
    return [
        GameSummary.model_validate(
            {
                **_game_fields(row),
                "opening": _opening_from_row(row),
                "analyzed": bool(row["analyzed"]),
            }
        )
        for row in rows
    ]


def get_game(db: Db, game_id: str) -> GameDetail | None:
    row = db.execute(
        """
        SELECT g.*, a.depth AS a_depth, a.evals AS a_evals,
               a.overall_acpl AS a_overall,
               a.acpl_by_phase AS a_acpl, a.judgment_counts AS a_counts
        FROM games AS g LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE g.id = ?
        """,
        (game_id,),
    ).fetchone()
    if row is None:
        return None
    return GameDetail.model_validate(
        {
            **_game_fields(row),
            "opening": _opening_from_row(row),
            "analysis": _analysis_from_row(row),
        }
    )


def latest_game_time(db: Db, username: str) -> int | None:
    """End time of the newest stored game — the sync cutoff."""
    row = db.execute(
        "SELECT MAX(end_time) AS latest FROM games WHERE username = ?",
        (username,),
    ).fetchone()
    latest = row["latest"]
    return None if latest is None else int(latest)


def games_needing_analysis(
    db: Db, username: str, depth: int, limit: int | None = None
) -> list[Game]:
    """Newest games with no analysis, or one shallower than `depth`."""
    rows = db.execute(
        """
        SELECT g.* FROM games AS g
        LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE g.username = ? AND (a.game_id IS NULL OR a.depth < ?)
        ORDER BY g.end_time DESC
        LIMIT ?
        """,
        (username, depth, -1 if limit is None else limit),
    ).fetchall()
    return [Game.model_validate(_game_fields(row)) for row in rows]


def count_games_needing_analysis(db: Db, username: str, depth: int) -> int:
    row = db.execute(
        """
        SELECT COUNT(*) AS n FROM games AS g
        LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE g.username = ? AND (a.game_id IS NULL OR a.depth < ?)
        """,
        (username, depth),
    ).fetchone()
    return int(row["n"])


def list_analyzed_games(db: Db, username: str) -> list[AnalyzedGame]:
    """Games with analyses (plus openings) — the coach report input."""
    rows = db.execute(
        """
        SELECT g.*, a.depth AS a_depth, a.evals AS a_evals,
               a.overall_acpl AS a_overall,
               a.acpl_by_phase AS a_acpl, a.judgment_counts AS a_counts
        FROM games AS g JOIN analyses AS a ON a.game_id = g.id
        WHERE g.username = ?
        ORDER BY g.end_time DESC
        """,
        (username,),
    ).fetchall()
    return [
        AnalyzedGame.model_validate(
            {
                **_game_fields(row),
                "opening": _opening_from_row(row),
                "analysis": _analysis_from_row(row),
            }
        )
        for row in rows
    ]


def games_missing_opening(db: Db, username: str) -> list[Game]:
    """Games not yet classified (new, or from before openings shipped)."""
    rows = db.execute(
        "SELECT * FROM games WHERE username = ? AND opening_eco IS NULL",
        (username,),
    ).fetchall()
    return [Game.model_validate(_game_fields(row)) for row in rows]


def opening_stats(db: Db, username: str) -> list[OpeningStats]:
    """Per-opening record over classified games, most-played first.

    avg_cp_loss stays None until engine analysis exists (milestone 4).
    """
    rows = db.execute(
        """
        SELECT g.opening_eco AS eco, g.opening_name AS name,
               COUNT(*) AS games,
               SUM(g.result = 'win') AS wins,
               SUM(g.result = 'loss') AS losses,
               SUM(g.result = 'draw') AS draws,
               AVG(a.overall_acpl) AS avg_cp_loss
        FROM games AS g LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE g.username = ? AND g.opening_eco IS NOT NULL
        GROUP BY g.opening_eco, g.opening_name
        ORDER BY games DESC, eco
        """,
        (username,),
    ).fetchall()
    return [
        OpeningStats(
            eco=row["eco"],
            name=row["name"],
            games=row["games"],
            wins=row["wins"],
            losses=row["losses"],
            draws=row["draws"],
            avg_cp_loss=(
                None if row["avg_cp_loss"] is None else round(row["avg_cp_loss"], 1)
            ),
        )
        for row in rows
    ]


def set_opening(db: Db, game_id: str, opening: Opening) -> None:
    with db:
        db.execute(
            "UPDATE games SET opening_eco = ?, opening_name = ?, opening_ply = ?"
            " WHERE id = ?",
            (opening.eco, opening.name, opening.ply, game_id),
        )


def _game_fields(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "username": row["username"],
        "color": row["color"],
        "pgn": row["pgn"],
        "san_moves": json.loads(row["san_moves"]),
        "time_control": row["time_control"],
        "time_class": row["time_class"],
        "result": row["result"],
        "end_time": row["end_time"],
        "opponent": row["opponent"],
        "player_rating": row["player_rating"],
        "opponent_rating": row["opponent_rating"],
        "accuracy": row["accuracy"],
    }


def _opening_from_row(row: sqlite3.Row) -> Opening | None:
    if row["opening_eco"] is None:
        return None
    return Opening(
        eco=row["opening_eco"], name=row["opening_name"], ply=row["opening_ply"]
    )


def _analysis_from_row(row: sqlite3.Row) -> GameAnalysis | None:
    if row["a_depth"] is None:
        return None
    return analysis_from_json(
        game_id=row["id"],
        depth=row["a_depth"],
        evals_json=row["a_evals"],
        overall_acpl=row["a_overall"],
        acpl_json=row["a_acpl"],
        counts_json=row["a_counts"],
    )
