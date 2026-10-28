"""
Task and checkpoint logging. Every session logs what it did, status, and next steps.
Keeps work from being lost across sessions.
"""
import sqlite3
import json
from datetime import datetime
from config import MEMORY_DB


def _conn():
    conn = sqlite3.connect(MEMORY_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_task_tables():
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'pending',
                priority TEXT DEFAULT 'normal',
                tags TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                status TEXT,
                progress TEXT,
                next_steps TEXT,
                blockers TEXT,
                session_notes TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS macro_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                data TEXT NOT NULL,
                summary TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                exchange TEXT,
                title TEXT,
                category TEXT,
                content TEXT,
                impact TEXT,
                impact_reasoning TEXT,
                date TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)


def log_task(task_id: str, title: str, description: str = "", priority: str = "normal", tags: list = None):
    with _conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO tasks (task_id, title, description, priority, tags)
               VALUES (?,?,?,?,?)""",
            (task_id, title, description, priority, json.dumps(tags or []))
        )


def update_task_status(task_id: str, status: str):
    completed_at = datetime.now().isoformat() if status == "completed" else None
    with _conn() as conn:
        conn.execute(
            "UPDATE tasks SET status=?, updated_at=datetime('now'), completed_at=? WHERE task_id=?",
            (status, completed_at, task_id)
        )


def log_checkpoint(task_id: str, status: str, progress: str, next_steps: str = "", blockers: str = "", notes: str = ""):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO checkpoints (task_id, status, progress, next_steps, blockers, session_notes)
               VALUES (?,?,?,?,?,?)""",
            (task_id, status, progress, next_steps, blockers, notes)
        )
    # Also append to flat log file
    _write_log_file(task_id, status, progress, next_steps, blockers)


def _write_log_file(task_id, status, progress, next_steps, blockers):
    import os
    log_path = os.path.join(os.path.dirname(MEMORY_DB), "..", "logs", "tasks.log")
    log_path = os.path.normpath(log_path)
    entry = (
        f"\n[CHECKPOINT] {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"Task: {task_id}\n"
        f"Status: {status}\n"
        f"Progress: {progress}\n"
        f"Next: {next_steps}\n"
        f"Blockers: {blockers or 'none'}\n"
        f"{'='*60}\n"
    )
    with open(log_path, "a") as f:
        f.write(entry)


def get_incomplete_tasks() -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT task_id, title, status, description, updated_at FROM tasks WHERE status NOT IN ('completed','cancelled') ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_checkpoint(task_id: str) -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (task_id,)
        ).fetchone()
    return dict(row) if row else {}


def save_macro_snapshot(date: str, data: dict, summary: str = ""):
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO macro_snapshots (date, data, summary) VALUES (?,?,?)",
            (date, json.dumps(data, default=str), summary)
        )


def save_announcement(ticker: str, exchange: str, title: str, category: str,
                      content: str, impact: str, reasoning: str, date: str):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO announcements (ticker, exchange, title, category, content, impact, impact_reasoning, date)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ticker, exchange, title, category, content, impact, reasoning, date)
        )


def get_recent_announcements(ticker: str = None, limit: int = 20) -> list:
    with _conn() as conn:
        if ticker:
            rows = conn.execute(
                "SELECT * FROM announcements WHERE ticker=? ORDER BY date DESC LIMIT ?",
                (ticker, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM announcements ORDER BY date DESC LIMIT ?",
                (limit,)
            ).fetchall()
    return [dict(r) for r in rows]
