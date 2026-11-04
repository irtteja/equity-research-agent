"""
Parallel DocumentAgent runner — extracts management statements with 4 worker threads.

Each worker handles one ticker's documents. Progress is persisted to
memory_store/document_agent_progress.json — already-processed tickers are skipped.

Run:
    python jobs/document_agent_runner.py
    python jobs/document_agent_runner.py --workers 4
    python jobs/document_agent_runner.py --limit 50   # process up to 50 tickers
    python jobs/document_agent_runner.py --reset      # clear progress, reprocess all

Scheduled: after trend_agent_runner (needs TrendSummary nodes to link against).
"""
import sys
import argparse
import threading
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.document_agent import DocumentAgent
from memory.work_queue import (
    enqueue_many, claim_batch, mark_done, mark_error,
    reset_claimed, queue_depth, clear_queue,
)
from memory.task_log import log_task, update_task_status, log_checkpoint

TASK_ID     = "document_agent_daily"
QUEUE       = "document"
REPO        = Path(__file__).parent.parent
_print_lock = threading.Lock()
_progress_lock = threading.Lock()


def _log(msg: str):
    with _print_lock:
        print(msg)


def _load_progress() -> dict:
    p = REPO / "memory_store" / "document_agent_progress.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"done": {}}


def enqueue_tickers(limit: int = 0) -> int:
    """Enqueue all unprocessed tickers from memory_store/documents/."""
    doc_dir = REPO / "memory_store" / "documents"
    if not doc_dir.exists():
        return 0

    progress = _load_progress()
    done = set(progress.get("done", {}).keys())

    tickers = sorted(d.name for d in doc_dir.iterdir() if d.is_dir())
    pending = [t for t in tickers if t not in done]
    if limit:
        pending = pending[:limit]

    items = [{"key": t, "payload": {}} for t in pending]
    return enqueue_many(QUEUE, items) if items else 0


def worker_loop(worker_id: str, batch_size: int) -> dict:
    agent = DocumentAgent()
    total_docs = total_stmts = total_errors = 0

    while True:
        reset_claimed(QUEUE, older_than_seconds=300)  # doc processing can be slow
        batch = claim_batch(QUEUE, worker_id, batch_size)
        if not batch:
            break

        for item in batch:
            ticker = item["item_key"]
            try:
                result = agent.process_ticker(ticker)
                mark_done(QUEUE, ticker)
                total_docs  += result.get("docs", 0)
                total_stmts += result.get("stmts", 0)
                if result["docs"] or result["stmts"]:
                    _log(f"  [{worker_id}] {ticker}: {result['docs']} docs, {result['stmts']} statements")
            except Exception as e:
                mark_error(QUEUE, ticker, str(e))
                total_errors += 1
                _log(f"  [{worker_id}] ERROR {ticker}: {e}")

    return {"docs": total_docs, "stmts": total_stmts, "errors": total_errors}


def main():
    parser = argparse.ArgumentParser(description="Parallel DocumentAgent runner")
    parser.add_argument("--workers", type=int, default=4,  help="Thread count (default 4)")
    parser.add_argument("--batch",   type=int, default=5,  help="Tickers per claim (default 5)")
    parser.add_argument("--limit",   type=int, default=0,  help="Max tickers to enqueue (0=all)")
    parser.add_argument("--reset",   action="store_true",  help="Clear progress and reprocess all")
    args = parser.parse_args()

    if args.reset:
        clear_queue(QUEUE)
        progress_file = REPO / "memory_store" / "document_agent_progress.json"
        if progress_file.exists():
            progress_file.write_text('{"done": {}}')
        print("[document_agent] Progress reset.")

    added = enqueue_tickers(limit=args.limit)
    depth = queue_depth(QUEUE)
    total_pending = depth["pending"] + depth["claimed"]
    print(f"[document_agent] Enqueued {added} tickers — total pending: {total_pending}")

    if total_pending == 0:
        print("[document_agent] Nothing to do.")
        return

    task_id = log_task(TASK_ID, f"DocumentAgent parallel ({args.workers} workers, {total_pending} tickers)")
    update_task_status(task_id, "in_progress")

    print(f"[document_agent] Starting {args.workers} workers…")
    totals = {"docs": 0, "stmts": 0, "errors": 0}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(worker_loop, f"doc-worker-{i}", args.batch): i
            for i in range(args.workers)
        }
        for fut in as_completed(futures):
            r = fut.result()
            totals["docs"]   += r["docs"]
            totals["stmts"]  += r["stmts"]
            totals["errors"] += r["errors"]

    final = queue_depth(QUEUE)
    print(
        f"[document_agent] Done — "
        f"docs={totals['docs']} statements={totals['stmts']} "
        f"errors={totals['errors']} remaining={final['pending']}"
    )

    # Graph stats
    try:
        from memory.graph_store import get_store
        gs = get_store()
        stats = gs.stats()
        print(f"[document_agent] Graph: {stats['nodes']:,} nodes · {stats['edges']:,} edges")
    except Exception:
        pass

    log_checkpoint(task_id,
        status="completed",
        progress=(
            f"docs={totals['docs']} statements={totals['stmts']} "
            f"workers={args.workers} errors={totals['errors']}"
        ),
    )
    update_task_status(task_id, "completed")


if __name__ == "__main__":
    main()
