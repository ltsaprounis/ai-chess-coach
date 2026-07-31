"""Game repository (docs/03-storage.md)."""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Sequence

from pydantic import BaseModel, Field

from chess_coach.domain import (
    AnalyzedGame,
    Color,
    Game,
    GameAnalysis,
    GameDetail,
    GameSummary,
    Opening,
    OpeningStats,
    PlayerSummary,
    Record,
    Result,
    TimeClass,
)
from chess_coach.storage.analyses import analysis_from_json, is_player_ply
from chess_coach.storage.db import Db


class GameFilters(BaseModel):
    """Optional filters for list_games.

    Paging is ge=0 because SQLite reads a negative LIMIT as
    "unlimited" (and a negative OFFSET as 0) — the same guard
    `AnalyzeRequest.limit` documents.

    `opening_name_like`, `opponent`, and `since`/`until` exist for the
    coach chat toolkit's `find_games` tool (docs/future-improvements/
    coach-chat.md), which queries by what a student says — an
    opponent's name, an opening's name — rather than by ECO code.
    `since`/`until` are an epoch-second window (`since` inclusive,
    `until` exclusive), the same semantics every other windowed query
    here uses.
    """

    opening_eco: str | None = None
    opening_name_like: str | None = None  # case-insensitive substring
    opponent: str | None = None  # case-insensitive exact match
    color: Color | None = None
    result: Result | None = None
    time_class: TimeClass | None = None
    analyzed: bool | None = None
    since: int | None = None
    until: int | None = None
    limit: int = Field(default=100, ge=0)
    offset: int = Field(default=0, ge=0)


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so a literal '%' or '_' in a search term
    (an opening name, say) is never mistaken for a wildcard."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_INSERT_COLUMNS = (
    "id, username, color, pgn, san_moves, time_control, time_class, "
    "result, end_time, opponent, player_rating, opponent_rating, accuracy, "
    "termination, chesscom_uuid"
)


def upsert_games(db: Db, games: list[Game]) -> None:
    """Insert new games; refresh the mutable fields on conflict."""
    rows = [
        (
            game.id,
            game.username,
            game.color,
            game.pgn,
            json.dumps(game.san_moves),
            game.time_control,
            game.time_class,
            game.result,
            game.end_time,
            game.opponent,
            game.player_rating,
            game.opponent_rating,
            game.accuracy,
            game.termination,
            # Ingestion mints ids as "{uuid}:{username}" (usernames
            # cannot contain ':', docs/02-ingestion.md); the raw uuid is
            # kept so perspective rows of one game stay groupable.
            game.id.rsplit(":", 1)[0],
        )
        for game in games
    ]
    with db:
        db.executemany(
            f"""
            INSERT INTO games ({_INSERT_COLUMNS})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                pgn = excluded.pgn,
                san_moves = excluded.san_moves,
                accuracy = excluded.accuracy,
                termination = excluded.termination
            """,
            rows,
        )


_SUMMARY_COLUMNS = (
    "g.id, g.color, g.time_class, g.result, g.end_time, g.opponent, "
    "g.player_rating, g.opponent_rating, g.accuracy, g.termination, "
    "g.san_moves, g.opening_eco, g.opening_name, g.opening_ply"
)

# The exact prefix `playerSystem()` needs for the repertoire
# drill-through (docs/03-storage.md, "GameSummary"). Both this and
# `_SYSTEM_PLIES` below encode the same rule — the player's own first
# three moves define a system (docs/06-coach.md) — and the frontend
# compares `playerSystem(first_plies)` output against `system` strings
# built here, so the three must move in lockstep.
_FIRST_PLIES = 6


