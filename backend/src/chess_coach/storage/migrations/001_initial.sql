CREATE TABLE games (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    color TEXT NOT NULL,
    pgn TEXT NOT NULL,
    san_moves TEXT NOT NULL,           -- JSON array of SAN strings
    time_control TEXT NOT NULL,
    time_class TEXT NOT NULL,
    result TEXT NOT NULL,
    end_time INTEGER NOT NULL,
    opponent TEXT NOT NULL,
    player_rating INTEGER NOT NULL,
    opponent_rating INTEGER NOT NULL,
    accuracy REAL,                     -- chess.com's own, nullable
    opening_eco TEXT,
    opening_name TEXT,
    opening_ply INTEGER
);

CREATE INDEX idx_games_username_end_time ON games (username, end_time DESC);

CREATE TABLE analyses (
    game_id TEXT PRIMARY KEY REFERENCES games (id) ON DELETE CASCADE,
    depth INTEGER NOT NULL,
    evals TEXT NOT NULL,               -- JSON list[MoveEval]
    acpl_by_phase TEXT NOT NULL,       -- JSON dict[Phase, float]
    judgment_counts TEXT NOT NULL      -- JSON dict[Judgment, int]
);
