-- One stored row per (game, perspective). Every column on a games row
-- except the shared PGN fields is one player's view of the game, so
-- the chess.com uuid alone cannot be the row's identity: a game
-- between two tracked players needs a row per side, and keying on the
-- uuid made the second player's sync collide with the first's row and
-- silently drop their copy (docs/CODEBASE-SCAN-2026-07.md, finding 1).
-- The id becomes "{uuid}:{username}" — chess.com usernames cannot
-- contain ':' — and the raw uuid is kept in chesscom_uuid, which both
-- preserves provenance and keeps a later cross-perspective eval reuse
-- or normalization migration possible.
--
-- The old PRIMARY KEY guaranteed at most one perspective per uuid, so
-- the rewrite is 1:1 and cannot collide. Child tables are rewritten
-- first, while games.id still holds the uuid they join on; foreign
-- keys go OFF around the transiently inconsistent state and back ON
-- for any migrations that follow in the same run (the pragma is
-- per-connection, so it must not be left off).
PRAGMA foreign_keys=OFF;
BEGIN;
ALTER TABLE games ADD COLUMN chesscom_uuid TEXT;
UPDATE explanations SET game_id = game_id || ':' ||
    (SELECT username FROM games WHERE games.id = explanations.game_id);
UPDATE analyses SET game_id = game_id || ':' ||
    (SELECT username FROM games WHERE games.id = analyses.game_id);
UPDATE games SET chesscom_uuid = id;
UPDATE games SET id = id || ':' || username;
COMMIT;
PRAGMA foreign_keys=ON;
