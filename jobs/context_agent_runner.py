"""
Parallel ContextAgent runner — enriches MetricValue nodes with 8 worker threads.

Uses work_queue.py for atomic claim semantics so workers never double-process.
Clears the work queue at start (or on --reset) and re-enqueues all pending nodes.

Run:
    python jobs/context_agent_runner.py           # 8 workers, batch=100
    python jobs/context_agent_runner.py --workers 4
    python jobs/context_agent_runner.py --reset   # clear work queue, reprocess all
    python jobs/context_agent_runner.py --dry-run # show backlog depth only
"""
import sys
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.context_agent import ContextAgent
from memory.work_queue import (
    enqueue_many, claim_batch, mark_done, mark_error,
    reset_claimed, queue_depth, clear_queue,
)
from memory.task_log import log_task, update_task_status, log_checkpoint
from memory.message_bus import publish

TASK_ID   = "context_agent_nightly"
QUEUE     = "context"
BATCH     = 100
_print_lock = threading.Lock()


def _log(msg: str):
    with _print_lock:
        print(msg)


# ── Enqueue ───────────────────────────────────────────────────────────────────

def enqueue_pending() -> int:
    """
    Pull all unenriched MetricValue node ids from graph and enqueue them.
    Returns count of newly inserted items (skips already-queued).
    """
    import sqlite3, json
    from pathlib import Path
    db = Path(__file__).parent.parent / "memory_store" / "graph.db"
    conn = sqlite3.connect(str(db), timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id FROM nodes WHERE node_type='MetricValue' AND enriched=0"
    ).fetchall()
    conn.close()

    items = [{"key": r["id"], "payload": {}} for r in rows]
    if not items:
        return 0
    return enqueue_many(QUEUE, items)


# ── Worker ────────────────────────────────────────────────────────────────────

def worker_loop(worker_id: str, batch_size: int) -> dict:
    """
    One worker thread. Claims batches from the queue until empty.
    Returns {processed, edges, errors}.
    """
    agent = ContextAgent()
    total_processed = total_edges = total_errors = 0

    while True:
        reset_claimed(QUEUE, older_than_seconds=120)  # recover stale items
        batch = claim_batch(QUEUE, worker_id, batch_size)
        if not batch:
            break

        for item in batch:
            node_id = item["item_key"]
            result  = agent.process_single(node_id)
            if result.get("error"):
                mark_error(QUEUE, node_id, result["error"])
                total_errors += 1
            else:
                mark_done(QUEUE, node_id)
                total_processed += result.get("processed", 0)
                total_edges      += result.get("edges_created", 0)

    return {"processed": total_processed, "edges": total_edges, "errors": total_errors}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Parallel ContextAgent enrichment")
    parser.add_argument("--workers",  type=int, default=8,   help="Thread count (default 8)")
    parser.add_argument("--batch",    type=int, default=BATCH, help="Items per claim (default 100)")
    parser.add_argument("--reset",    action="store_true",   help="Clear work queue and re-enqueue all pending")
    parser.add_argument("--dry-run",  action="store_true",   help="Show backlog depth and exit")
    args = parser.parse_args()

    if args.reset:
        clear_queue(QUEUE)
        print(f"[context_agent] Work queue '{QUEUE}' cleared.")

    if args.dry_run:
        depth = queue_depth(QUEUE)
        import sqlite3
        from pathlib import Path
        db = Path(__file__).parent.parent / "memory_store" / "graph.db"
        conn = sqlite3.connect(str(db), timeout=5)
        backlog = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE node_type='MetricValue' AND enriched=0"
        ).fetchone()[0]
        conn.close()
        print(f"[context_agent] Unenriched MetricValue nodes: {backlog:,}")
        print(f"[context_agent] Work queue depth: {depth}")
        return

    # Enqueue unenriched nodes
    added = enqueue_pending()
    depth = queue_depth(QUEUE)
    total_pending = depth["pending"] + depth["claimed"]
    print(f"[context_agent] Enqueued {added} new nodes — total pending: {total_pending}")

    if total_pending == 0:
        print("[context_agent] Nothing to do.")
        return

    # Log task
    task_id = log_task(TASK_ID, f"ContextAgent parallel ({args.workers} workers, {total_pending} nodes)")
    update_task_status(task_id, "in_progress")

    # Launch workers
    print(f"[context_agent] Starting {args.workers} workers…")
    totals = {"processed": 0, "edges": 0, "errors": 0}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(worker_loop, f"worker-{i}", args.batch): i
            for i in range(args.workers)
        }
        for fut in as_completed(futures):
            r = fut.result()
            totals["processed"] += r["processed"]
            totals["edges"]     += r["edges"]
            totals["errors"]    += r["errors"]

    final_depth = queue_depth(QUEUE)
    print(
        f"[context_agent] Done — "
        f"processed={totals['processed']} edges={totals['edges']} "
        f"errors={totals['errors']} remaining={final_depth['pending']}"
    )

    log_checkpoint(task_id,
        status="completed",
        progress=(
            f"processed={totals['processed']} edges={totals['edges']} "
            f"errors={totals['errors']} workers={args.workers}"
        ),
    )
    update_task_status(task_id, "completed")

    publish("context_agent", "agent_health", {
        "status":    "completed",
        "processed": totals["processed"],
        "edges":     totals["edges"],
        "errors":    totals["errors"],
        "workers":   args.workers,
    }, ttl_hours=30)


if __name__ == "__main__":
    main()
