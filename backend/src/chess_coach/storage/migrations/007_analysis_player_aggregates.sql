-- Per-perspective aggregate columns, derived from evals when an
-- analysis is saved. opening_stats previously parsed every analyzed
-- game's full evals JSON in Python on every request — a cost that
-- scales with the archive and is paid per Dashboard filter change
-- (docs/CODEBASE-SCAN-2026-07.md, finding 11); with these columns it
-- reads four integers per game instead.
--
-- Backfill uses JSON1's json_each over the stored evals. Player plies
-- are odd for white, even for black (ply 1 = white's first move); 20
-- is OPENING_PLIES at the time of this migration — migrations are
-- snapshots, not code, so the constant is inlined.
ALTER TABLE analyses ADD COLUMN player_moves INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analyses ADD COLUMN player_loss INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analyses ADD COLUMN opening_moves INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analyses ADD COLUMN opening_loss INTEGER NOT NULL DEFAULT 0;
UPDATE analyses SET
    player_moves = (
        SELECT COUNT(*)
        FROM json_each(analyses.evals) AS je
        WHERE (json_extract(je.value, '$.ply') % 2)
            = (SELECT CASE color WHEN 'white' THEN 1 ELSE 0 END
               FROM games WHERE games.id = analyses.game_id)
    ),
    player_loss = (
        SELECT COALESCE(SUM(json_extract(je.value, '$.cp_loss')), 0)
        FROM json_each(analyses.evals) AS je
        WHERE (json_extract(je.value, '$.ply') % 2)
            = (SELECT CASE color WHEN 'white' THEN 1 ELSE 0 END
               FROM games WHERE games.id = analyses.game_id)
    ),
    opening_moves = (
        SELECT COUNT(*)
        FROM json_each(analyses.evals) AS je
        WHERE json_extract(je.value, '$.ply') <= 20
          AND (json_extract(je.value, '$.ply') % 2)
            = (SELECT CASE color WHEN 'white' THEN 1 ELSE 0 END
               FROM games WHERE games.id = analyses.game_id)
    ),
    opening_loss = (
        SELECT COALESCE(SUM(json_extract(je.value, '$.cp_loss')), 0)
        FROM json_each(analyses.evals) AS je
        WHERE json_extract(je.value, '$.ply') <= 20
          AND (json_extract(je.value, '$.ply') % 2)
            = (SELECT CASE color WHEN 'white' THEN 1 ELSE 0 END
               FROM games WHERE games.id = analyses.game_id)
    );
