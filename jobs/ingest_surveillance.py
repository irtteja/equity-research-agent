"""
Ingest NSE surveillance flags into graph.db as SurveillanceFlag nodes.

Fetches surveillance_indicator() — the full NSE surveillance matrix.
Creates a SurveillanceFlag node for each stock that has any active flag
(GSM stage 1-6, ESM stage 1-2, LT/ST ASM stage 1-2, IRP).

Stocks with '100' or '0' in all flag columns are considered clean and
get no node (or have any existing node pruned to belief_state='retracted').

The composite_ranker reads SurveillanceFlag nodes and excludes flagged
tickers from BUY recommendations, regardless of their composite score.

CLI:
  python jobs/ingest_surveillance.py [--dry-run]
"""
import json
import sqlite3
import argparse
from datetime import date
from pathlib import Path

REPO = Path(__file__).parent.parent
GRAPH_DB = REPO / "memory_store" / "graph.db"

CLEAN_VAL = {"100", "0", ""}

FLAG_COLS = {
    "gsm":    "GSM",
    "esm":    "ESM",
    "lt_asm": "Long_Term_Additional_Surveillance_Measure (Long Term ASM)",
    "st_asm": "Short_Term_Additional_Surveillance_Measure (Short Term ASM)",
    "irp":    "Insolvency_Resolution_Process(IRP)",
}

# Level '1' in GSM = early stage; '6' = most restrictive
GSM_SEVERITY = {"1": "low", "2": "low", "3": "medium", "4": "medium", "5": "high", "6": "critical"}
ESM_SEVERITY = {"1": "medium", "2": "high"}
ASM_SEVERITY = {"1": "low", "2": "medium"}
IRP_SEVERITY = {"1": "critical", "2": "critical", "3": "critical"}


def _severity(flags: dict) -> str:
    """Compute highest severity across active flags."""
    order = ["critical", "high", "medium", "low"]
    sev = "low"
    for s in [
        GSM_SEVERITY.get(flags.get("gsm", ""), ""),
        ESM_SEVERITY.get(flags.get("esm", ""), ""),
        ASM_SEVERITY.get(flags.get("lt_asm", ""), ""),
        ASM_SEVERITY.get(flags.get("st_asm", ""), ""),
        IRP_SEVERITY.get(flags.get("irp", ""), ""),
    ]:
        if s and order.index(s) < order.index(sev):
            sev = s
    return sev


def run(dry_run: bool = False) -> dict:
    import sys
    sys.path.insert(0, str(REPO))
    from nselib import NSE

    n = NSE()
    raw = n.surveillance_indicator()

    flagged = {}
    for r in raw:
        sym = r.get("Symbol", "")
        if not sym:
            continue
        active = {short: r.get(long, "") for short, long in FLAG_COLS.items()
                  if r.get(long, "") not in CLEAN_VAL}
        if active:
            flagged[sym] = active

    if dry_run:
        return {
            "status": "dry_run",
            "total_surveillance_stocks": len(raw),
            "actually_flagged": len(flagged),
            "sample": dict(list(flagged.items())[:5]),
        }

    today = date.today().isoformat()
    if not GRAPH_DB.exists():
        return {"status": "no_graph_db"}

    conn = sqlite3.connect(GRAPH_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY, node_type TEXT, name TEXT, ticker TEXT,
            properties TEXT, confidence REAL DEFAULT 0.50,
            belief_state TEXT DEFAULT 'forming', evidence_count INTEGER DEFAULT 1,
            enriched INTEGER DEFAULT 0, created_at TEXT
        )
    """)

    inserted = 0
    updated = 0
    retracted = 0

    # Upsert flagged tickers
    for sym, active_flags in flagged.items():
        node_id = f"surv-{sym}"
        severity = _severity(active_flags)
        props = json.dumps({
            "flags": active_flags,
            "severity": severity,
            "as_of": today,
        }, ensure_ascii=False)

        existing = conn.execute(
            "SELECT id, belief_state FROM nodes WHERE id=?", (node_id,)
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE nodes SET properties=?, confidence=1.0,
                  belief_state='confirmed', evidence_count=evidence_count+1
                WHERE id=?
            """, (props, node_id))
            updated += 1
        else:
            conn.execute("""
                INSERT INTO nodes (id, node_type, name, ticker, properties,
                  confidence, belief_state, evidence_count, enriched, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                node_id, "SurveillanceFlag",
                f"{sym} surveillance flag ({severity})", sym,
                props, 1.0, "confirmed", 1, 1, today,
            ))
            inserted += 1

    # Retract any tickers that were previously flagged but are now clean
    cur = conn.execute(
        "SELECT id, ticker FROM nodes WHERE node_type='SurveillanceFlag' AND belief_state='confirmed'"
    )
    for row in cur.fetchall():
        if row[1] not in flagged:
            conn.execute(
                "UPDATE nodes SET belief_state='retracted', confidence=0.1 WHERE id=?",
                (row[0],)
            )
            retracted += 1

    conn.commit()
    conn.close()

    return {
        "status": "ok",
        "total_surveillance_stocks": len(raw),
        "actually_flagged": len(flagged),
        "inserted": inserted,
        "updated": updated,
        "retracted": retracted,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest NSE surveillance flags")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
