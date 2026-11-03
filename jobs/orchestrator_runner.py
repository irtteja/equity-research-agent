"""
Orchestrator runner — wires all agents together and runs the swarm cycle.

Run every 30 minutes via scheduled task.
Also run manually: python3 jobs/orchestrator_runner.py

Flow:
  1. Publish my heartbeat to bus
  2. Run OrchestratorAgent (reads inbox, detects confluences, writes nodes)
  3. Print summary
  4. Log checkpoint
"""
import sys
import os
import json
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from memory.message_bus import publish
from memory.task_log import log_task, log_checkpoint, update_task_status
from agents.orchestrator import run as orchestrator_run


def main():
    task_id = log_task("orchestrator_run", f"Orchestrator swarm cycle {datetime.now().date()}")

    publish(
        sender="orchestrator_runner",
        event_type="agent_health",
        payload={"status": "starting", "ts": datetime.now().isoformat()},
        recipient="all",
        ttl_hours=2,
    )

    try:
        result = orchestrator_run()

        conf = result.get("confluences", {})
        gaps = result.get("self_check", {}).get("gaps", [])

        log_checkpoint(task_id, status="completed",
            progress=(
                f"Confluences: {conf.get('total',0)} "
                f"(buy={conf.get('buy',0)}, sell={conf.get('sell',0)}) | "
                f"Gaps: {len(gaps)}"
            ),
            next_steps="Run again in 30min; wire dashboard to live confluence nodes",
            blockers="; ".join(gaps) if gaps else None,
        )
        update_task_status(task_id, "completed")

    except Exception as e:
        log_checkpoint(task_id, status="failed",
            progress="Orchestrator cycle crashed",
            next_steps="Check agents/orchestrator.py",
            blockers=str(e),
        )
        update_task_status(task_id, "failed")
        raise


if __name__ == "__main__":
    main()
