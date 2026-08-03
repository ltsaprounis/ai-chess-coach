"""Coach chat thread + message repository (docs/03-storage.md,
docs/archive/coach-chat.md).

`ChatMessage` (the transcript unit) lives in `domain` because coach and
the API layer share it too; `ChatThread`/`ChatThreadSummary` are
storage's own result types, like `CachedReport`.
"""

import sqlite3
import time
from typing import Literal

from pydantic import BaseModel

from chess_coach.domain import ChatMessage
from chess_coach.storage.db import Db

ChatScope = Literal["report", "game"]

_TITLE_MAX = 80  # first user message, sliced for the thread-list title


class ChatThread(BaseModel):
    id: str
    username: str
    agent_id: str
    scope: ChatScope
    game_id: str | None = None
    ply: int | None = None
    since: int = 0  # 0 = open, as in ReportKey
    until: int = 0
    time_class: str = ""  # '' = all controls
    provider_state: str | None = None
    created_at: int
    updated_at: int


class ChatThreadSummary(BaseModel):
    """One thread-list row."""

    id: str
    scope: ChatScope
    game_id: str | None
    ply: int | None
    since: int
    until: int
    time_class: str
    agent_id: str
    title: str  # first user message, sliced to ~80 chars
    messages: int  # total rows, the cap check's input
    updated_at: int


def create_chat_thread(
    db: Db,
    *,
    thread_id: str,
    username: str,
    agent_id: str,
    scope: ChatScope,
    game_id: str | None = None,
    ply: int | None = None,
    since: int = 0,
    until: int = 0,
    time_class: str = "",
) -> ChatThread:
    """Reads the clock once; created_at == updated_at on the returned
    row, same one-reading rule as `save_report`."""
    now = int(time.time())
    with db:
        db.execute(
            """
            INSERT INTO chat_threads
                (id, username, agent_id, scope, game_id, ply,
                 since, until, time_class, provider_state,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                thread_id,
                username,
                agent_id,
                scope,
                game_id,
                ply,
                since,
                until,
                time_class,
                now,
                now,
            ),
        )
    return ChatThread(
        id=thread_id,
        username=username,
        agent_id=agent_id,
        scope=scope,
        game_id=game_id,
        ply=ply,
        since=since,
        until=until,
        time_class=time_class,
        provider_state=None,
        created_at=now,
        updated_at=now,
    )


def get_chat_thread(db: Db, thread_id: str) -> ChatThread | None:
    row = db.execute("SELECT * FROM chat_threads WHERE id = ?", (thread_id,)).fetchone()
    return None if row is None else _thread_from_row(row)


def list_chat_threads(db: Db, username: str) -> list[ChatThreadSummary]:
    """Most recently updated first."""
    rows = db.execute(
        """
        SELECT t.id AS id, t.scope AS scope, t.game_id AS game_id,
               t.ply AS ply, t.since AS since, t.until AS until,
               t.time_class AS time_class, t.agent_id AS agent_id,
               t.updated_at AS updated_at,
               COUNT(m.thread_id) AS messages,
               (SELECT content FROM chat_messages
                WHERE thread_id = t.id AND role = 'user'
                ORDER BY seq LIMIT 1) AS first_user_message
        FROM chat_threads AS t
        LEFT JOIN chat_messages AS m ON m.thread_id = t.id
        WHERE t.username = ?
        GROUP BY t.id
        ORDER BY t.updated_at DESC
        """,
        (username,),
    ).fetchall()
    return [
        ChatThreadSummary(
            id=row["id"],
            scope=row["scope"],
            game_id=row["game_id"],
            ply=row["ply"],
            since=row["since"],
            until=row["until"],
            time_class=row["time_class"],
            agent_id=row["agent_id"],
            title=_title(row["first_user_message"]),
            messages=row["messages"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


def _title(first_user_message: str | None) -> str:
    return "" if first_user_message is None else first_user_message[:_TITLE_MAX]


def delete_chat_thread(db: Db, thread_id: str) -> bool:
    """Deletes the thread and its messages in one transaction (explicit
    delete, never FK cascade); False when the thread doesn't exist.

    The existence check runs inside the write transaction — the lock
    `with db:` holds makes the read-then-delete atomic, the same
    pattern `save_analysis` uses for its read-then-write.
    """
    with db:
        exists = db.execute(
            "SELECT 1 FROM chat_threads WHERE id = ?", (thread_id,)
        ).fetchone()
        if exists is None:
            return False
        db.execute("DELETE FROM chat_messages WHERE thread_id = ?", (thread_id,))
        db.execute("DELETE FROM chat_threads WHERE id = ?", (thread_id,))
    return True


def list_chat_messages(db: Db, thread_id: str) -> list[ChatMessage]:
    """Seq ascending — oldest first, the transcript replay order."""
    rows = db.execute(
        """
        SELECT role, content, created_at FROM chat_messages
        WHERE thread_id = ? ORDER BY seq ASC
        """,
        (thread_id,),
    ).fetchall()
    return [
        ChatMessage(
            role=row["role"], content=row["content"], created_at=row["created_at"]
        )
        for row in rows
    ]


def append_chat_exchange(
    db: Db,
    thread_id: str,
    user: ChatMessage,
    assistant: ChatMessage,
    provider_state: str | None,
) -> None:
    """One transaction: both rows at the next two seqs, `provider_state`
    overwritten (None clears it), `updated_at` set to the assistant
    message's `created_at`. A turn is atomic — a user message with no
    reply is never persisted (docs/07-api.md: aborts persist nothing).
    """
    with db:
        row = db.execute(
            "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM chat_messages"
            " WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        assert row is not None  # aggregate queries always return one row
        next_seq = int(row["max_seq"]) + 1
        db.execute(
            """
            INSERT INTO chat_messages (thread_id, seq, role, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (thread_id, next_seq, user.role, user.content, user.created_at),
        )
        db.execute(
            """
            INSERT INTO chat_messages (thread_id, seq, role, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                next_seq + 1,
                assistant.role,
                assistant.content,
                assistant.created_at,
            ),
        )
        db.execute(
            "UPDATE chat_threads SET provider_state = ?, updated_at = ? WHERE id = ?",
            (provider_state, assistant.created_at, thread_id),
        )


def clear_chat_provider_state(db: Db, thread_id: str) -> None:
    """The abort/error path: the discarded turn may have reached the
    provider's warm session, so the next turn must replay from the
    stored transcript rather than resume a diverged session.
    """
    with db:
        db.execute(
            "UPDATE chat_threads SET provider_state = NULL WHERE id = ?",
            (thread_id,),
        )


def _thread_from_row(row: sqlite3.Row) -> ChatThread:
    return ChatThread(
        id=row["id"],
        username=row["username"],
        agent_id=row["agent_id"],
        scope=row["scope"],
        game_id=row["game_id"],
        ply=row["ply"],
        since=row["since"],
        until=row["until"],
        time_class=row["time_class"],
        provider_state=row["provider_state"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
