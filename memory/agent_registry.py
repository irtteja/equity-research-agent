"""
Dynamic agent registry — agents stored as DB records in graph.db.

Agents are configured in the agent_configs table, not hard-coded in Python.
This lets the system add, modify, or disable agents without code deploys.

Schema: agent_configs(id, agent_type, display_name, description,
        prompt_template, tools, trigger_condition, queue_name,
        max_concurrency, priority, enabled, version, created_at, updated_at)
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"

_DDL = """
CREATE TABLE IF NOT EXISTS agent_configs (
    id                TEXT    PRIMARY KEY,
    agent_type        TEXT    NOT NULL,
    display_name      TEXT,
    description       TEXT,
    prompt_template   TEXT,
    tools             TEXT    DEFAULT '[]',
    trigger_condition TEXT,
    queue_name        TEXT,
    max_concurrency   INTEGER DEFAULT 4,
    priority          INTEGER DEFAULT 5,
    enabled           INTEGER DEFAULT 1,
    version           INTEGER DEFAULT 1,
    created_at        TEXT,
    updated_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_type ON agent_configs(agent_type);
CREATE INDEX IF NOT EXISTS idx_agent_enabled ON agent_configs(enabled);
"""

_DEFAULTS = [
    {
        "id": "context_enricher_v1",
        "agent_type": "context",
        "display_name": "Context Enricher",
        "description": "Enriches MetricValue nodes with PRECEDED_BY temporal edges.",
        "tools": json.dumps(["graph_store", "metric_history"]),
        "trigger_condition": "episode_type=xbrl_result",
        "queue_name": "episodes",
        "max_concurrency": 8,
        "priority": 7,
        "prompt_template": "Enrich MetricValue node {{node_id}} for {{ticker}}. Compute delta vs prior period and create PRECEDED_BY edge.",
    },
    {
        "id": "trend_analyzer_v1",
        "agent_type": "trend",
        "display_name": "Trend Analyzer",
        "description": "Builds TrendSummary nodes from MetricValue edge chains.",
        "tools": json.dumps(["graph_store", "edge_query"]),
        "trigger_condition": "episode_type=metric_trend",
        "queue_name": "episodes",
        "max_concurrency": 4,
        "priority": 6,
        "prompt_template": "Analyze trend for {{ticker}} metric {{metric}} over last {{n}} periods. Write TrendSummary node.",
    },
    {
        "id": "document_processor_v1",
        "agent_type": "document",
        "display_name": "Document Processor",
        "description": "Extracts ManagementStatement nodes from annual reports and concall PDFs.",
        "tools": json.dumps(["pdf_reader", "graph_store", "nselib"]),
        "trigger_condition": "new_document",
        "queue_name": "documents",
        "max_concurrency": 4,
        "priority": 5,
        "prompt_template": "Process document {{doc_id}} for {{ticker}}. Extract management statements and financial metrics.",
    },
    {
        "id": "analyst_synthesizer_v1",
        "agent_type": "analyst",
        "display_name": "Analyst Synthesizer",
        "description": "Synthesizes per-ticker graph context into structured analyst notes with conviction scores.",
        "tools": json.dumps(["graph_store", "results_store", "trend_query"]),
        "trigger_condition": "confluence_signal",
        "queue_name": "analyst_queue",
        "max_concurrency": 4,
        "priority": 8,
        "prompt_template": "Synthesize full analyst note for {{ticker}}. Read all graph context and produce BUY/HOLD/WATCH/AVOID recommendation with rationale.",
    },
    {
        "id": "price_monitor_v1",
        "agent_type": "price",
        "display_name": "Price Monitor",
        "description": "Fetches and stores daily price snapshots and 52W range positions.",
        "tools": json.dumps(["nselib", "graph_store", "yfinance"]),
        "trigger_condition": "daily",
        "queue_name": "price_queue",
        "max_concurrency": 2,
        "priority": 9,
        "prompt_template": "Fetch price snapshot for {{ticker}} and update PriceSnapshot + ConfluenceSignal nodes.",
    },
    {
        "id": "alert_evaluator_v1",
        "agent_type": "alert",
        "display_name": "Alert Evaluator",
        "description": "Evaluates alert thresholds and fires notifications for triggered conditions.",
        "tools": json.dumps(["graph_store", "alert_store", "message_bus"]),
        "trigger_condition": "threshold_breach",
        "queue_name": "alert_queue",
        "max_concurrency": 2,
        "priority": 10,
        "prompt_template": "Evaluate alert {{alert_id}} for {{ticker}}. Check current value against threshold and trigger if breached.",
    },
]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_table():
    with _conn() as c:
        c.executescript(_DDL)


_ensure_table()


def register_agent(config: dict) -> str:
    """
    Upsert an agent config by id. Returns the id.
    Pass a dict with any subset of the schema fields.
    """
    now = datetime.now().isoformat()
    agent_id = config["id"]
    with _conn() as c:
        existing = c.execute("SELECT id FROM agent_configs WHERE id=?", (agent_id,)).fetchone()
        if existing:
            # Update mutable fields
            fields = {k: v for k, v in config.items() if k not in ("id", "created_at")}
            fields["updated_at"] = now
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE agent_configs SET {sets} WHERE id=?",
                      list(fields.values()) + [agent_id])
        else:
            row = {
                "id": agent_id,
                "agent_type": config.get("agent_type", "generic"),
                "display_name": config.get("display_name", agent_id),
                "description": config.get("description", ""),
                "prompt_template": config.get("prompt_template", ""),
                "tools": config.get("tools", "[]"),
                "trigger_condition": config.get("trigger_condition", ""),
                "queue_name": config.get("queue_name", ""),
                "max_concurrency": config.get("max_concurrency", 4),
                "priority": config.get("priority", 5),
                "enabled": config.get("enabled", 1),
                "version": config.get("version", 1),
                "created_at": now,
                "updated_at": now,
            }
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" * len(row))
            c.execute(f"INSERT INTO agent_configs ({cols}) VALUES ({placeholders})",
                      list(row.values()))
    return agent_id


def get_agent(agent_id: str) -> dict | None:
    """Return one agent config dict, or None if not found."""
    with _conn() as c:
        row = c.execute("SELECT * FROM agent_configs WHERE id=?", (agent_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["tools"] = json.loads(d["tools"] or "[]")
    except Exception:
        d["tools"] = []
    return d


def list_agents(agent_type: str = None, enabled_only: bool = True) -> list[dict]:
    """Return agent configs, optionally filtered by type and enabled state."""
    with _conn() as c:
        base = "SELECT * FROM agent_configs WHERE 1=1"
        params = []
        if enabled_only:
            base += " AND enabled=1"
        if agent_type:
            base += " AND agent_type=?"
            params.append(agent_type)
        base += " ORDER BY priority DESC, id"
        rows = c.execute(base, params).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["tools"] = json.loads(d["tools"] or "[]")
        except Exception:
            d["tools"] = []
        out.append(d)
    return out


def update_agent(agent_id: str, **kwargs) -> bool:
    """Patch specific fields of an agent config. Returns True if found."""
    kwargs.pop("id", None)
    kwargs["updated_at"] = datetime.now().isoformat()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    with _conn() as c:
        cur = c.execute(f"UPDATE agent_configs SET {sets} WHERE id=?",
                        list(kwargs.values()) + [agent_id])
    return cur.rowcount > 0


def seed_defaults() -> int:
    """Seed the 6 built-in agent configs if the table is empty. Returns count inserted."""
    with _conn() as c:
        count = c.execute("SELECT COUNT(*) FROM agent_configs").fetchone()[0]
    if count > 0:
        return 0
    inserted = 0
    for cfg in _DEFAULTS:
        register_agent(cfg)
        inserted += 1
    return inserted


def get_queue_agents(queue_name: str) -> list[dict]:
    """Return all enabled agents that consume a given queue."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM agent_configs WHERE queue_name=? AND enabled=1 ORDER BY priority DESC",
            (queue_name,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["tools"] = json.loads(d["tools"] or "[]")
        except Exception:
            d["tools"] = []
        out.append(d)
    return out


# Auto-seed on import
seed_defaults()
