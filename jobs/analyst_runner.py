"""
analyst_runner — parallel analyst synthesis for all signal tickers.

Consumes the analyst_queue work queue using ThreadPoolExecutor (4 threads).
Each work item is a ticker; AnalystAgent.process_single() runs the full
heuristic synthesis and writes an analyst_synthesis Episode.

Usage:
  python jobs/analyst_runner.py                 # run queue until empty
  python jobs/analyst_runner.py --enqueue       # enqueue from signals then run
  python jobs/analyst_runner.py --limit 20      # max items this run
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

QUEUE      = "analyst_queue"
N_THREADS  = 4
BATCH_SIZE = 50


def run(limit: int = 500) -> dict:
    """Drain analyst_queue up to `limit` items. Returns summary."""
    from agents.analyst_agent import AnalystAgent
    from memory import work_queue as wq

    agent     = AnalystAgent()
    worker_id = f"analyst_runner_{datetime.now().strftime('%H%M%S')}"

    processed = errors = 0
    start = time.time()

    while processed + errors < limit:
        # Recover stale claims
        wq.reset_claimed(QUEUE)

        items = wq.claim_batch(QUEUE, worker_id, BATCH_SIZE)
        if not items:
            break

        def _work(item):
            key = item["item_key"]
            try:
                result = agent.process_single(key)
                wq.mark_done(QUEUE, key)
                return True, key, result
            except Exception as exc:
                wq.mark_error(QUEUE, key, str(exc)[:300])
                return False, key, str(exc)

        with ThreadPoolExecutor(max_workers=N_THREADS) as pool:
            futures = {pool.submit(_work, item): item for item in items}
            for future in as_completed(futures):
                ok, key, detail = future.result()
                if ok:
                    processed += 1
                    rec = detail.get("recommendation", "?") if isinstance(detail, dict) else "?"
                    print(f"  ✓ {key} → {rec}")
                else:
                    errors += 1
                    print(f"  ✗ {key}: {detail}")

    elapsed = round(time.time() - start, 1)
    depth = wq.queue_depth(QUEUE)
    summary = {
        "processed": processed,
        "errors": errors,
        "elapsed_s": elapsed,
        "queue_remaining": depth["pending"],
    }
    print(f"\nanalyst_runner: processed={processed} errors={errors} "
          f"elapsed={elapsed}s remaining={depth['pending']}")
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Run analyst synthesis queue")
    p.add_argument("--enqueue", action="store_true",
                   help="Enqueue tickers with signals before running")
    p.add_argument("--limit",   type=int, default=500,
                   help="Max items to process this run")
    args = p.parse_args()

    if args.enqueue:
        from jobs.swarm_runner import SwarmRunner
        n = SwarmRunner().enqueue_analyst_work()
        print(f"Enqueued {n} tickers")

    result = run(limit=args.limit)