def _game_filter_clauses(
    username: str, filters: GameFilters
) -> tuple[list[str], list[object]]:
    """The WHERE fragments `GameFilters` becomes, shared by every query
    that takes one -- `list_games` and `game_record` must agree on what
    a filter means, and one implementation is how they stay agreed."""
    clauses = ["g.username = ?"]
    params: list[object] = [username]
    if filters.color is not None:
        clauses.append("g.color = ?")
        params.append(filters.color)
    if filters.opening_eco is not None:
        clauses.append("g.opening_eco = ?")
        params.append(filters.opening_eco)
    if filters.opening_name_like is not None:
        clauses.append("LOWER(g.opening_name) LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(filters.opening_name_like.lower())}%")
    if filters.opponent is not None:
        clauses.append("LOWER(g.opponent) = LOWER(?)")
        params.append(filters.opponent)
    if filters.result is not None:
        clauses.append("g.result = ?")
        params.append(filters.result)
    if filters.time_class is not None:
        clauses.append("g.time_class = ?")
        params.append(filters.time_class)
    if filters.analyzed is not None:
        clauses.append(
            "a.game_id IS NOT NULL" if filters.analyzed else "a.game_id IS NULL"
        )
    if filters.since is not None:
        clauses.append("g.end_time >= ?")
        params.append(filters.since)
    if filters.until is not None:
        clauses.append("g.end_time < ?")
        params.append(filters.until)
    return clauses, params


def list_games(db: Db, username: str, filters: GameFilters) -> list[GameSummary]:
    clauses, params = _game_filter_clauses(username, filters)

    rows = db.execute(
        f"""
        SELECT {_SUMMARY_COLUMNS}, a.game_id IS NOT NULL AS analyzed
        FROM games AS g LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE {" AND ".join(clauses)}
        ORDER BY g.end_time DESC
        LIMIT ? OFFSET ?
        """,
        [*params, filters.limit, filters.offset],
    ).fetchall()
    return [
        GameSummary.model_validate(
            {
                **_summary_fields(row),
                "opening": _opening_from_row(row),
                "analyzed": bool(row["analyzed"]),
            }
        )
        for row in rows
    ]


def game_record(db: Db, username: str, filters: GameFilters) -> Record:
    """W/D/L over every game matching `filters` -- the counting half of
    the coach's comparison guard (docs/06-coach.md, "Reading a
    comparison").

    One GROUP BY rather than three counts, and it deliberately returns a
    `Record` and not a score: the coach computes both the mean and its
    variance from W/D/L, so handing it a percentage would throw away
    exactly the half it needs.

    Paging on `filters` is ignored -- a record is over the whole match,
    not a page of it.
    """
    clauses, params = _game_filter_clauses(username, filters)
    rows = db.execute(
        f"""
        SELECT g.result AS result, COUNT(*) AS n
        FROM games AS g LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE {" AND ".join(clauses)}
        GROUP BY g.result
        """,
        params,
    ).fetchall()
    counts = {row["result"]: row["n"] for row in rows}
    return Record(
        games=sum(counts.values()),
        wins=counts.get("win", 0),
        losses=counts.get("loss", 0),
        draws=counts.get("draw", 0),
    )


def get_game(db: Db, game_id: str) -> GameDetail | None:
    row = db.execute(
        """
        SELECT g.*, a.depth AS a_depth, a.evals AS a_evals,
               a.overall_acpl AS a_overall,
               a.acpl_by_phase AS a_acpl, a.judgment_counts AS a_counts
        FROM games AS g LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE g.id = ?
        """,
        (game_id,),
    ).fetchone()
    if row is None:
        return None
    return GameDetail.model_validate(
        {
            **_game_fields(row),
            "opening": _opening_from_row(row),
            "analysis": _analysis_from_row(row),
        }
    )


def list_players(db: Db) -> list[PlayerSummary]:
    """Stored players, most games first — the saved-players picker."""
    rows = db.execute(
        """
        SELECT username, COUNT(*) AS games, MAX(end_time) AS last_played
        FROM games
        GROUP BY username
        ORDER BY games DESC, username
        """
    ).fetchall()
    return [
        PlayerSummary(
            username=row["username"],
            games=row["games"],
            last_played=row["last_played"],
        )
        for row in rows
    ]


def latest_game_time(db: Db, username: str) -> int | None:
    """End time of the newest stored game — the sync cutoff."""
    # Aggregate queries always return exactly one row.
    (row,) = db.execute(
        "SELECT MAX(end_time) AS latest FROM games WHERE username = ?",
        (username,),
    ).fetchall()
    latest = row["latest"]
    return None if latest is None else int(latest)


