"""
SwarmRunner — dynamic swarm dispatcher.

Reads agent_configs from agent_registry (DB records, not hard-coded classes).
Dispatches work to the right agent class using AGENT_CLASS_MAP.
Uses ThreadPoolExecutor for in-process concurrency.

Usage:
  from jobs.swarm_runner import SwarmRunner
  runner = SwarmRunner()
  runner.run_all_queues()          # dispatch all enabled agents
  runner.run_queue("analyst_queue")  # run one queue
  runner.enqueue_analyst_work()    # populate analyst_queue from signals
"""
import importlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).parent.parent

log = logging.getLogger("swarm_runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


# Maps agent_type (registry field) → Python class path
AGENT_CLASS_MAP = {
    "context":  "agents.context_agent.ContextAgent",
    "trend":    "agents.trend_agent.TrendAgent",
    "document": "agents.document_agent.DocumentAgent",
    "analyst":  "agents.analyst_agent.AnalystAgent",
    "price":    "agents.price_agent.PriceAgent",
    "alert":    "agents.alert_agent.AlertAgent",
}


def _load_agent_class(dotted_path: str):
    """Dynamically import and return the agent class at dotted_path."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


class SwarmRunner:
    """
    Reads agent_configs, dynamically dispatches work_queue items to agents.
    """

    def __init__(self):
        import sys
        sys.path.insert(0, str(REPO))
        from memory.agent_registry import list_agents
        from memory import work_queue as wq
        self._list_agents = list_agents
        self.wq = wq

    # ── Queue dispatch ────────────────────────────────────────────────────────

    def run_queue(self, queue_name: str, batch_size: int = 50) -> dict:
        """
        Claim a batch from queue_name, dispatch each item to the right agent.
        Respects max_concurrency from the registry config for that queue.
        Returns {processed, errors, skipped}.
        """
        from memory.agent_registry import get_queue_agents
        agents = get_queue_agents(queue_name)
        if not agents:
            return {"processed": 0, "errors": 0, "skipped": 0, "reason": f"no agents for queue {queue_name!r}"}

        # Use the first (highest priority) enabled agent for this queue
        cfg = agents[0]
        agent_type = cfg["agent_type"]
        max_threads = cfg.get("max_concurrency", 4)
        worker_id = f"swarm_{queue_name}_{datetime.now().strftime('%H%M%S')}"

        # Recover stale claims first
        try:
            self.wq.reset_claimed(queue_name)
        except Exception:
            pass

        items = self.wq.claim_batch(queue_name, worker_id, batch_size)
        if not items:
            return {"processed": 0, "errors": 0, "skipped": 0, "reason": "queue empty"}

        # Load agent class
        class_path = AGENT_CLASS_MAP.get(agent_type)
        if not class_path:
            for item in items:
                self.wq.mark_error(queue_name, item["item_key"], f"unknown agent_type={agent_type}")
            return {"processed": 0, "errors": len(items), "skipped": 0}

        try:
            AgentClass = _load_agent_class(class_path)
        except Exception as exc:
            for item in items:
                self.wq.mark_error(queue_name, item["item_key"], str(exc))
            return {"processed": 0, "errors": len(items), "skipped": 0, "reason": str(exc)}

        # Instantiate once and dispatch in parallel
        agent_inst = AgentClass()
        processed = errors = 0

        def _dispatch(item):
            key = item["item_key"]
            try:
                payload = json.loads(item.get("payload", "{}"))
                result = agent_inst.process_single(key)
                self.wq.mark_done(queue_name, key)
                return True, key
            except Exception as exc:
                self.wq.mark_error(queue_name, key, str(exc)[:300])
                return False, key

        with ThreadPoolExecutor(max_workers=min(max_threads, len(items))) as pool:
            futures = {pool.submit(_dispatch, item): item for item in items}
            for future in as_completed(futures):
                ok, key = future.result()
                if ok:
                    processed += 1
                else:
                    errors += 1

        log.info(f"[{queue_name}] agent={agent_type} processed={processed} errors={errors}")
        return {"processed": processed, "errors": errors, "skipped": 0}

    def run_all_queues(self) -> dict:
        """
        Run all enabled agents' queues in parallel (one thread per distinct queue).
        Returns summary dict keyed by queue_name.
        """
        agents = self._list_agents(enabled_only=True)
        queues = list({a["queue_name"] for a in agents if a.get("queue_name")})

        if not queues:
            return {}

        summary = {}
        with ThreadPoolExecutor(max_workers=min(4, len(queues))) as pool:
            futures = {pool.submit(self.run_queue, q): q for q in queues}
            for future in as_completed(futures):
                q = futures[future]
                try:
                    summary[q] = future.result()
                except Exception as exc:
                    summary[q] = {"error": str(exc)}

        return summary

    # ── Analyst work enqueue ──────────────────────────────────────────────────

    def enqueue_analyst_work(self, tickers: list[str] = None, limit: int = 100) -> int:
        """
        Enqueue analyst_queue work items for tickers with ConfluenceSignal nodes.
        If tickers not provided, reads from graph. Returns count enqueued.
        """
        if tickers is None:
            tickers = self._tickers_with_signals(limit)

        items = [
            {"key": ticker, "payload": {"ticker": ticker}, "priority": 5}
            for ticker in tickers[:limit]
        ]
        inserted = self.wq.enqueue_many("analyst_queue", items)
        log.info(f"enqueue_analyst_work: {inserted}/{len(items)} tickers enqueued")
        return inserted

    def _tickers_with_signals(self, limit: int = 100) -> list[str]:
        """Fetch tickers with ConfluenceSignal nodes from graph.db."""
        import sqlite3
        db = REPO / "memory_store" / "graph.db"
        if not db.exists():
            return []
        with sqlite3.connect(str(db), timeout=5) as conn:
            rows = conn.execute(
                "SELECT ticker FROM nodes WHERE node_type='ConfluenceSignal' "
                "AND ticker IS NOT NULL AND ticker != '' "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r[0] for r in rows]


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))

    p = argparse.ArgumentParser(description="Run SwarmRunner dispatch")
    p.add_argument("--queue",     help="Run one specific queue")
    p.add_argument("--all",       action="store_true", help="Run all queues")
    p.add_argument("--enqueue-analyst", action="store_true", help="Enqueue analyst work from signals")
    args = p.parse_args()

    runner = SwarmRunner()

    if args.enqueue_analyst:
        n = runner.enqueue_analyst_work()
        print(f"Enqueued {n} analyst work items")
    elif args.queue:
        result = runner.run_queue(args.queue)
        print(json.dumps(result, indent=2))
    elif args.all:
        summary = runner.run_all_queues()
        print(json.dumps(summary, indent=2))
    else:
        p.print_help()
