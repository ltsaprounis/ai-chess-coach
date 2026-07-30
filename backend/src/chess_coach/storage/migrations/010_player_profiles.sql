-- The durable student profile (docs/06-coach.md, "Player profile"):
-- one row per player -- the LLM narrative plus the facts snapshot it
-- described. Facts are re-derived fresh on every profile GET; this
-- row is the narrative's provenance and the embed paths' single
-- read, not the source of current numbers. games_covered and the
-- covered window ride inside the facts JSON (PlayerProfile carries
-- them).
--
-- Upserted on regeneration under any agent -- the profile is
-- singular, not per-agent like the `reports` cache, so username alone
-- is the primary key.
CREATE TABLE player_profiles (
    username TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,            -- agent that wrote the narrative
    prompt_version TEXT NOT NULL,      -- coach.PROFILE_PROMPT_VERSION at
                                        --   save; staleness metadata,
                                        --   never a cache key
    facts TEXT NOT NULL,               -- JSON PlayerProfile, narrative
                                        --   field excluded
    narrative TEXT NOT NULL,           -- the LLM layer, markdown
    created_at INTEGER NOT NULL        -- unix seconds
);
