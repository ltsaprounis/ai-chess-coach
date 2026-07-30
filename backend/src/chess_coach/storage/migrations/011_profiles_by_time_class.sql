-- Re-key the student profile by time control (docs/06-coach.md,
-- "Player profile"): one row per player per time control, not one per
-- player. A 2100 bullet player and their 1500 rapid self are different
-- students, and a single profile averaging both describes neither --
-- including in the explain and chat prompts that embed it, which now
-- read the row matching the game's own time control.
--
-- '' means "all controls mixed", the same sentinel the `reports` cache
-- uses for an unfiltered scope. Time control alone keys the narrative;
-- the window filter deliberately does not, because its `since` moves
-- with the calendar and would strand every stored row overnight.
--
-- Existing rows are dropped rather than migrated into the '' slot.
-- They were written under profile-v1, which instructed the model to
-- address the student as "you" -- text that, once embedded in another
-- prompt, tells the reading coach *they* are the one who hangs pieces.
-- Facts are re-derived on every GET, so nothing factual is lost; the
-- narrative costs one click to regenerate under profile-v2.
DROP TABLE IF EXISTS player_profiles;

CREATE TABLE player_profiles (
    username TEXT NOT NULL,
    time_class TEXT NOT NULL,          -- '' = all controls mixed
    agent_id TEXT NOT NULL,            -- agent that wrote the narrative
    prompt_version TEXT NOT NULL,      -- coach.PROFILE_PROMPT_VERSION at
                                        --   save; staleness metadata,
                                        --   never a cache key
    facts TEXT NOT NULL,               -- JSON PlayerProfile, narrative
                                        --   field excluded
    narrative TEXT NOT NULL,           -- the LLM layer, markdown
    created_at INTEGER NOT NULL,       -- unix seconds
    PRIMARY KEY (username, time_class)
);
