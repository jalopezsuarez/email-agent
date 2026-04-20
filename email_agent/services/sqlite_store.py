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

    def has_email_graph_id(self, graph_id: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM emails WHERE graph_id = ? LIMIT 1",
                (graph_id,),
            ).fetchone()
            return bool(row)

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
                recipient_addr = _normalise_email(recipient)
                rows = c.execute(
                    "SELECT * FROM style_samples WHERE lower(recipient) = ? "
                    "ORDER BY sent_at DESC LIMIT ?",
                    (recipient_addr, limit),
                ).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            rows = c.execute(
                "SELECT * FROM style_samples ORDER BY sent_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def style_profile(self, recipient: str | None = None, limit: int = 30) -> dict[str, Any]:
        rows = self.list_style_samples_for(recipient, limit=limit)
        return _summarise_style_rows(rows, examples_limit=3)

    def correspondence_reference(self, sender: str | None, limit: int = 3) -> dict[str, Any]:
        sender_addr = _normalise_email(sender)
        sender_domain = sender_addr.split("@", 1)[-1] if sender_addr and "@" in sender_addr else None
        if not sender_addr or not sender_domain:
            return {
                "sender": sender_addr,
                "sender_domain": sender_domain,
                "match_scope": "none",
                "exact_reply_count": 0,
                "domain_reply_count": 0,
                "last_replied_at": None,
                "avg_word_count": None,
                "tone_tags": [],
                "greetings": [],
                "signoffs": [],
                "recent_subjects": [],
                "sample_recipients": [],
                "example_replies": [],
            }

        with self._conn() as c:
            exact_rows = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM style_samples WHERE lower(recipient) = ? "
                    "ORDER BY sent_at DESC LIMIT ?",
                    (sender_addr, limit),
                ).fetchall()
            ]
            exact_count = int(
                c.execute(
                    "SELECT COUNT(*) AS n FROM style_samples WHERE lower(recipient) = ?",
                    (sender_addr,),
                ).fetchone()["n"]
            )
            domain_rows = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM style_samples WHERE lower(recipient_domain) = ? "
                    "ORDER BY sent_at DESC LIMIT ?",
                    (sender_domain, limit),
                ).fetchall()
            ]
            domain_count = int(
                c.execute(
                    "SELECT COUNT(*) AS n FROM style_samples WHERE lower(recipient_domain) = ?",
                    (sender_domain,),
                ).fetchone()["n"]
            )

        reference_rows = exact_rows or domain_rows
        summary = _summarise_style_rows(reference_rows, examples_limit=limit)
        return {
            "sender": sender_addr,
            "sender_domain": sender_domain,
            "match_scope": "exact" if exact_rows else "domain" if domain_rows else "none",
            "exact_reply_count": exact_count,
            "domain_reply_count": domain_count,
            "last_replied_at": summary["last_sent_at"],
            "avg_word_count": summary["avg_word_count"],
            "tone_tags": summary["tone_tags"],
            "greetings": summary["greetings"],
            "signoffs": summary["signoffs"],
            "recent_subjects": summary["recent_subjects"],
            "sample_recipients": summary["sample_recipients"],
            "example_replies": [
                {
                    "recipient": row.get("recipient") or "",
                    "subject": row.get("subject") or "",
                    "sent_at": row.get("sent_at") or "",
                    "body_text": row.get("body_text") or "",
                }
                for row in summary["examples"]
            ],
        }

    def style_samples_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM style_samples").fetchone()["n"]

    def style_sent_range(self) -> tuple[str | None, str | None]:
        with self._conn() as c:
            row = c.execute(
                "SELECT MIN(sent_at) AS min_sent_at, MAX(sent_at) AS max_sent_at "
                "FROM style_samples"
            ).fetchone()
            if not row:
                return None, None
            return row["min_sent_at"], row["max_sent_at"]

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


def _summarise_style_rows(rows: list[dict[str, Any]], *, examples_limit: int = 3) -> dict[str, Any]:
    word_counts = [int(r.get("word_count") or 0) for r in rows if r.get("word_count") not in (None, "")]
    return {
        "sample_count": len(rows),
        "last_sent_at": rows[0].get("sent_at") if rows else None,
        "avg_word_count": round(sum(word_counts) / len(word_counts)) if word_counts else None,
        "greetings": _unique_non_empty([r.get("greeting") for r in rows], limit=5),
        "signoffs": _unique_non_empty([r.get("signoff") for r in rows], limit=5),
        "tone_tags": _unique_non_empty([r.get("tone_tag") for r in rows], limit=5),
        "recent_subjects": _unique_non_empty([r.get("subject") for r in rows], limit=5),
        "sample_recipients": _unique_non_empty([r.get("recipient") for r in rows], limit=5),
        "examples": rows[:examples_limit],
    }


def _unique_non_empty(values: Iterable[Any], limit: int = 5) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def _normalise_email(value: str | None) -> str | None:
    if not value:
        return None
    normalised = value.strip().lower()
    return normalised or None
