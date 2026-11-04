"""
Atomic work queue backed by graph.db — shared across parallel workers.

Schema (table: work_queue):
  id         — autoincrement PK
  queue      — logical queue name (e.g. 'context', 'trend', 'document')
  item_key   — unique key within the queue (e.g. node_id, ticker)
  payload    — JSON blob passed to worker
  status     — 'pending' | 'claimed' | 'done' | 'error'
  priority   — higher = picked first (default 0)
  claimed_at — ISO timestamp when a worker claimed it
  worker_id  — string identifier of the claiming worker
  done_at    — ISO timestamp when done or errored
  error      — error message on failure
  created_at — ISO timestamp

UNIQUE(queue, item_key) prevents duplicate work.

Thread safety: each write uses a short-timeout SQLite connection; workers
claim batches atomically with UPDATE … WHERE status='pending' … LIMIT N.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"

_DDL = """
CREATE TABLE IF NOT EXISTS work_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    queue      TEXT    NOT NULL,
    item_key   TEXT    NOT NULL,
    payload    TEXT    DEFAULT '{}',
    status     TEXT    DEFAULT 'pending',
    priority   INTEGER DEFAULT 0,
    claimed_at TEXT,
    worker_id  TEXT,
    done_at    TEXT,
    error      TEXT,
    created_at TEXT    DEFAULT (datetime('now')),
    UNIQUE(queue, item_key)
);
CREATE INDEX IF NOT EXISTS idx_wq_queue_status ON work_queue(queue, status, priority DESC);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_table():
    with _conn() as c:
        c.executescript(_DDL)


_ensure_table()


# ── Write ─────────────────────────────────────────────────────────────────────

def enqueue(queue: str, item_key: str, payload: dict = None, priority: int = 0) -> bool:
    """
    Add item to queue. Idempotent — if item_key already exists in this queue
    (any status), skip. Returns True if inserted, False if already present.
    """
    with _conn() as c:
        try:
            c.execute(
                "INSERT INTO work_queue (queue, item_key, payload, priority) VALUES (?,?,?,?)",
                (queue, item_key, json.dumps(payload or {}), priority),
            )
            return True
        except sqlite3.IntegrityError:
            return False  # already queued


def enqueue_many(queue: str, items: list[dict]) -> int:
    """
    Bulk enqueue. Each item: {'key': ..., 'payload': {...}, 'priority': 0}.
    Returns count of newly inserted items.
    """
    inserted = 0
    with _conn() as c:
        for item in items:
            try:
                c.execute(
                    "INSERT INTO work_queue (queue, item_key, payload, priority) VALUES (?,?,?,?)",
                    (queue, item["key"],
                     json.dumps(item.get("payload", {})),
                     item.get("priority", 0)),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                pass
    return inserted


# ── Claim ─────────────────────────────────────────────────────────────────────

def claim_batch(queue: str, worker_id: str, batch_size: int = 50) -> list[dict]:
    """
    Atomically claim up to batch_size pending items. Returns claimed rows.
    Uses UPDATE … WHERE id IN (SELECT … FOR UPDATE equivalent via rowid trick).
    """
    now = datetime.now().isoformat()
    with _conn() as c:
        # Fetch candidate ids (pending, highest priority first)
        rows = c.execute(
            "SELECT id FROM work_queue WHERE queue=? AND status='pending' "
            "ORDER BY priority DESC, id ASC LIMIT ?",
            (queue, batch_size),
        ).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))

        # Claim them atomically
        c.execute(
            f"UPDATE work_queue SET status='claimed', claimed_at=?, worker_id=? "
            f"WHERE id IN ({placeholders}) AND status='pending'",
            [now, worker_id] + ids,
        )
        # Re-fetch the actually claimed rows
        claimed = c.execute(
            f"SELECT * FROM work_queue WHERE id IN ({placeholders}) AND worker_id=?",
            ids + [worker_id],
        ).fetchall()
    return [dict(r) for r in claimed]


# ── Complete ──────────────────────────────────────────────────────────────────

def mark_done(queue: str, item_key: str):
    with _conn() as c:
        c.execute(
            "UPDATE work_queue SET status='done', done_at=? WHERE queue=? AND item_key=?",
            (datetime.now().isoformat(), queue, item_key),
        )


def mark_error(queue: str, item_key: str, error: str):
    with _conn() as c:
        c.execute(
            "UPDATE work_queue SET status='error', done_at=?, error=? "
            "WHERE queue=? AND item_key=?",
            (datetime.now().isoformat(), error[:500], queue, item_key),
        )


def reset_claimed(queue: str, older_than_seconds: int = 300):
    """Return stale claimed items to pending (crashed worker recovery)."""
    cutoff = datetime.now().isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE work_queue SET status='pending', claimed_at=NULL, worker_id=NULL "
            "WHERE queue=? AND status='claimed' AND claimed_at < datetime(?, '-' || ? || ' seconds')",
            (queue, cutoff, older_than_seconds),
        )


# ── Query ─────────────────────────────────────────────────────────────────────

def queue_depth(queue: str) -> dict:
    """Return {pending, claimed, done, error} counts."""
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) as n FROM work_queue WHERE queue=? GROUP BY status",
            (queue,),
        ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    return {
        "pending": counts.get("pending", 0),
        "claimed": counts.get("claimed", 0),
        "done":    counts.get("done", 0),
        "error":   counts.get("error", 0),
    }


def get_errors(queue: str, limit: int = 20) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT item_key, error, done_at FROM work_queue "
            "WHERE queue=? AND status='error' ORDER BY done_at DESC LIMIT ?",
            (queue, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_queue(queue: str):
    """Remove all rows for a queue. Used for resets."""
    with _conn() as c:
        c.execute("DELETE FROM work_queue WHERE queue=?", (queue,))
