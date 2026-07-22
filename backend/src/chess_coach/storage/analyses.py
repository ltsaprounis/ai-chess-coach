"""Analysis repository (docs/03-storage.md)."""

import json

from chess_coach.domain import GameAnalysis
from chess_coach.storage.db import Db


def save_analysis(db: Db, analysis: GameAnalysis) -> None:
    with db:
        db.execute(
            """
            INSERT INTO analyses
                (game_id, depth, evals, acpl_by_phase, judgment_counts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (game_id) DO UPDATE SET
                depth = excluded.depth,
                evals = excluded.evals,
                acpl_by_phase = excluded.acpl_by_phase,
                judgment_counts = excluded.judgment_counts
            """,
            (
                analysis.game_id,
                analysis.depth,
                json.dumps([e.model_dump() for e in analysis.evals]),
                json.dumps(analysis.acpl_by_phase),
                json.dumps(analysis.judgment_counts),
            ),
        )


def list_analyses(db: Db, username: str) -> list[GameAnalysis]:
    rows = db.execute(
        """
        SELECT a.game_id, a.depth, a.evals, a.acpl_by_phase, a.judgment_counts
        FROM analyses AS a JOIN games AS g ON g.id = a.game_id
        WHERE g.username = ?
        """,
        (username,),
    ).fetchall()
    return [
        analysis_from_json(
            game_id=row["game_id"],
            depth=row["depth"],
            evals_json=row["evals"],
            acpl_json=row["acpl_by_phase"],
            counts_json=row["judgment_counts"],
        )
        for row in rows
    ]


def analysis_from_json(
    game_id: str, depth: int, evals_json: str, acpl_json: str, counts_json: str
) -> GameAnalysis:
    return GameAnalysis.model_validate(
        {
            "game_id": game_id,
            "depth": depth,
            "evals": json.loads(evals_json),
            "acpl_by_phase": json.loads(acpl_json),
            "judgment_counts": json.loads(counts_json),
        }
    )
