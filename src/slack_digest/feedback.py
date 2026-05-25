from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_FEEDBACK_ID_RE = re.compile(r"^[\w]+$")

_DEFAULT_DB_PATH = Path.home() / ".slack-digest" / "feedback.db"


def _db_path() -> Path:
    env = os.environ.get("DIGEST_FEEDBACK_DB")
    return Path(env) if env else _DEFAULT_DB_PATH


_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn

    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _init_tables(_conn)
    return _conn


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS digest_items (
            feedback_id   TEXT PRIMARY KEY,
            digest_run_id TEXT NOT NULL,
            digest_date   TEXT NOT NULL,
            section       TEXT NOT NULL,
            channel       TEXT NOT NULL,
            author        TEXT NOT NULL,
            summary       TEXT NOT NULL,
            relevance     TEXT,
            themes        TEXT,
            score         REAL,
            raw_text      TEXT
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            feedback_id   TEXT NOT NULL UNIQUE,
            digest_run_id TEXT NOT NULL,
            digest_date   TEXT NOT NULL,
            section       TEXT NOT NULL,
            channel       TEXT NOT NULL,
            author        TEXT NOT NULL,
            summary       TEXT NOT NULL,
            relevance     TEXT,
            themes        TEXT,
            score         REAL,
            vote          TEXT NOT NULL,
            voted_at      TEXT NOT NULL,
            raw_text      TEXT
        );

        CREATE TABLE IF NOT EXISTS tuning_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date      TEXT NOT NULL,
            votes_up      INTEGER NOT NULL,
            votes_down    INTEGER NOT NULL,
            changes       TEXT NOT NULL,
            config_before TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            applied_at    TEXT,
            dismissed_at  TEXT
        );
    """)


def save_digest_item(
    feedback_id: str,
    digest_run_id: str,
    digest_date: str,
    section: str,
    channel: str,
    author: str,
    summary: str,
    relevance: str | None,
    themes: list[dict] | None,
    score: float | None,
    raw_text: str | None,
) -> None:
    if not _FEEDBACK_ID_RE.match(feedback_id):
        logger.warning(f"Invalid feedback_id: {feedback_id}")
        return
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO digest_items
           (feedback_id, digest_run_id, digest_date, section, channel, author,
            summary, relevance, themes, score, raw_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            feedback_id, digest_run_id, digest_date, section, channel, author,
            summary, relevance, json.dumps(themes) if themes else None, score, raw_text,
        ),
    )
    conn.commit()


def get_current_vote(feedback_id: str) -> str | None:
    conn = _get_conn()
    row = conn.execute("SELECT vote FROM feedback WHERE feedback_id = ?", (feedback_id,)).fetchone()
    return row["vote"] if row else None


def delete_feedback(feedback_id: str) -> bool:
    conn = _get_conn()
    conn.execute("DELETE FROM feedback WHERE feedback_id = ?", (feedback_id,))
    conn.commit()
    return True


def record_feedback(feedback_id: str, vote: str) -> bool:
    if not _FEEDBACK_ID_RE.match(feedback_id):
        logger.warning(f"Invalid feedback_id: {feedback_id}")
        return False
    if vote not in ("up", "down"):
        logger.warning(f"Invalid vote: {vote}")
        return False

    conn = _get_conn()
    row = conn.execute("SELECT * FROM digest_items WHERE feedback_id = ?", (feedback_id,)).fetchone()
    if not row:
        logger.warning(f"No digest_item found for feedback_id: {feedback_id}")
        return False

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO feedback
           (feedback_id, digest_run_id, digest_date, section, channel, author,
            summary, relevance, themes, score, vote, voted_at, raw_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(feedback_id) DO UPDATE SET vote=excluded.vote, voted_at=excluded.voted_at""",
        (
            feedback_id, row["digest_run_id"], row["digest_date"], row["section"],
            row["channel"], row["author"], row["summary"], row["relevance"],
            row["themes"], row["score"], vote, now, row["raw_text"],
        ),
    )
    conn.commit()
    return True


def get_unprocessed_votes() -> list[sqlite3.Row]:
    conn = _get_conn()
    watermark = conn.execute(
        "SELECT MAX(applied_at) FROM tuning_log WHERE status = 'applied'"
    ).fetchone()[0]

    if watermark:
        return conn.execute(
            "SELECT * FROM feedback WHERE voted_at > ?", (watermark,)
        ).fetchall()
    return conn.execute("SELECT * FROM feedback").fetchall()


def get_last_tuning_run_date() -> str | None:
    conn = _get_conn()
    row = conn.execute("SELECT MAX(run_date) FROM tuning_log").fetchone()
    return row[0] if row else None


def has_pending_proposal() -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM tuning_log WHERE status = 'pending' LIMIT 1").fetchone()
    return row is not None


def insert_tuning_log(
    votes_up: int,
    votes_down: int,
    changes: dict,
    config_before: dict,
    status: str = "pending",
    applied_at: str | None = None,
) -> int:
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO tuning_log (run_date, votes_up, votes_down, changes, config_before, status, applied_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (now, votes_up, votes_down, json.dumps(changes), json.dumps(config_before), status, applied_at),
    )
    conn.commit()
    return cursor.lastrowid


def apply_tuning(tuning_log_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM tuning_log WHERE id = ? AND status = 'pending'", (tuning_log_id,)).fetchone()
    if not row:
        return None
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE tuning_log SET status = 'applied', applied_at = ? WHERE id = ?",
        (now, tuning_log_id),
    )
    conn.commit()
    return json.loads(row["changes"])


def dismiss_tuning(tuning_log_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM tuning_log WHERE id = ? AND status = 'pending'", (tuning_log_id,)).fetchone()
    if not row:
        return False
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE tuning_log SET status = 'dismissed', dismissed_at = ? WHERE id = ?",
        (now, tuning_log_id),
    )
    conn.commit()
    return True


def get_last_applied_config_before() -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT config_before FROM tuning_log WHERE status = 'applied' ORDER BY applied_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return json.loads(row["config_before"])


def get_tuning_history(limit: int = 10) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tuning_log ORDER BY run_date DESC LIMIT ?", (limit,)
    ).fetchall()
    results = []
    for row in rows:
        changes = json.loads(row["changes"])
        results.append({
            "id": row["id"],
            "run_date": row["run_date"],
            "votes_up": row["votes_up"],
            "votes_down": row["votes_down"],
            "status": row["status"],
            "observations": changes.get("observations", ""),
        })
    return results
