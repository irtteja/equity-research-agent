"""
AlertAgent runner — generates the daily market intelligence brief.

Reads TrendSummary nodes from the knowledge graph, indices bhavcopy,
and recent corporate announcements. Prints and saves a plain-language brief.

Run:  python jobs/alert_agent_runner.py
      python jobs/alert_agent_runner.py --date 2026-11-03

Scheduled: after trend_agent_runner (which runs after context_agent_runner).
"""
import sys
import argparse
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.alert_agent import AlertAgent
from memory.task_log import log_task, update_task_status, log_checkpoint

TASK_ID = "alert_agent_daily"


def main():
    parser = argparse.ArgumentParser(description="Generate daily market intelligence brief")
    parser.add_argument("--date", default=None, help="Date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.date) if args.date else date.today()

    task_id = log_task(TASK_ID, f"AlertAgent daily brief for {as_of}")
    update_task_status(task_id, "in_progress")

    try:
        agent = AlertAgent()
        brief = agent.print_brief(as_of=as_of)

        s = brief.get("summary", {})
        log_checkpoint(task_id,
            status="completed",
            progress=(
                f"trends={s.get('total_trends',0)} "
                f"strong+={s.get('strong_positive',0)} "
                f"strong-={s.get('strong_negative',0)} "
                f"inflections={s.get('inflections',0)}"
            ),
        )
        update_task_status(task_id, "completed")

    except Exception as exc:
        log_checkpoint(task_id, status="failed",
            progress="AlertAgent failed", blockers=str(exc))
        update_task_status(task_id, "failed")
        raise


if __name__ == "__main__":
    main()
