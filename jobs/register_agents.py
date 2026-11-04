"""
Register new agents in agent_configs table and seed prediction_log.

Idempotent — safe to re-run.

Usage:
  python jobs/register_agents.py
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"
sys.path.insert(0, str(REPO))


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


AGENTS = [
    {
        "agent_type":     "alert_agent",
        "display_name":   "Alert Agent",
        "description":    "Monitors belief_log for state flips, conviction surges/drops, stale BUY signals. Writes AlertEvent nodes.",
        "prompt_template": "",
        "tools":          json.dumps(["belief_log", "nodes", "graph_store"]),
        "trigger_condition": "synthesis_phase",
        "queue_name":     "synthesis",
        "max_concurrency": 1,
        "priority":       8,
        "enabled":        1,
        "version":        "1.0.0",
    },
    {
        "agent_type":     "devil_advocate",
        "display_name":   "Devil's Advocate",
        "description":    "Finds bear arguments for every BUY signal. Scores from 5 sources: trend deterioration, 52W high, belief trajectory, debt/margin, negative mgmt language. Writes BearCase nodes.",
        "prompt_template": "",
        "tools":          json.dumps(["nodes", "belief_log", "graph_store"]),
        "trigger_condition": "synthesis_phase",
        "queue_name":     "synthesis",
        "max_concurrency": 4,
        "priority":       7,
        "enabled":        1,
        "version":        "1.0.0",
    },
    {
        "agent_type":     "risk_agent",
        "display_name":   "Risk Agent",
        "description":    "Portfolio risk: sector concentration, signal quality score, liquidity check. Writes RiskReport nodes.",
        "prompt_template": "",
        "tools":          json.dumps(["nodes", "belief_log", "graph_store"]),
        "trigger_condition": "synthesis_phase",
        "queue_name":     "synthesis",
        "max_concurrency": 1,
        "priority":       6,
        "enabled":        1,
        "version":        "1.0.0",
    },
    {
        "agent_type":     "backtest_agent",
        "display_name":   "Backtest Agent",
        "description":    "Seeds prediction_log from ConfluenceSignals and updates t30/60/90 outcomes from PriceSnapshot nodes.",
        "prompt_template": "",
        "tools":          json.dumps(["nodes", "prediction_log"]),
        "trigger_condition": "meta_phase",
        "queue_name":     "meta",
        "max_concurrency": 1,
        "priority":       5,
        "enabled":        1,
        "version":        "1.0.0",
    },
]


def register_agents():
    with _conn() as c:
        # Ensure agent_configs table exists
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_configs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_type       TEXT NOT NULL UNIQUE,
                display_name     TEXT,
                description      TEXT,
                prompt_template  TEXT,
                tools            TEXT,
                trigger_condition TEXT,
                queue_name       TEXT,
                max_concurrency  INTEGER DEFAULT 1,
                priority         INTEGER DEFAULT 5,
                enabled          INTEGER DEFAULT 1,
                version          TEXT,
                created_at       TEXT DEFAULT (datetime('now')),
                updated_at       TEXT DEFAULT (datetime('now'))
            )
        """)

        registered = 0
        for a in AGENTS:
            existing = c.execute(
                "SELECT id FROM agent_configs WHERE agent_type=?",
                (a["agent_type"],)
            ).fetchone()
            if existing:
                c.execute(
                    """UPDATE agent_configs SET
                       display_name=?, description=?, tools=?,
                       trigger_condition=?, queue_name=?, max_concurrency=?,
                       priority=?, enabled=?, version=?, updated_at=?
                       WHERE agent_type=?""",
                    (a["display_name"], a["description"], a["tools"],
                     a["trigger_condition"], a["queue_name"], a["max_concurrency"],
                     a["priority"], a["enabled"], a["version"],
                     datetime.now().isoformat(), a["agent_type"])
                )
                print(f"  updated: {a['agent_type']}")
            else:
                c.execute(
                    """INSERT INTO agent_configs
                       (agent_type, display_name, description, prompt_template, tools,
                        trigger_condition, queue_name, max_concurrency, priority, enabled, version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (a["agent_type"], a["display_name"], a["description"],
                     a["prompt_template"], a["tools"],
                     a["trigger_condition"], a["queue_name"], a["max_concurrency"],
                     a["priority"], a["enabled"], a["version"])
                )
                registered += 1
                print(f"  registered: {a['agent_type']}")

    return registered


def seed_predictions():
    from memory.prediction_log import seed_from_confluence_signals, update_outcomes
    seeded  = seed_from_confluence_signals()
    updated = update_outcomes()
    return seeded, updated


if __name__ == "__main__":
    print("Registering agents...")
    n = register_agents()
    print(f"Done: {n} new agents registered")

    print("\nSeeding prediction_log from ConfluenceSignals...")
    seeded, updated = seed_predictions()
    print(f"Done: {seeded} predictions seeded, {updated} outcomes updated")
