"""
DocumentAgent runner — extracts management statements from concall transcripts
and annual reports, writes ManagementStatement nodes into the knowledge graph.

Reads:  memory_store/documents/<TICKER>/transcript/*.json
        memory_store/documents/<TICKER>/annual_report/*.json
Writes: graph ManagementStatement nodes + CORROBORATES/CONTRADICTS edges

Run:  python jobs/document_agent_runner.py
      python jobs/document_agent_runner.py --limit 50
      python jobs/document_agent_runner.py --reset   (clear progress, reprocess all)

Scheduled: after trend_agent_runner (needs TrendSummary nodes to link against).
"""
import sys
import argparse
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.document_agent import DocumentAgent
from memory.task_log import log_task, update_task_status, log_checkpoint

TASK_ID = "document_agent_daily"
REPO    = Path(__file__).parent.parent


def main():
    parser = argparse.ArgumentParser(description="Extract management statements into knowledge graph")
    parser.add_argument("--limit", type=int, default=0, help="Max tickers to process (0=all)")
    parser.add_argument("--reset", action="store_true", help="Clear progress and reprocess all tickers")
    args = parser.parse_args()

    if args.reset:
        progress_file = REPO / "memory_store" / "document_agent_progress.json"
        if progress_file.exists():
            progress_file.write_text('{"done": {}}')
            print("[document_agent] Progress reset.")

    task_id = log_task(TASK_ID, f"DocumentAgent pass (limit={args.limit or 'all'})")
    update_task_status(task_id, "in_progress")

    try:
        agent  = DocumentAgent()
        result = agent.process_all(limit=args.limit)

        log_checkpoint(task_id,
            status="completed",
            progress=(
                f"tickers={result['tickers']} "
                f"documents={result['documents']} "
                f"statements={result['statements']}"
            ),
        )
        update_task_status(task_id, "completed")

        print(f"[DocumentAgent] tickers={result['tickers']}  "
              f"docs={result['documents']}  statements={result['statements']}")

        # Show graph stats
        from memory.graph_store import get_store
        gs = get_store()
        stats = gs.stats()
        print(f"[DocumentAgent] Graph: {stats['nodes']:,} nodes · {stats['edges']:,} edges")

    except Exception as exc:
        log_checkpoint(task_id, status="failed",
            progress="DocumentAgent failed", blockers=str(exc))
        update_task_status(task_id, "failed")
        raise


if __name__ == "__main__":
    main()
