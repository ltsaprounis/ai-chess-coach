CREATE TABLE explanations (
    game_id TEXT NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    ply INTEGER NOT NULL,              -- 1-based, matches MoveEval.ply
    agent_id TEXT NOT NULL,            -- coach agent that produced it
    text TEXT NOT NULL,                -- the explanation, markdown
    created_at INTEGER NOT NULL,       -- unix seconds
    PRIMARY KEY (game_id, ply, agent_id)
);
