-- Raw per-player chess.com result code, nullable: existing rows
-- predate this column and stay NULL until the game is re-synced (see
-- upsert_games' ON CONFLICT clause, which backfills it on re-sync).
ALTER TABLE games ADD COLUMN termination TEXT;
