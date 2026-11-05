"""
Export critical graph nodes to committed JSON snapshots.

Solves the ephemeral graph.db problem: since graph.db is gitignored and
resets every session, this job exports the computed output nodes (the ones
that take days of data to build) into versioned JSON files that ARE committed.

Exports:
  - CompositeRank nodes (final ranked watchlist + scores)
  - BusinessQuality nodes (quality tier + quality_score)
  - ConfluenceSignal nodes (conviction signal + score)
  - TechnicalSignal nodes (pos_52w, RSI, 52W high/low)
  - ValuationScore nodes (PE, PB, EV/EBITDA, ROE)
  - conviction_history rows (Bayesian accumulation over sessions)

Output:
  memory_store/snapshots/YYYY-MM-DD.json  — daily archive
  memory_store/snapshots/latest.json      — overwritten every run (import reads this)

CLI:
  python jobs/export_snapshot.py [--date YYYY-MM-DD] [--no-archive]
"""
import json
import sqlite3
import argparse
from datetime import date
from pathlib import Path

_GRAPH_DB = Path(__file__).parent.parent / "memory_store" / "graph.db"
_SNAP_DIR = Path(__file__).parent.parent / "memory_store" / "snapshots"

EXPORT_NODE_TYPES = {
    "CompositeRank",
    "BusinessQuality",
    "ConfluenceSignal",
    "TechnicalSignal",
    "ValuationScore",
    "PeerComparison",
    "SentimentScore",
    "ConcallInsight",
    "BearCase",
    "AnnualReportInsight",
    "AlertEvent",
    "SurveillanceFlag",
}


def _load_nodes(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT id, node_type, name, ticker, properties,
               confidence, belief_state, evidence_count, enriched, created_at
        FROM nodes
        WHERE node_type IN ({})
    """.format(",".join("?" * len(EXPORT_NODE_TYPES))),
        list(EXPORT_NODE_TYPES)
    )
    rows = cur.fetchall()
    nodes = []
    for row in rows:
        props = {}
        try:
            if row[4]:
                props = json.loads(row[4])
        except Exception:
            pass
        nodes.append({
            "id": row[0], "node_type": row[1], "name": row[2],
            "ticker": row[3], "properties": props,
            "confidence": row[5], "belief_state": row[6],
            "evidence_count": row[7], "enriched": row[8],
            "created_at": row[9],
        })
    return nodes


def _load_conviction_history(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT ticker, record_date, bayesian_conf, sentiment_score,
               macro_score, technical_score, conviction_score, signal, belief_state
        FROM conviction_history
        ORDER BY ticker, record_date
    """)
    rows = cur.fetchall()
    return [
        {
            "ticker": r[0], "record_date": r[1],
            "bayesian_conf": r[2], "sentiment_score": r[3],
            "macro_score": r[4], "technical_score": r[5],
            "conviction_score": r[6], "signal": r[7], "belief_state": r[8],
        }
        for r in rows
    ]


def export(run_date: str | None = None, archive: bool = True) -> dict:
    if not _GRAPH_DB.exists():
        return {"status": "error", "reason": "graph.db not found"}

    _SNAP_DIR.mkdir(parents=True, exist_ok=True)
    today = run_date or date.today().isoformat()

    conn = sqlite3.connect(_GRAPH_DB)
    try:
        nodes = _load_nodes(conn)
        conviction = _load_conviction_history(conn)
    finally:
        conn.close()

    snapshot = {
        "exported_at": today,
        "graph_db_path": str(_GRAPH_DB),
        "node_count": len(nodes),
        "conviction_count": len(conviction),
        "nodes": nodes,
        "conviction_history": conviction,
    }

    latest_path = _SNAP_DIR / "latest.json"
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    if archive:
        archive_path = _SNAP_DIR / f"{today}.json"
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    return {
        "status": "ok",
        "nodes": len(nodes),
        "conviction_rows": len(conviction),
        "latest": str(latest_path),
        "archived": str(_SNAP_DIR / f"{today}.json") if archive else None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export graph snapshot to JSON")
    parser.add_argument("--date", help="Override date (YYYY-MM-DD)", default=None)
    parser.add_argument("--no-archive", action="store_true", help="Skip daily archive copy")
    args = parser.parse_args()

    result = export(run_date=args.date, archive=not args.no_archive)
    print(json.dumps(result, indent=2))
