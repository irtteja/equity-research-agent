"""
Message Bus — SQLite-backed pub/sub for inter-agent communication.

Every agent can:
  publish(sender, event_type, payload, recipient='all', priority='normal')
  subscribe(recipient) → list of unread messages
  ack(message_id)      → mark delivered

Event types (convention):
  trend_signal        TrendAgent found a strong trend
  price_alert         PriceAgent: 52W low/high breach, unusual volume
  announcement_alert  AnnouncementAgent: material filing found
  macro_shift         MacroAgent: FII/DII reversal, VIX spike
  confluence_signal   Orchestrator: multi-agent signal convergence
  gap_detected        any agent: data gap or failure
  agent_health        any agent: heartbeat with status
  action_required     Orchestrator → specific agent: do something now

Agents check their inbox at the start of every run.
Orchestrator reads all messages (recipient='all' + recipient='orchestrator').
"""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

DB = Path(__file__).parent.parent / "memory_store" / "graph.db"

_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    sender      TEXT NOT NULL,
    recipient   TEXT NOT NULL DEFAULT 'all',
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    priority    TEXT NOT NULL DEFAULT 'normal',
    created_at  TEXT NOT NULL,
    read_at     TEXT,
    ttl_hours   INTEGER DEFAULT 48
);
CREATE INDEX IF NOT EXISTS idx_msg_recipient  ON messages(recipient);
CREATE INDEX IF NOT EXISTS idx_msg_event_type ON messages(event_type);
CREATE INDEX IF NOT EXISTS idx_msg_read_at    ON messages(read_at);
CREATE INDEX IF NOT EXISTS idx_msg_created_at ON messages(created_at);
"""

def _conn():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def publish(sender: str, event_type: str, payload: dict,
            recipient: str = "all", priority: str = "normal",
            ttl_hours: int = 48) -> str:
    mid = str(uuid.uuid4())
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages(id,sender,recipient,event_type,payload,priority,created_at,ttl_hours) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (mid, sender, recipient, event_type, json.dumps(payload), priority,
             datetime.now().isoformat(), ttl_hours)
        )
    return mid


def subscribe(recipient: str, event_types: list[str] | None = None,
              unread_only: bool = True) -> list[dict]:
    """Return messages for this recipient (and broadcast 'all' messages)."""
    with _conn() as conn:
        clauses = ["(recipient=? OR recipient='all')"]
        params: list = [recipient]
        if unread_only:
            clauses.append("read_at IS NULL")
        if event_types:
            placeholders = ",".join("?" * len(event_types))
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(event_types)
        # ignore expired messages
        clauses.append("datetime(created_at, '+' || ttl_hours || ' hours') > datetime('now')")
        where = " AND ".join(clauses)
        rows = conn.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY created_at ASC",
            params
        ).fetchall()
        return [dict(r) for r in rows]


def ack(message_id: str):
    with _conn() as conn:
        conn.execute(
            "UPDATE messages SET read_at=? WHERE id=?",
            (datetime.now().isoformat(), message_id)
        )


def ack_all(recipient: str):
    with _conn() as conn:
        conn.execute(
            "UPDATE messages SET read_at=? WHERE (recipient=? OR recipient='all') AND read_at IS NULL",
            (datetime.now().isoformat(), recipient)
        )


def get_recent(event_type: str | None = None, hours: int = 24, limit: int = 50) -> list[dict]:
    """Read recent messages regardless of read state — for orchestrator overview."""
    with _conn() as conn:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        if event_type:
            rows = conn.execute(
                "SELECT * FROM messages WHERE created_at > ? AND event_type=? ORDER BY created_at DESC LIMIT ?",
                (cutoff, event_type, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE created_at > ? ORDER BY created_at DESC LIMIT ?",
                (cutoff, limit)
            ).fetchall()
        return [dict(r) for r in rows]


def purge_expired():
    with _conn() as conn:
        conn.execute(
            "DELETE FROM messages WHERE datetime(created_at, '+' || ttl_hours || ' hours') <= datetime('now')"
        )
