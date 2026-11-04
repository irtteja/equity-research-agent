"""
Agent performance tracking — records per-run metrics for each agent.

Table: agent_perf(id, agent_id, run_at, items_processed, items_failed,
                   duration_s, queue_name, notes, created_at)

Used by meta_cognition.py to identify underperforming agents and
adjust priorities dynamically in the agent_registry.
"""
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _ensure_table():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_perf (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id         TEXT    NOT NULL,
                run_at           TEXT    NOT NULL,
                items_processed  INTEGER DEFAULT 0,
                items_failed     INTEGER DEFAULT 0,
                duration_s       REAL    DEFAULT 0,
                queue_name       TEXT    DEFAULT '',
                notes            TEXT    DEFAULT '',
                created_at       TEXT    DEFAULT (datetime('now'))
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ap_agent ON agent_perf(agent_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ap_run ON agent_perf(run_at)")


_ensure_table()


def record_run(agent_id: str, items_processed: int, items_failed: int,
               duration_s: float, queue_name: str = "", notes: str = "") -> int:
    """Record a completed agent run. Returns the new row id."""
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO agent_perf
               (agent_id, run_at, items_processed, items_failed,
                duration_s, queue_name, notes)
               VALUES (?,?,?,?,?,?,?)""",
            (agent_id, datetime.now().isoformat(),
             items_processed, items_failed, duration_s, queue_name, notes)
        )
        return cur.lastrowid


def get_stats(agent_id: str, days_back: int = 7) -> dict:
    """Return aggregate stats for one agent over the last N days."""
    cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()
    with _conn() as c:
        row = c.execute(
            """SELECT COUNT(*) as runs,
                      SUM(items_processed) as total_ok,
                      SUM(items_failed) as total_fail,
                      AVG(duration_s) as avg_dur,
                      MAX(run_at) as last_run
               FROM agent_perf
               WHERE agent_id=? AND run_at >= ?""",
            (agent_id, cutoff),
        ).fetchone()
    total = (row["total_ok"] or 0) + (row["total_fail"] or 0)
    success_rate = (row["total_ok"] or 0) / total if total > 0 else None
    return {
        "agent_id":     agent_id,
        "runs":         row["runs"] or 0,
        "total_ok":     row["total_ok"] or 0,
        "total_fail":   row["total_fail"] or 0,
        "success_rate": round(success_rate, 3) if success_rate is not None else None,
        "avg_dur_s":    round(row["avg_dur"] or 0, 2),
        "last_run":     row["last_run"],
    }


def all_stats(days_back: int = 7) -> list[dict]:
    """Return stats for every agent that ran in the last N days."""
    cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()
    with _conn() as c:
        rows = c.execute(
            """SELECT agent_id,
                      COUNT(*) as runs,
                      SUM(items_processed) as total_ok,
                      SUM(items_failed) as total_fail,
                      AVG(duration_s) as avg_dur,
                      MAX(run_at) as last_run
               FROM agent_perf WHERE run_at >= ?
               GROUP BY agent_id ORDER BY total_fail DESC""",
            (cutoff,),
        ).fetchall()
    out = []
    for r in rows:
        total = (r["total_ok"] or 0) + (r["total_fail"] or 0)
        sr = (r["total_ok"] or 0) / total if total > 0 else None
        out.append({
            "agent_id":     r["agent_id"],
            "runs":         r["runs"],
            "total_ok":     r["total_ok"] or 0,
            "total_fail":   r["total_fail"] or 0,
            "success_rate": round(sr, 3) if sr is not None else None,
            "avg_dur_s":    round(r["avg_dur"] or 0, 2),
            "last_run":     r["last_run"],
        })
    return out


def worst_performing(days_back: int = 7, min_runs: int = 3) -> list[dict]:
    """Return agents with the highest failure rate (minimum min_runs runs)."""
    return [
        s for s in all_stats(days_back)
        if s["runs"] >= min_runs and s["success_rate"] is not None and s["success_rate"] < 0.8
    ]


def prune_old(keep_days: int = 90):
    """Delete records older than keep_days."""
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM agent_perf WHERE run_at < ?", (cutoff,))
