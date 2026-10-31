"""
Scheduled runner for ContextAgent — enriches MetricValue nodes nightly.

Reads unenriched nodes from the graph, adds PRECEDED_BY edges (delta between
consecutive periods) and CORROBORATES/CONTRADICTS edges from management
statements.

Run: python jobs/context_agent_runner.py
Scheduled: every night after ingestion + XBRL results fetch are done.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.context_agent import ContextAgent
from memory.task_log import log_task, update_task_status, log_checkpoint

TASK_ID = "context_agent_nightly"


def main():
    task_id = log_task(TASK_ID, "ContextAgent nightly enrichment pass")

    try:
        agent = ContextAgent()
        result = agent.process_pending(limit=200)

        log_checkpoint(task_id,
            status="completed",
            progress=(
                f"processed={result['processed']} edges_created={result['edges_created']} "
                f"skipped={result['skipped']}"
            ),
        )
        update_task_status(task_id, "completed")
        print(f"[context_agent] {result}")

    except Exception as exc:
        log_checkpoint(task_id, status="failed",
            progress="context agent enrichment failed",
            blockers=str(exc))
        update_task_status(task_id, "failed")
        raise


if __name__ == "__main__":
    main()
