"""
ACP Storage Engine - SQLite Persistence for Messages, Cursors & Roster
"""

import os
import sqlite3
from typing import List, Optional, Dict, Any
from .protocol import Message, MessageMode, get_utc_now_iso


class Storage:
    def __init__(self, db_path: str = "acp_chatroom.db"):
        self.db_path = db_path
        # Ensure parent dir exists
        parent_dir = os.path.dirname(os.path.abspath(db_path))
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                target TEXT NOT NULL,
                mode TEXT NOT NULL,
                topic TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """)
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_topic_id 
            ON messages(topic, id)
            """)
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_target 
            ON messages(target, id)
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS cursors (
                handle TEXT NOT NULL,
                topic TEXT NOT NULL,
                last_read_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (handle, topic)
            )
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS roster (
                handle TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
            """)
            conn.commit()

    def save_message(self, msg: Message) -> Message:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO messages (sender, target, mode, topic, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.sender,
                    msg.target,
                    msg.mode.value if isinstance(msg.mode, MessageMode) else str(msg.mode),
                    msg.topic,
                    msg.content,
                    msg.created_at,
                ),
            )
            msg.id = cursor.lastrowid
            conn.commit()
        return msg

    def get_cursor(self, handle: str, topic: str) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT last_read_id FROM cursors WHERE handle = ? AND topic = ?",
                (handle, topic),
            )
            row = cursor.fetchone()
            return row["last_read_id"] if row else 0

    def set_cursor(self, handle: str, topic: str, last_read_id: int) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cursors (handle, topic, last_read_id)
                VALUES (?, ?, ?)
                ON CONFLICT(handle, topic) DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)
                """,
                (handle, topic, last_read_id),
            )
            conn.commit()

    def read_topic_messages(
        self,
        topic: str = "general",
        reader: Optional[str] = None,
        unread_only: bool = True,
        limit: int = 50,
        update_cursor: bool = True,
    ) -> List[Message]:
        last_id = 0
        if reader and unread_only:
            last_id = self.get_cursor(reader, topic)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            if last_id > 0:
                cursor.execute(
                    """
                    SELECT id, sender, target, mode, topic, content, created_at
                    FROM messages
                    WHERE topic = ? AND id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (topic, last_id, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, sender, target, mode, topic, content, created_at
                    FROM messages
                    WHERE topic = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (topic, limit),
                )
            rows = cursor.fetchall()

        messages = [
            Message(
                id=r["id"],
                sender=r["sender"],
                target=r["target"],
                mode=MessageMode(r["mode"]),
                topic=r["topic"],
                content=r["content"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

        # If fetched via DESC (when last_id == 0), restore chronological order
        if last_id == 0:
            messages.reverse()

        if reader and update_cursor and messages:
            max_id = max(m.id for m in messages if m.id is not None)
            self.set_cursor(reader, topic, max_id)

        return messages

    def get_explicit_history(self, handle: str, limit: int = 50) -> List[Message]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, sender, target, mode, topic, content, created_at
                FROM messages
                WHERE mode = 'explicit' AND (target = ? OR sender = ?)
                ORDER BY id DESC
                LIMIT ?
                """,
                (handle, handle, limit),
            )
            rows = cursor.fetchall()

        msgs = [
            Message(
                id=r["id"],
                sender=r["sender"],
                target=r["target"],
                mode=MessageMode(r["mode"]),
                topic=r["topic"],
                content=r["content"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
        msgs.reverse()
        return msgs

    def update_roster(self, handle: str, status: str) -> None:
        now = get_utc_now_iso()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO roster (handle, status, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(handle) DO UPDATE SET status = excluded.status, last_seen = excluded.last_seen
                """,
                (handle, status, now),
            )
            conn.commit()

    def get_roster(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT handle, status, last_seen FROM roster ORDER BY handle ASC")
            rows = cursor.fetchall()
            return [
                {"handle": r["handle"], "status": r["status"], "last_seen": r["last_seen"]}
                for r in rows
            ]
