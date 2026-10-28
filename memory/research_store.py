import sqlite3
import json
from datetime import datetime
from config import MEMORY_DB


def _conn():
    conn = sqlite3.connect(MEMORY_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS research_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                query TEXT,
                report TEXT,
                tools_used TEXT,
                gaps_identified TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS generated_tools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                code TEXT NOT NULL,
                trigger_reason TEXT,
                times_used INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS knowledge_base (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                insight TEXT,
                source TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)


def save_research(ticker: str, query: str, report: str, tools_used: list, gaps: list):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO research_sessions (ticker, query, report, tools_used, gaps_identified) VALUES (?,?,?,?,?)",
            (ticker, query, report, json.dumps(tools_used), json.dumps(gaps))
        )


def get_past_research(ticker: str, limit: int = 3) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ticker, query, report, created_at FROM research_sessions WHERE ticker=? ORDER BY created_at DESC LIMIT ?",
            (ticker, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def save_generated_tool(name: str, description: str, code: str, reason: str):
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO generated_tools (name, description, code, trigger_reason) VALUES (?,?,?,?)",
            (name, description, code, reason)
        )


def get_all_generated_tools() -> list:
    with _conn() as conn:
        rows = conn.execute("SELECT name, description, code FROM generated_tools ORDER BY times_used DESC").fetchall()
    return [dict(r) for r in rows]


def increment_tool_usage(name: str):
    with _conn() as conn:
        conn.execute("UPDATE generated_tools SET times_used = times_used + 1 WHERE name=?", (name,))


def save_insight(ticker: str, insight: str, source: str = "agent"):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO knowledge_base (ticker, insight, source) VALUES (?,?,?)",
            (ticker, insight, source)
        )


def get_insights(ticker: str) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT insight, source, created_at FROM knowledge_base WHERE ticker=? ORDER BY created_at DESC LIMIT 10",
            (ticker,)
        ).fetchall()
    return [dict(r) for r in rows]
