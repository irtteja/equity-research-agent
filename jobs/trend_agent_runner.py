"""
Parallel TrendAgent runner — computes trend summaries with 4 worker threads.

Each worker handles a slice of (ticker, metric) pairs independently.
Run after context_agent_runner (needs PRECEDED_BY edges to exist).

Run:
    python jobs/trend_agent_runner.py
    python jobs/trend_agent_runner.py --workers 4
    python jobs/trend_agent_runner.py --limit 2000  # max pairs to process
"""
import sys
import argparse
import threading
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.trend_agent import TrendAgent
from memory.work_queue import (
    enqueue_many, claim_batch, mark_done, mark_error,
    reset_claimed, queue_depth, clear_queue,
)
from memory.task_log import log_task, update_task_status, log_checkpoint
from memory.message_bus import publish

TASK_ID     = "trend_agent_nightly"
QUEUE       = "trend"
_print_lock = threading.Lock()


def _log(msg: str):
    with _print_lock:
        print(msg)


def enqueue_pairs(limit: int = 0) -> int:
    """Enqueue all distinct (ticker, metric) pairs that have enriched MetricValue nodes."""
    db = Path(__file__).parent.parent / "memory_store" / "graph.db"
    conn = sqlite3.connect(str(db), timeout=10)
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT DISTINCT ticker, json_extract(properties,'$.metric') as metric "
        "FROM nodes WHERE node_type='MetricValue' AND enriched=1 "
        "AND ticker != '' AND json_extract(properties,'$.metric') != ''"
    )
    if limit:
        sql += f" LIMIT {limit}"
    rows = conn.execute(sql).fetchall()
    conn.close()

    items = [
        {"key": f"{r['ticker']}::{r['metric']}", "payload": {"ticker": r["ticker"], "metric": r["metric"]}}
        for r in rows
    ]
    return enqueue_many(QUEUE, items)


def worker_loop(worker_id: str, batch_size: int) -> dict:
    agent = TrendAgent()
    total_written = total_strong = total_errors = 0

    while True:
        reset_claimed(QUEUE, older_than_seconds=120)
        batch = claim_batch(QUEUE, worker_id, batch_size)
        if not batch:
            break

        for item in batch:
            payload = {}
            try:
                import json
                payload = json.loads(item.get("payload", "{}"))
            except Exception:
                pass
            ticker = payload.get("ticker") or item["item_key"].split("::")[0]
            metric = payload.get("metric") or item["item_key"].split("::")[-1]
            try:
                result = agent.process_pair(ticker, metric)
                mark_done(QUEUE, item["item_key"])
                total_written += result.get("written", 0)
                total_strong  += result.get("strong", 0)
            except Exception as e:
                mark_error(QUEUE, item["item_key"], str(e))
                total_errors += 1

    return {"written": total_written, "strong": total_strong, "errors": total_errors}


def main():
    parser = argparse.ArgumentParser(description="Parallel TrendAgent runner")
    parser.add_argument("--workers", type=int, default=4,   help="Thread count (default 4)")
    parser.add_argument("--batch",   type=int, default=50,  help="Items per claim (default 50)")
    parser.add_argument("--limit",   type=int, default=0,   help="Max pairs to process (0=all)")
    parser.add_argument("--reset",   action="store_true",   help="Clear queue and re-enqueue all pairs")
    args = parser.parse_args()

    if args.reset:
        clear_queue(QUEUE)
        print(f"[trend_agent] Work queue '{QUEUE}' cleared.")

    added = enqueue_pairs(limit=args.limit)
    depth = queue_depth(QUEUE)
    total_pending = depth["pending"] + depth["claimed"]
    print(f"[trend_agent] Enqueued {added} pairs — total pending: {total_pending}")

    if total_pending == 0:
        print("[trend_agent] Nothing to do.")
        return

    task_id = log_task(TASK_ID, f"TrendAgent parallel ({args.workers} workers, {total_pending} pairs)")
    update_task_status(task_id, "in_progress")

    print(f"[trend_agent] Starting {args.workers} workers…")
    totals = {"written": 0, "strong": 0, "errors": 0}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(worker_loop, f"trend-worker-{i}", args.batch): i
            for i in range(args.workers)
        }
        for fut in as_completed(futures):
            r = fut.result()
            totals["written"] += r["written"]
            totals["strong"]  += r["strong"]
            totals["errors"]  += r["errors"]

    final = queue_depth(QUEUE)
    print(
        f"[trend_agent] Done — "
        f"trends_written={totals['written']} strong={totals['strong']} "
        f"errors={totals['errors']} remaining={final['pending']}"
    )

    # Show top strong trends
    try:
        agent = TrendAgent()
        strong = agent.strong_trends()
        if strong:
            print("Strong trends:")
            for t in strong[:10]:
                icon = "▲" if t["direction"] == "improving" else "▼"
                print(f"  {icon} {t['ticker']} {t['metric']}: {t['streak']}p "
                      f"Δ{t.get('delta_pct', 0):+.1f}% momentum={t.get('momentum', 0):+.2f}")
    except Exception:
        pass

    log_checkpoint(task_id,
        status="completed",
        progress=(
            f"trends_written={totals['written']} strong={totals['strong']} "
            f"workers={args.workers} errors={totals['errors']}"
        ),
    )
    update_task_status(task_id, "completed")

    publish("trend_agent", "agent_health", {
        "status":          "completed",
        "trends_written":  totals["written"],
        "strong_trends":   totals["strong"],
        "workers":         args.workers,
    }, ttl_hours=30)


if __name__ == "__main__":
    main()
