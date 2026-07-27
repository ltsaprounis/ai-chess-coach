-- Cached whole-report coaching runs. The window columns are NOT NULL
-- with sentinels (since/until 0 = open-ended, time_class '' = all
-- controls) because SQLite treats NULLs as distinct in a primary key;
-- nullable window columns would make every all-time report a cache
-- miss that silently inserts a duplicate row and re-bills the model.
CREATE TABLE reports (
    username TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    since INTEGER NOT NULL,
    until INTEGER NOT NULL,
    time_class TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt TEXT NOT NULL,
    advice TEXT NOT NULL,
    games_analyzed INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (username, agent_id, since, until, time_class, prompt_version)
);
