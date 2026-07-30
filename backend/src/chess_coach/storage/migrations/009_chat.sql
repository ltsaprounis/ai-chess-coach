-- Coach chat threads and transcripts (docs/future-improvements/
-- coach-chat.md). The stored transcript is the conversation's single
-- source of truth; provider-side sessions are only ever a cache of
-- it, keyed by the opaque provider_state resume token.
--
-- since/until/time_class reuse the reports cache's sentinel
-- convention (0 = open, '' = all controls): SQLite treats NULLs as
-- distinct in a comparison, so a nullable window would make every
-- all-time thread fail to compare equal to another all-time thread's
-- scope, exactly the hazard documented on `reports`.
--
-- game_id has no foreign key on purpose: games are never deleted, so
-- there is no cascade to wire and no orphan risk, and a hard
-- reference would only add migration friction if games is ever
-- restructured.
CREATE TABLE chat_threads (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    game_id TEXT,
    ply INTEGER,
    since INTEGER NOT NULL,
    until INTEGER NOT NULL,
    time_class TEXT NOT NULL,
    provider_state TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX idx_chat_threads_username_updated_at
    ON chat_threads (username, updated_at);

CREATE TABLE chat_messages (
    thread_id TEXT NOT NULL REFERENCES chat_threads (id),
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (thread_id, seq)
);