def _needing_analysis_clauses(
    username: str,
    depth: int,
    version: int,
    *,
    since: int | None,
    until: int | None,
    time_class: TimeClass | None,
) -> tuple[list[str], list[object]]:
    """WHERE clauses shared by `games_needing_analysis` and its counter.

    Mirrors `list_analyzed_games`/`count_games`'s window semantics
    exactly (`since` inclusive, `until` exclusive) so a scoped analyze
    run and its remaining count describe the same games. A row shallower
    than `depth` OR saved under an `analysis_version` older than
    `version` both count as needing analysis, so an engine version bump
    (migration 008's grandfathered version 1) re-queues every stored
    game with no endpoint change.
    """
    clauses = [
        "g.username = ?",
        "(a.game_id IS NULL OR a.depth < ? OR a.analysis_version < ?)",
    ]
    params: list[object] = [username, depth, version]
    if since is not None:
        clauses.append("g.end_time >= ?")
        params.append(since)
    if until is not None:
        clauses.append("g.end_time < ?")
        params.append(until)
    if time_class is not None:
        clauses.append("g.time_class = ?")
        params.append(time_class)
    return clauses, params


def games_needing_analysis(
    db: Db,
    username: str,
    depth: int,
    version: int,
    limit: int | None = None,
    *,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> list[Game]:
    """Newest games with no analysis, one shallower than `depth`, or one
    saved under an `analysis_version` older than `version`.

    `version` is `engine.ANALYSIS_VERSION`, injected by the API layer
    like `depth` — storage records it, never defines it. `since`/`until`
    (epoch seconds; `since` inclusive, `until` exclusive) restrict to a
    time window; `time_class` restricts to one time control. All
    default to the full history. Newest-first order is unchanged.
    """
    clauses, params = _needing_analysis_clauses(
        username, depth, version, since=since, until=until, time_class=time_class
    )
    rows = db.execute(
        f"""
        SELECT g.* FROM games AS g
        LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE {" AND ".join(clauses)}
        ORDER BY g.end_time DESC
        LIMIT ?
        """,
        [*params, -1 if limit is None else limit],
    ).fetchall()
    return [Game.model_validate(_game_fields(row)) for row in rows]


def count_games_needing_analysis(
    db: Db,
    username: str,
    depth: int,
    version: int,
    *,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> int:
    """Count of `games_needing_analysis`'s scope, same filters."""
    clauses, params = _needing_analysis_clauses(
        username, depth, version, since=since, until=until, time_class=time_class
    )
    (row,) = db.execute(
        f"""
        SELECT COUNT(*) AS n FROM games AS g
        LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchall()
    return int(row["n"])


def list_analyzed_games(
    db: Db,
    username: str,
    *,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> list[AnalyzedGame]:
    """Games with analyses (plus openings) — the coach report input.

    `since`/`until` (epoch seconds; `since` inclusive, `until`
    exclusive) restrict to a time window; `time_class` restricts to one
    time control. All default to the full history.
    """
    clauses = ["g.username = ?"]
    params: list[object] = [username]
    if since is not None:
        clauses.append("g.end_time >= ?")
        params.append(since)
    if until is not None:
        clauses.append("g.end_time < ?")
        params.append(until)
    if time_class is not None:
        clauses.append("g.time_class = ?")
        params.append(time_class)
    rows = db.execute(
        f"""
        SELECT g.*, a.depth AS a_depth, a.evals AS a_evals,
               a.overall_acpl AS a_overall,
               a.acpl_by_phase AS a_acpl, a.judgment_counts AS a_counts
        FROM games AS g JOIN analyses AS a ON a.game_id = g.id
        WHERE {" AND ".join(clauses)}
        ORDER BY g.end_time DESC
        """,
        params,
    ).fetchall()
    return [
        AnalyzedGame.model_validate(
            {
                **_game_fields(row),
                "opening": _opening_from_row(row),
                "analysis": _analysis_from_row(row),
            }
        )
        for row in rows
    ]


def count_games(
    db: Db,
    username: str,
    *,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> int:
    """Every stored game matching the filters, analyzed or not.

    The "of 1,010" denominator behind the report's coverage statement
    (`PlayerReport.games_in_scope`). The WHERE clause mirrors
    `list_analyzed_games` exactly (`since` inclusive, `until`
    exclusive) minus the analysis join, so the count and the analyzed
    list describe the same scope and can never drift apart.
    """
    clauses = ["g.username = ?"]
    params: list[object] = [username]
    if since is not None:
        clauses.append("g.end_time >= ?")
        params.append(since)
    if until is not None:
        clauses.append("g.end_time < ?")
        params.append(until)
    if time_class is not None:
        clauses.append("g.time_class = ?")
        params.append(time_class)
    (row,) = db.execute(
        f"""
        SELECT COUNT(*) AS n FROM games AS g
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchall()
    return int(row["n"])


def count_analyzed_games(
    db: Db,
    username: str,
    *,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> int:
    """`count_games` plus the analysis join — how many of that scope are
    analyzed.

    `len(list_analyzed_games(...))` for the same filters without
    materializing (and re-parsing the evals of) every game. The profile
    needs the analyzed count for a scope it is not otherwise aggregating
    — the staleness hint compares a stored narrative's coverage against
    the live figure for that same scope (docs/07-api.md, "Player
    profile") — and replaying an archive to learn one integer is not a
    trade worth making.
    """
    clauses = ["g.username = ?"]
    params: list[object] = [username]
    if since is not None:
        clauses.append("g.end_time >= ?")
        params.append(since)
    if until is not None:
        clauses.append("g.end_time < ?")
        params.append(until)
    if time_class is not None:
        clauses.append("g.time_class = ?")
        params.append(time_class)
    (row,) = db.execute(
        f"""
        SELECT COUNT(*) AS n FROM games AS g
        JOIN analyses AS a ON a.game_id = g.id
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchall()
    return int(row["n"])


def list_game_summaries(
    db: Db,
    username: str,
    *,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> list[GameSummary]:
    """Every stored game in the scope as a `GameSummary`, analyzed or
    not — the report's volume layer (docs/06-coach.md, "Volume and
    quality").

    `GameSummary` rather than `Game` on purpose: the volume aggregates
    read ratings, results, openings and the opening ply prefix, all of
    which this row carries, and none of which justify shipping a PGN
    per game. Unpaged, like `list_analyzed_games` — the caller is
    aggregating the whole scope, and a page limit would silently
    truncate the very denominator this exists to provide.

    Window semantics mirror `list_analyzed_games` exactly (`since`
    inclusive, `until` exclusive), so the volume list and the analyzed
    list describe the same scope and cannot drift.
    """
    clauses = ["g.username = ?"]
    params: list[object] = [username]
    if since is not None:
        clauses.append("g.end_time >= ?")
        params.append(since)
    if until is not None:
        clauses.append("g.end_time < ?")
        params.append(until)
    if time_class is not None:
        clauses.append("g.time_class = ?")
        params.append(time_class)
    rows = db.execute(
        f"""
        SELECT {_SUMMARY_COLUMNS}, a.game_id IS NOT NULL AS analyzed
        FROM games AS g LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE {" AND ".join(clauses)}
        ORDER BY g.end_time DESC
        """,
        params,
    ).fetchall()
    return [
        GameSummary.model_validate(
            {
                **_summary_fields(row),
                "opening": _opening_from_row(row),
                "analyzed": bool(row["analyzed"]),
            }
        )
        for row in rows
    ]


def games_missing_opening(db: Db, username: str) -> list[Game]:
    """Games not yet classified (new, or from before openings shipped)."""
    rows = db.execute(
        "SELECT * FROM games WHERE username = ? AND opening_eco IS NULL",
        (username,),
    ).fetchall()
    return [Game.model_validate(_game_fields(row)) for row in rows]


_OpeningKey = tuple[Color, str, str]  # (color, eco, name)


def opening_stats(
    db: Db,
    username: str,
    *,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> list[OpeningStats]:
    """Per-(color, opening) record over classified games, most-played first.

    `since`/`until` (epoch seconds; `since` inclusive, `until`
    exclusive) restrict to a time window; `time_class` restricts to one
    time control. All default to the full history.

    Rows are keyed by (color, eco, name) — the opening is a property of
    the game, and without color the table would merge the openings the
    player chose with the ones their opponents chose against them (see
    docs/06-coach.md, "Repertoire: keyed by the side the player had").
    The two ACPL columns sum the per-perspective aggregates
    `save_analysis` derives from the evals at save time (four integers
    per analyzed game) — never the evals themselves, whose per-request
    JSON parse used to dominate this endpoint at archive scale.
    `opening_acpl`/`avg_cp_loss` are None until a group has at least
    one analyzed game.

    `faced` marks rows whose name describes the opponent's choice: per
    game, opponent-named iff `opening_ply`'s parity belongs to the
    opponent; per row, a strict majority of the group's games (ties are
    chosen). Same rule as `06-coach.md`'s Python implementation over
    analyzed games; `test_repertoire_agreement.py` keeps the two in
    sync.
    """
    clauses = ["g.username = ?", "g.opening_eco IS NOT NULL"]
    params: list[object] = [username]
    if since is not None:
        clauses.append("g.end_time >= ?")
        params.append(since)
    if until is not None:
        clauses.append("g.end_time < ?")
        params.append(until)
    if time_class is not None:
        clauses.append("g.time_class = ?")
        params.append(time_class)
    rows = db.execute(
        f"""
        SELECT g.id AS id, g.color AS color, g.opening_eco AS eco,
               g.opening_name AS name, g.opening_ply AS opening_ply,
               g.result AS result, g.san_moves AS san_moves,
               a.game_id IS NOT NULL AS analyzed,
               a.player_moves AS player_moves, a.player_loss AS player_loss,
               a.opening_moves AS opening_moves, a.opening_loss AS opening_loss
        FROM games AS g LEFT JOIN analyses AS a ON a.game_id = g.id
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchall()

    groups: dict[_OpeningKey, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[(row["color"], row["eco"], row["name"])].append(row)

    stats = [
        _opening_group_stats(key, group_rows) for key, group_rows in groups.items()
    ]
    stats.sort(key=lambda s: (-s.games, s.eco, s.name, s.color))
    return stats


def _opening_group_stats(key: _OpeningKey, rows: list[sqlite3.Row]) -> OpeningStats:
    color, eco, name = key
    wins = sum(1 for r in rows if r["result"] == "win")
    losses = sum(1 for r in rows if r["result"] == "loss")
    draws = sum(1 for r in rows if r["result"] == "draw")
    opponent_named = sum(1 for r in rows if _is_opponent_named(r, color))
    faced = opponent_named * 2 > len(rows)  # strict majority; ties are chosen

    best_line = _most_played_line(rows, color)

    analyzed_games = 0
    total_loss = 0
    total_moves = 0
    opening_loss = 0
    opening_moves = 0
    for row in rows:
        if not row["analyzed"]:
            continue
        analyzed_games += 1
        total_loss += row["player_loss"]
        total_moves += row["player_moves"]
        opening_loss += row["opening_loss"]
        opening_moves += row["opening_moves"]

    return OpeningStats(
        eco=eco,
        name=name,
        color=color,
        system=_format_own_moves(best_line, color),
        first_moves=_format_first_moves(best_line),
        faced=faced,
        games=len(rows),
        wins=wins,
        losses=losses,
        draws=draws,
        analyzed_games=analyzed_games,
        opening_acpl=(
            round(opening_loss / opening_moves, 1) if opening_moves else None
        ),
        avg_cp_loss=(round(total_loss / total_moves, 1) if total_moves else None),
        opening_moves=opening_moves,
        player_moves=total_moves,
    )


_SYSTEM_PLIES = 6  # enough for 3 of either side's own moves


def _most_played_line(rows: list[sqlite3.Row], color: Color) -> tuple[str, ...]:
    """The representative line: most-played *player* sequence first, then
    the most-played full line within it. Ties broken by lowest game id.

    `system` is built only from the player's own moves, so that is what
    must win the vote: several different opponent replies to the same
    player system split its games across multiple 6-ply full lines, and
    picking the single most-played full line (as if `first_moves` alone
    mattered) can hand the row to a minority player line that happens to
    face a less varied opponent (docs/06-coach.md, "Repertoire: keyed by
    the side the player had"). So the player's own move subsequence is
    tallied first — that decides `system` — and only then is the
    most-played full line, within that winning subsequence, picked for
    `first_moves`. Both steps break ties by lowest game id, so the
    result never flaps between calls.
    """
    game_ids_by_line: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for row in rows:
        san_moves: list[str] = json.loads(row["san_moves"])
        line = tuple(san_moves[:_SYSTEM_PLIES])
        game_ids_by_line[line].append(row["id"])

    lines_by_player_seq: dict[tuple[str, ...], list[tuple[str, ...]]] = defaultdict(
        list
    )
    for line in game_ids_by_line:
        player_seq = tuple(
            san for ply, san in enumerate(line, start=1) if is_player_ply(ply, color)
        )
        lines_by_player_seq[player_seq].append(line)

    def player_seq_game_ids(player_seq: tuple[str, ...]) -> list[str]:
        return [
            game_id
            for line in lines_by_player_seq[player_seq]
            for game_id in game_ids_by_line[line]
        ]

    top_player_count = max(len(player_seq_game_ids(seq)) for seq in lines_by_player_seq)
    top_player_seqs = [
        seq
        for seq in lines_by_player_seq
        if len(player_seq_game_ids(seq)) == top_player_count
    ]
    winning_player_seq = min(
        top_player_seqs, key=lambda seq: min(player_seq_game_ids(seq))
    )

    candidate_lines = lines_by_player_seq[winning_player_seq]
    top_count = max(len(game_ids_by_line[line]) for line in candidate_lines)
    candidates = [
        line for line in candidate_lines if len(game_ids_by_line[line]) == top_count
    ]
    return min(candidates, key=lambda line: min(game_ids_by_line[line]))


def _is_opponent_named(row: sqlite3.Row, color: Color) -> bool:
    """True iff `opening_ply`'s parity belongs to the opponent (06-coach.md,
    "Repertoire: keyed by the side the player had")."""
    ply = row["opening_ply"]
    if ply is None:
        return False  # no ply on a classified row: treat as player-named
    return (color == "white") == (ply % 2 == 0)


def _format_own_moves(line: Sequence[str], color: Color, count: int = 3) -> str:
    """The player's own first `count` moves, e.g. "1.d4 2.Nf3 3.Bg5"
    as White or "1...d6 2...Nf6 3...g6" as Black."""
    separator = "." if color == "white" else "..."
    parts: list[str] = []
    for ply, san in enumerate(line, start=1):
        if not is_player_ply(ply, color):
            continue
        move_number = (ply + 1) // 2 if color == "white" else ply // 2
        parts.append(f"{move_number}{separator}{san}")
        if len(parts) == count:
            break
    return " ".join(parts)


def _format_first_moves(line: Sequence[str]) -> str:
    """The line with both sides answering, e.g. "1.d4 e5 2.dxe5 Nc6"."""
    parts: list[str] = []
    for i in range(0, len(line), 2):
        move_number = i // 2 + 1
        if i + 1 < len(line):
            parts.append(f"{move_number}.{line[i]} {line[i + 1]}")
        else:
            parts.append(f"{move_number}.{line[i]}")
    return " ".join(parts)


def set_opening(db: Db, game_id: str, opening: Opening) -> None:
    with db:
        db.execute(
            "UPDATE games SET opening_eco = ?, opening_name = ?, opening_ply = ?"
            " WHERE id = ?",
            (opening.eco, opening.name, opening.ply, game_id),
        )


def _summary_fields(row: sqlite3.Row) -> dict[str, object]:
    san_moves: list[str] = json.loads(row["san_moves"])
    return {
        "id": row["id"],
        "color": row["color"],
        "time_class": row["time_class"],
        "result": row["result"],
        "end_time": row["end_time"],
        "opponent": row["opponent"],
        "player_rating": row["player_rating"],
        "opponent_rating": row["opponent_rating"],
        "accuracy": row["accuracy"],
        "termination": row["termination"],
        "first_plies": san_moves[:_FIRST_PLIES],
    }


def _game_fields(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "username": row["username"],
        "color": row["color"],
        "pgn": row["pgn"],
        "san_moves": json.loads(row["san_moves"]),
        "time_control": row["time_control"],
        "time_class": row["time_class"],
        "result": row["result"],
        "end_time": row["end_time"],
        "opponent": row["opponent"],
        "player_rating": row["player_rating"],
        "opponent_rating": row["opponent_rating"],
        "accuracy": row["accuracy"],
        "termination": row["termination"],
    }


def _opening_from_row(row: sqlite3.Row) -> Opening | None:
    if row["opening_eco"] is None:
        return None
    return Opening(
        eco=row["opening_eco"], name=row["opening_name"], ply=row["opening_ply"]
    )


def _analysis_from_row(row: sqlite3.Row) -> GameAnalysis | None:
    if row["a_depth"] is None:
        return None
    return analysis_from_json(
        game_id=row["id"],
        depth=row["a_depth"],
        evals_json=row["a_evals"],
        overall_acpl=row["a_overall"],
        acpl_json=row["a_acpl"],
        counts_json=row["a_counts"],
    )
