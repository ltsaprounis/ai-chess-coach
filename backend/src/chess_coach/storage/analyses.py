"""Analysis repository (docs/03-storage.md)."""

import json

from chess_coach.domain import OPENING_PLIES, Color, GameAnalysis
from chess_coach.storage.db import Db


def is_player_ply(ply: int, color: Color) -> bool:
    """Plies alternate white/black starting at 1 (white's first move)."""
    return ply % 2 == 1 if color == "white" else ply % 2 == 0


def save_analysis(db: Db, analysis: GameAnalysis, version: int) -> None:
    """Persist the analysis plus its per-perspective aggregates.

    The four aggregate columns (player/opening move counts and summed
    losses) are what lets `opening_stats` aggregate ACPL without
    re-parsing every analyzed game's evals JSON per request; they are
    derived here, once, from the evals being saved. Migration 007
    backfilled them for rows saved before the columns existed.

    `version` is `engine.ANALYSIS_VERSION`, injected by the API layer
    exactly like `depth`/thresholds are — storage records the value,
    never defines it (storage must not import the engine component).
    Migration 008's DEFAULT grandfathers rows saved before versioning
    existed as version 1, so a version bump marks them all stale at
    once for `games_needing_analysis`.
    """
    # The color read runs inside the write transaction: the lock `with
    # db:` takes is what keeps any statement safe against a concurrent
    # writer's COMMIT on the shared connection, and it also makes the
    # read-then-write atomic.
    with db:
        row = db.execute(
            "SELECT color FROM games WHERE id = ?", (analysis.game_id,)
        ).fetchone()
        # No game row: keep the zeros and let the INSERT fail on the
        # foreign key below, exactly as it did before the aggregates.
        color: Color = "white" if row is None else row["color"]
        player_moves = player_loss = opening_moves = opening_loss = 0
        for move_eval in analysis.evals:
            if not is_player_ply(move_eval.ply, color):
                continue
            player_moves += 1
            player_loss += move_eval.cp_loss
            if move_eval.ply <= OPENING_PLIES:
                opening_moves += 1
                opening_loss += move_eval.cp_loss

        db.execute(
            """
            INSERT INTO analyses
                (game_id, depth, evals, analysis_version, overall_acpl,
                 acpl_by_phase, judgment_counts,
                 player_moves, player_loss, opening_moves, opening_loss)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (game_id) DO UPDATE SET
                depth = excluded.depth,
                evals = excluded.evals,
                analysis_version = excluded.analysis_version,
                overall_acpl = excluded.overall_acpl,
                acpl_by_phase = excluded.acpl_by_phase,
                judgment_counts = excluded.judgment_counts,
                player_moves = excluded.player_moves,
                player_loss = excluded.player_loss,
                opening_moves = excluded.opening_moves,
                opening_loss = excluded.opening_loss
            """,
            (
                analysis.game_id,
                analysis.depth,
                json.dumps([e.model_dump() for e in analysis.evals]),
                version,
                analysis.overall_acpl,
                json.dumps(analysis.acpl_by_phase),
                json.dumps(analysis.judgment_counts),
                player_moves,
                player_loss,
                opening_moves,
                opening_loss,
            ),
        )


def analysis_from_json(
    game_id: str,
    depth: int,
    evals_json: str,
    overall_acpl: float,
    acpl_json: str,
    counts_json: str,
) -> GameAnalysis:
    return GameAnalysis.model_validate(
        {
            "game_id": game_id,
            "depth": depth,
            "evals": json.loads(evals_json),
            "overall_acpl": overall_acpl,
            "acpl_by_phase": json.loads(acpl_json),
            "judgment_counts": json.loads(counts_json),
        }
    )
