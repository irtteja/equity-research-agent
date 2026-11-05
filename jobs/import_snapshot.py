"""
Import graph snapshot into graph.db on session start.

Reads memory_store/snapshots/latest.json (committed to git) and seeds
graph.db with the previously exported nodes and conviction_history rows.
Uses INSERT OR IGNORE so it never overwrites fresher in-session data.

Called automatically by orchestrator.py at session start (before Phase 1).
Also callable standalone to restore a specific snapshot date.

CLI:
  python jobs/import_snapshot.py [--date YYYY-MM-DD] [--dry-run]
"""
import json
import sqlite3
import argparse
from datetime import date
from pathlib import Path

_GRAPH_DB = Path(__file__).parent.parent / "memory_store" / "graph.db"
_SNAP_DIR = Path(__file__).parent.parent / "memory_store" / "snapshots"


def _ensure_graph_schema(conn: sqlite3.Connection):
    """Create tables if they don't exist (graph.db may be brand-new)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY,
            node_type TEXT NOT NULL,
            name TEXT,
            ticker TEXT,
            properties TEXT,
            confidence REAL DEFAULT 0.50,
            belief_state TEXT DEFAULT 'forming',
            evidence_count INTEGER DEFAULT 1,
            enriched INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS conviction_history (
            ticker TEXT,
            record_date TEXT,
            bayesian_conf REAL,
            sentiment_score REAL,
            macro_score REAL,
            technical_score REAL,
            conviction_score REAL,
            signal TEXT,
            belief_state TEXT,
            UNIQUE(ticker, record_date)
        );
    """)
    conn.commit()


def import_snapshot(snap_date: str | None = None, dry_run: bool = False) -> dict:
    if snap_date:
        snap_path = _SNAP_DIR / f"{snap_date}.json"
    else:
        snap_path = _SNAP_DIR / "latest.json"

    if not snap_path.exists():
        return {"status": "no_snapshot", "path": str(snap_path)}

    with open(snap_path, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    nodes = snapshot.get("nodes", [])
    conviction = snapshot.get("conviction_history", [])

    if dry_run:
        return {
            "status": "dry_run",
            "snapshot_date": snapshot.get("exported_at"),
            "would_import_nodes": len(nodes),
            "would_import_conviction": len(conviction),
        }

    _GRAPH_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_GRAPH_DB)
    _ensure_graph_schema(conn)

    nodes_inserted = 0
    nodes_skipped = 0
    for n in nodes:
        props_str = json.dumps(n.get("properties") or {}, ensure_ascii=False)
        try:
            conn.execute("""
                INSERT OR IGNORE INTO nodes
                  (id, node_type, name, ticker, properties,
                   confidence, belief_state, evidence_count, enriched, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                n["id"], n["node_type"], n.get("name"), n.get("ticker"),
                props_str, n.get("confidence", 0.5), n.get("belief_state", "forming"),
                n.get("evidence_count", 1), n.get("enriched", 0), n.get("created_at"),
            ))
            nodes_inserted += conn.execute("SELECT changes()").fetchone()[0]
        except Exception:
            nodes_skipped += 1

    conv_inserted = 0
    for r in conviction:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO conviction_history
                  (ticker, record_date, bayesian_conf, sentiment_score,
                   macro_score, technical_score, conviction_score, signal, belief_state)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                r["ticker"], r["record_date"], r.get("bayesian_conf"),
                r.get("sentiment_score"), r.get("macro_score"),
                r.get("technical_score"), r.get("conviction_score"),
                r.get("signal"), r.get("belief_state"),
            ))
            conv_inserted += conn.execute("SELECT changes()").fetchone()[0]
        except Exception:
            pass

    conn.commit()
    conn.close()

    return {
        "status": "ok",
        "snapshot_date": snapshot.get("exported_at"),
        "nodes_imported": nodes_inserted,
        "nodes_skipped": nodes_skipped,
        "conviction_imported": conv_inserted,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import graph snapshot into graph.db")
    parser.add_argument("--date", help="Import specific date (YYYY-MM-DD)", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported")
    args = parser.parse_args()

    result = import_snapshot(snap_date=args.date, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
