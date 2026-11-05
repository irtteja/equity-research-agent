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
from memory.message_bus import publish

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
        result = agent.run(hours_back=24)

        alerts = result.get("alerts", [])
        critical = [a for a in alerts if a.get("severity") == "critical"]
        log_checkpoint(task_id,
            status="completed",
            progress=(
                f"alerts={len(alerts)} "
                f"critical={len(critical)} "
                f"as_of={as_of}"
            ),
        )
        update_task_status(task_id, "completed")
        publish("alert_agent", "agent_health", {
            "status": "completed",
            "alerts": len(alerts),
            "critical": len(critical),
            "as_of": str(as_of),
        }, ttl_hours=30)
        for a in alerts[:10]:
            print(f"  [{a.get('severity','?')}] {a.get('ticker','?')}: {a.get('message','')[:80]}")

    except Exception as exc:
        log_checkpoint(task_id, status="failed",
            progress="AlertAgent failed", blockers=str(exc))
        update_task_status(task_id, "failed")
        publish("alert_agent", "gap_detected", {
            "error": str(exc), "agent": "alert_agent",
        }, recipient="orchestrator", priority="high")
        raise


if __name__ == "__main__":
    main()
