"""Repertoire game repository (docs/03-storage.md).

Feeds the openings-explorer tree (docs/future-improvements/
openings-explorer.md): the one place storage hands out `san_moves`
(and evals) sliced rather than whole, documented on `RepertoireGame`
in `domain.py`.
"""

import json
import sqlite3

from chess_coach.domain import RepertoireGame, TimeClass
from chess_coach.storage.db import Db


def list_repertoire_games(
    db: Db,
    username: str,
    *,
    max_plies: int,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> list[RepertoireGame]:
    """Every stored game in scope, analyzed or not — the repertoire tree
    input (docs/future-improvements/openings-explorer.md, "storage").

    LEFT JOIN on `analyses`: an unanalyzed game still comes back, with
    `evals=None`. Window semantics are identical to
    `list_analyzed_games` (`since` inclusive, `until` exclusive;
    `time_class` optional); all default to the full history.

    `san_moves` and `evals` are both sliced to `max_plies` here, and
    `pgn` is never selected — the documented exception to "`san_moves`
    never crosses the boundary" (docs/03-storage.md, `GameSummary`):
    rows stay bounded by the cap and the consumer is server-side (the
    openings component), so the uncapped-archive-to-browser problem
    that rule exists to prevent does not apply. Evals are parsed from
    JSON before slicing — SQLite cannot slice a JSON array.
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
        SELECT g.id AS id, g.color AS color, g.result AS result,
               g.san_moves AS san_moves, a.evals AS evals
        FROM games AS g LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE {" AND ".join(clauses)}
        ORDER BY g.end_time DESC
        """,
        params,
    ).fetchall()
    return [
        RepertoireGame.model_validate(_repertoire_fields(row, max_plies))
        for row in rows
    ]


def _repertoire_fields(row: sqlite3.Row, max_plies: int) -> dict[str, object]:
    evals_json: str | None = row["evals"]
    return {
        "id": row["id"],
        "color": row["color"],
        "result": row["result"],
        "san_moves": json.loads(row["san_moves"])[:max_plies],
        "evals": None if evals_json is None else json.loads(evals_json)[:max_plies],
    }
