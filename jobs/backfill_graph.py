"""
One-shot backfill: loads all existing stored data into the knowledge graph.

Processes:
  memory_store/results/<TICKER>.jsonl  → MetricValue nodes via result_to_graph()
  memory_store/documents/**/*.json     → facts/statements via facts_to_graph()

Safe to re-run — episode_bridge and graph_store are both idempotent.
Run: python jobs/backfill_graph.py
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from memory.episode_bridge import result_to_graph, facts_to_graph, statement_to_graph
from memory.graph_store import get_store
from memory.task_log import log_task, update_task_status, log_checkpoint


def backfill_xbrl() -> dict:
    results_dir = REPO / "memory_store" / "results"
    if not results_dir.exists():
        return {"tickers": 0, "quarters": 0, "nodes": 0}

    gs = get_store()
    tickers = quarters = nodes = 0

    for path in sorted(results_dir.glob("*.jsonl")):
        ticker = path.stem
        if ticker.startswith("_"):
            continue
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        if not rows:
            continue

        tickers += 1
        for row in rows:
            nids = result_to_graph(ticker, row)
            nodes  += len(nids)
            quarters += 1

        print(f"  {ticker}: {len(rows)} quarters → {nodes} nodes so far")

    return {"tickers": tickers, "quarters": quarters, "nodes": nodes}


def backfill_documents() -> dict:
    docs_dir = REPO / "memory_store" / "documents"
    if not docs_dir.exists():
        return {"files": 0, "facts": 0, "stmts": 0}

    files = facts_total = stmts_total = 0

    for doc_path in sorted(docs_dir.rglob("*.json")):
        try:
            data = json.loads(doc_path.read_text())
        except Exception:
            continue

        ticker = data.get("symbol", doc_path.parent.name).upper()
        raw_facts = data.get("facts", data.get("output", {}).get("facts", []))
        if not raw_facts:
            continue

        files += 1
        numeric = [f for f in raw_facts if f.get("raw", {}).get("value") is not None]
        stmts   = [f for f in raw_facts if f.get("raw", {}).get("speaker")]

        if numeric:
            facts_to_graph(ticker, numeric)
            facts_total += len(numeric)
        for s in stmts:
            statement_to_graph(ticker, s)
            stmts_total += 1

        print(f"  {doc_path.name}: {len(numeric)} facts, {len(stmts)} statements")

    return {"files": files, "facts": facts_total, "stmts": stmts_total}


def main():
    task_id = log_task("backfill_graph", "Backfill knowledge graph from existing stored data")
    print("\n[BACKFILL] Loading existing data into knowledge graph...")

    xbrl = backfill_xbrl()
    print(f"\n  XBRL: {xbrl['tickers']} tickers, {xbrl['quarters']} quarters, {xbrl['nodes']} nodes")

    docs = backfill_documents()
    print(f"  Docs: {docs['files']} files, {docs['facts']} facts, {docs['stmts']} statements")

    gs = get_store()
    stats = gs.stats()
    print(f"\n  Graph now: {stats['nodes']} nodes, {stats['edges']} edges, {stats['episodes']} episodes")
    for t, n in sorted(stats["by_type"].items()):
        print(f"    {t}: {n}")

    log_checkpoint(task_id,
        status="completed",
        progress=f"xbrl={xbrl['quarters']} quarters, docs={docs['facts']} facts; graph={stats['nodes']}n/{stats['edges']}e",
    )
    update_task_status(task_id, "completed")
    return stats


if __name__ == "__main__":
    main()
