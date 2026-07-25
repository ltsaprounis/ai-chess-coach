"""Game repository (docs/03-storage.md)."""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Sequence

from pydantic import BaseModel

from chess_coach.domain import (
    OPENING_PLIES,
    AnalyzedGame,
    Color,
    Game,
    GameAnalysis,
    GameDetail,
    GameSummary,
    Opening,
    OpeningStats,
    PlayerSummary,
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
    "result, end_time, opponent, player_rating, opponent_rating, accuracy, "
    "termination"
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
            game.termination,
        )
        for game in games
    ]
    with db:
        db.executemany(
            f"""
            INSERT INTO games ({_INSERT_COLUMNS})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                pgn = excluded.pgn,
                san_moves = excluded.san_moves,
                accuracy = excluded.accuracy,
                termination = excluded.termination
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


def list_players(db: Db) -> list[PlayerSummary]:
    """Stored players, most games first — the saved-players picker."""
    rows = db.execute(
        """
        SELECT username, COUNT(*) AS games, MAX(end_time) AS last_played
        FROM games
        GROUP BY username
        ORDER BY games DESC, username
        """
    ).fetchall()
    return [
        PlayerSummary(
            username=row["username"],
            games=row["games"],
            last_played=row["last_played"],
        )
        for row in rows
    ]


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


def list_analyzed_games(
    db: Db,
    username: str,
    *,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> list[AnalyzedGame]:
    """Games with analyses (plus openings) — the coach report input.

    `since`/`until` (epoch seconds; `since` inclusive, `until`
    exclusive) restrict to a time window; `time_class` restricts to one
    time control. All default to the full history.
    """
    clauses = ["g.username = ?"]
    params: list[object] = [username]
    if since is not None:
        clauses.append("g.end_time >= ?")
        params.append(since)
    if until is not None:
        clauses.append("g.end_time < ?")
        params.append(until)
    if time_class is not None:
        clauses.append("g.time_class = ?")
        params.append(time_class)
    rows = db.execute(
        f"""
        SELECT g.*, a.depth AS a_depth, a.evals AS a_evals,
               a.overall_acpl AS a_overall,
               a.acpl_by_phase AS a_acpl, a.judgment_counts AS a_counts
        FROM games AS g JOIN analyses AS a ON a.game_id = g.id
        WHERE {" AND ".join(clauses)}
        ORDER BY g.end_time DESC
        """,
        params,
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


_OpeningKey = tuple[Color, str, str]  # (color, eco, name)


def opening_stats(
    db: Db,
    username: str,
    *,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> list[OpeningStats]:
    """Per-(color, opening) record over classified games, most-played first.

    `since`/`until` (epoch seconds; `since` inclusive, `until`
    exclusive) restrict to a time window; `time_class` restricts to one
    time control. All default to the full history.

    Rows are keyed by (color, eco, name) — the opening is a property of
    the game, and without color the table would merge the openings the
    player chose with the ones their opponents chose against them (see
    docs/06-coach.md, "Repertoire: keyed by the side the player had").
    Grouping and the `system`/`first_moves` strings need only the SQL
    columns; the two ACPL columns need per-move data that only exists
    in `analyses.evals`, so those are finished in Python rather than in
    SQL. `opening_acpl`/`avg_cp_loss` are None until a group has at
    least one analyzed game.
    """
    clauses = ["g.username = ?", "g.opening_eco IS NOT NULL"]
    params: list[object] = [username]
    if since is not None:
        clauses.append("g.end_time >= ?")
        params.append(since)
    if until is not None:
        clauses.append("g.end_time < ?")
        params.append(until)
    if time_class is not None:
        clauses.append("g.time_class = ?")
        params.append(time_class)
    rows = db.execute(
        f"""
        SELECT g.id AS id, g.color AS color, g.opening_eco AS eco,
               g.opening_name AS name, g.result AS result,
               g.san_moves AS san_moves, a.evals AS evals
        FROM games AS g LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchall()

    groups: dict[_OpeningKey, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[(row["color"], row["eco"], row["name"])].append(row)

    stats = [
        _opening_group_stats(key, group_rows) for key, group_rows in groups.items()
    ]
    stats.sort(key=lambda s: (-s.games, s.eco, s.name, s.color))
    return stats


def _opening_group_stats(key: _OpeningKey, rows: list[sqlite3.Row]) -> OpeningStats:
    color, eco, name = key
    wins = sum(1 for r in rows if r["result"] == "win")
    losses = sum(1 for r in rows if r["result"] == "loss")
    draws = sum(1 for r in rows if r["result"] == "draw")

    best_line = _most_played_line(rows, color)

    analyzed_games = 0
    total_loss = 0
    total_moves = 0
    opening_loss = 0
    opening_moves = 0
    for row in rows:
        if row["evals"] is None:
            continue
        analyzed_games += 1
        for move_eval in json.loads(row["evals"]):
            if not _is_player_ply(move_eval["ply"], color):
                continue
            cp_loss = move_eval["cp_loss"]
            total_loss += cp_loss
            total_moves += 1
            if move_eval["ply"] <= OPENING_PLIES:
                opening_loss += cp_loss
                opening_moves += 1

    return OpeningStats(
        eco=eco,
        name=name,
        color=color,
        system=_format_own_moves(best_line, color),
        first_moves=_format_first_moves(best_line),
        games=len(rows),
        wins=wins,
        losses=losses,
        draws=draws,
        analyzed_games=analyzed_games,
        opening_acpl=(
            round(opening_loss / opening_moves, 1) if opening_moves else None
        ),
        avg_cp_loss=(round(total_loss / total_moves, 1) if total_moves else None),
        opening_moves=opening_moves,
        player_moves=total_moves,
    )


_SYSTEM_PLIES = 6  # enough for 3 of either side's own moves


def _most_played_line(rows: list[sqlite3.Row], color: Color) -> tuple[str, ...]:
    """The representative line: most-played *player* sequence first, then
    the most-played full line within it. Ties broken by lowest game id.

    `system` is built only from the player's own moves, so that is what
    must win the vote: several different opponent replies to the same
    player system split its games across multiple 6-ply full lines, and
    picking the single most-played full line (as if `first_moves` alone
    mattered) can hand the row to a minority player line that happens to
    face a less varied opponent (docs/06-coach.md, "Repertoire: keyed by
    the side the player had"). So the player's own move subsequence is
    tallied first — that decides `system` — and only then is the
    most-played full line, within that winning subsequence, picked for
    `first_moves`. Both steps break ties by lowest game id, so the
    result never flaps between calls.
    """
    game_ids_by_line: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for row in rows:
        san_moves: list[str] = json.loads(row["san_moves"])
        line = tuple(san_moves[:_SYSTEM_PLIES])
        game_ids_by_line[line].append(row["id"])

    lines_by_player_seq: dict[tuple[str, ...], list[tuple[str, ...]]] = defaultdict(
        list
    )
    for line in game_ids_by_line:
        player_seq = tuple(
            san for ply, san in enumerate(line, start=1) if _is_player_ply(ply, color)
        )
        lines_by_player_seq[player_seq].append(line)

    def player_seq_game_ids(player_seq: tuple[str, ...]) -> list[str]:
        return [
            game_id
            for line in lines_by_player_seq[player_seq]
            for game_id in game_ids_by_line[line]
        ]

    top_player_count = max(len(player_seq_game_ids(seq)) for seq in lines_by_player_seq)
    top_player_seqs = [
        seq
        for seq in lines_by_player_seq
        if len(player_seq_game_ids(seq)) == top_player_count
    ]
    winning_player_seq = min(
        top_player_seqs, key=lambda seq: min(player_seq_game_ids(seq))
    )

    candidate_lines = lines_by_player_seq[winning_player_seq]
    top_count = max(len(game_ids_by_line[line]) for line in candidate_lines)
    candidates = [
        line for line in candidate_lines if len(game_ids_by_line[line]) == top_count
    ]
    return min(candidates, key=lambda line: min(game_ids_by_line[line]))


def _is_player_ply(ply: int, color: Color) -> bool:
    """Plies alternate white/black starting at 1 (white's first move)."""
    return ply % 2 == 1 if color == "white" else ply % 2 == 0


def _format_own_moves(line: Sequence[str], color: Color, count: int = 3) -> str:
    """The player's own first `count` moves, e.g. "1.d4 2.Nf3 3.Bg5"
    as White or "1...d6 2...Nf6 3...g6" as Black."""
    separator = "." if color == "white" else "..."
    parts: list[str] = []
    for ply, san in enumerate(line, start=1):
        if not _is_player_ply(ply, color):
            continue
        move_number = (ply + 1) // 2 if color == "white" else ply // 2
        parts.append(f"{move_number}{separator}{san}")
        if len(parts) == count:
            break
    return " ".join(parts)


def _format_first_moves(line: Sequence[str]) -> str:
    """The line with both sides answering, e.g. "1.d4 e5 2.dxe5 Nc6"."""
    parts: list[str] = []
    for i in range(0, len(line), 2):
        move_number = i // 2 + 1
        if i + 1 < len(line):
            parts.append(f"{move_number}.{line[i]} {line[i + 1]}")
        else:
            parts.append(f"{move_number}.{line[i]}")
    return " ".join(parts)


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
        "termination": row["termination"],
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
