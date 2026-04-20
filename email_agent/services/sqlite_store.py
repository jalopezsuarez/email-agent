"""SQLite persistence layer.

Tables
------
emails          : one row per ingested inbox message.
decisions       : audit log of agent decisions (ai or human).
feedback        : explicit human corrections used for training.
style_samples   : extracted writing samples from the user's Sent Items.
drafts          : AI-generated reply drafts stored in 'Atendidos IA'.
config_kv       : runtime config overrides editable from the panel.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_id TEXT UNIQUE NOT NULL,
    received_at TEXT NOT NULL,
    subject TEXT,
    from_addr TEXT,
    from_name TEXT,
    to_addrs TEXT,
    body_snippet TEXT,
    folder_id TEXT,
    folder_name TEXT,
    category TEXT,                 -- personal/work/transactional/marketing/notification
    status TEXT DEFAULT 'new',     -- new | classified | pending_review | drafted | ignored
    confidence REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER REFERENCES emails(id) ON DELETE CASCADE,
    agent TEXT NOT NULL,           -- coordinator | classifier | responder
    action TEXT NOT NULL,          -- classify | draft | move | skip | flag_review
    target TEXT,                   -- folder id / draft id / ...
    confidence REAL,
    reviewed_by TEXT DEFAULT 'ai', -- ai | human
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER REFERENCES emails(id) ON DELETE CASCADE,
    correct_folder_id TEXT,
    correct_folder_name TEXT,
    correct_category TEXT,
    user_note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS style_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_id TEXT UNIQUE NOT NULL,
    sent_at TEXT,
    recipient TEXT,
    recipient_domain TEXT,
    subject TEXT,
    body_text TEXT,
    greeting TEXT,
    signoff TEXT,
    word_count INTEGER,
    tone_tag TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER REFERENCES emails(id) ON DELETE CASCADE,
    graph_draft_id TEXT,
    body_html TEXT,
    approved INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS config_kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(status);
CREATE INDEX IF NOT EXISTS idx_decisions_email ON decisions(email_id);
CREATE INDEX IF NOT EXISTS idx_drafts_email ON drafts(email_id);
CREATE INDEX IF NOT EXISTS idx_style_recipient ON style_samples(recipient);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(self._path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ---------- emails ----------
    def upsert_email(self, data: dict[str, Any]) -> int:
        with self._conn() as c:
            cur = c.execute("SELECT id FROM emails WHERE graph_id = ?", (data["graph_id"],))
            row = cur.fetchone()
            if row:
                return int(row["id"])
            cur = c.execute(
                """INSERT INTO emails
                   (graph_id, received_at, subject, from_addr, from_name, to_addrs,
                    body_snippet, folder_id, folder_name, status)
                   VALUES (?,?,?,?,?,?,?,?,?, 'new')""",
                (
                    data["graph_id"],
                    data.get("received_at") or _utcnow(),
                    data.get("subject"),
                    data.get("from_addr"),
                    data.get("from_name"),
                    json.dumps(data.get("to_addrs") or []),
                    data.get("body_snippet"),
                    data.get("folder_id"),
                    data.get("folder_name"),
                ),
            )
            return int(cur.lastrowid)

    def update_email(self, email_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(
                f"UPDATE emails SET {cols} WHERE id = ?",
                (*fields.values(), email_id),
            )

    def get_email(self, email_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
            return dict(row) if row else None

    def list_pending_reviews(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM emails WHERE status = 'pending_review' "
                "ORDER BY received_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def last_seen_received_at(self) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(received_at) AS m FROM emails"
            ).fetchone()
            return row["m"] if row and row["m"] else None

    # ---------- decisions ----------
    def log_decision(
        self,
        email_id: int,
        agent: str,
        action: str,
        *,
        target: str | None = None,
        confidence: float | None = None,
        reviewed_by: str = "ai",
        notes: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO decisions
                   (email_id, agent, action, target, confidence, reviewed_by, notes)
                   VALUES (?,?,?,?,?,?,?)""",
                (email_id, agent, action, target, confidence, reviewed_by, notes),
            )
            return int(cur.lastrowid)

    def decision_stats(self) -> dict[str, Any]:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) AS n FROM emails").fetchone()["n"]
            classified = c.execute(
                "SELECT COUNT(*) AS n FROM emails WHERE status='classified'"
            ).fetchone()["n"]
            pending = c.execute(
                "SELECT COUNT(*) AS n FROM emails WHERE status='pending_review'"
            ).fetchone()["n"]
            drafts = c.execute("SELECT COUNT(*) AS n FROM drafts").fetchone()["n"]
            avg_conf = c.execute(
                "SELECT AVG(confidence) AS c FROM decisions WHERE agent='classifier'"
            ).fetchone()["c"]
            return {
                "total_emails": total,
                "classified": classified,
                "pending_review": pending,
                "drafts": drafts,
                "avg_classifier_confidence": round(avg_conf, 3) if avg_conf else None,
            }

    # ---------- feedback ----------
    def add_feedback(self, email_id: int, **fields: Any) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO feedback
                   (email_id, correct_folder_id, correct_folder_name,
                    correct_category, user_note)
                   VALUES (?,?,?,?,?)""",
                (
                    email_id,
                    fields.get("correct_folder_id"),
                    fields.get("correct_folder_name"),
                    fields.get("correct_category"),
                    fields.get("user_note"),
                ),
            )
            return int(cur.lastrowid)

    def list_feedback_for_folder(self, folder_id: str, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT f.*, e.subject, e.body_snippet
                   FROM feedback f JOIN emails e ON e.id = f.email_id
                   WHERE f.correct_folder_id = ? ORDER BY f.id DESC LIMIT ?""",
                (folder_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- style samples ----------
    def insert_style_sample(self, row: dict[str, Any]) -> int | None:
        with self._conn() as c:
            try:
                cur = c.execute(
                    """INSERT INTO style_samples
                       (graph_id, sent_at, recipient, recipient_domain, subject,
                        body_text, greeting, signoff, word_count, tone_tag)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["graph_id"],
                        row.get("sent_at"),
                        row.get("recipient"),
                        row.get("recipient_domain"),
                        row.get("subject"),
                        row.get("body_text"),
                        row.get("greeting"),
                        row.get("signoff"),
                        row.get("word_count"),
                        row.get("tone_tag"),
                    ),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None  # already indexed

    def list_style_samples_for(self, recipient: str | None, limit: int = 5) -> list[dict]:
        with self._conn() as c:
            if recipient:
                rows = c.execute(
                    "SELECT * FROM style_samples WHERE recipient = ? "
                    "ORDER BY sent_at DESC LIMIT ?",
                    (recipient, limit),
                ).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            rows = c.execute(
                "SELECT * FROM style_samples ORDER BY sent_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def style_samples_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM style_samples").fetchone()["n"]

    def tag_style_sample(self, sample_id: int, tone_tag: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE style_samples SET tone_tag = ? WHERE id = ?",
                (tone_tag, sample_id),
            )

    # ---------- drafts ----------
    def insert_draft(
        self,
        email_id: int,
        graph_draft_id: str | None,
        body_html: str,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO drafts (email_id, graph_draft_id, body_html, updated_at)
                   VALUES (?,?,?,?)""",
                (email_id, graph_draft_id, body_html, _utcnow()),
            )
            return int(cur.lastrowid)

    def update_draft(self, draft_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _utcnow()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(
                f"UPDATE drafts SET {cols} WHERE id = ?",
                (*fields.values(), draft_id),
            )

    def list_drafts(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT d.*, e.subject, e.from_addr, e.from_name, e.body_snippet
                   FROM drafts d JOIN emails e ON e.id = d.email_id
                   ORDER BY d.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- config ----------
    def get_config(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM config_kv WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_config(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO config_kv(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def all_config(self) -> dict[str, str]:
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM config_kv").fetchall()
            return {r["key"]: r["value"] for r in rows}
