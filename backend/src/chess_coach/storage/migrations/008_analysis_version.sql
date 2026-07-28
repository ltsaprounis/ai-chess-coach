-- Stored analyses carry the engine.ANALYSIS_VERSION they were saved
-- under (injected by the API layer; storage never imports engine).
-- The DEFAULT deliberately grandfathers every pre-existing row as
-- version 1 -- the carried-state semantic that predates versioning --
-- so a future version bump marks them all stale at once, exactly like
-- a depth bump does (docs/future-improvements/engine-search-hangs.md,
-- "Re-analysing the existing rows").
ALTER TABLE analyses ADD COLUMN analysis_version INTEGER NOT NULL DEFAULT 1;
