"""
Scheduled runner for TrendAgent — computes trend summaries nightly.

Reads enriched MetricValue nodes (PRECEDED_BY edges from ContextAgent),
computes momentum/acceleration/streak for each (ticker, metric) pair,
writes TrendSummary nodes. Run after context_agent_runner.

Run: python jobs/trend_agent_runner.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.trend_agent import TrendAgent
from memory.task_log import log_task, update_task_status, log_checkpoint

TASK_ID = "trend_agent_nightly"


def main():
    task_id = log_task(TASK_ID, "TrendAgent nightly trend computation pass")

    try:
        agent  = TrendAgent()
        result = agent.process_all(min_periods=2)

        strong = agent.strong_trends()
        strong_summary = ", ".join(
            f"{t['ticker']} {t['metric']} ({t['direction']}, {t['streak']}p)"
            for t in strong[:5]
        ) or "none"

        log_checkpoint(task_id,
            status="completed",
            progress=(
                f"pairs={result['pairs_analyzed']} trends={result['trends_written']} "
                f"strong={result['strong_trends']} | top: {strong_summary}"
            ),
        )
        update_task_status(task_id, "completed")
        print(f"[trend_agent] {result}")
        if strong:
            print(f"Strong trends:")
            for t in strong[:10]:
                icon = "▲" if t["direction"] == "improving" else "▼"
                print(f"  {icon} {t['ticker']} {t['metric']}: Δ{t['delta_pct']:+.1f}% "
                      f"{t['streak']}p, momentum={t['momentum']:+.2f} ({t['acceleration']})")

    except Exception as exc:
        log_checkpoint(task_id, status="failed",
            progress="trend agent failed",
            blockers=str(exc))
        update_task_status(task_id, "failed")
        raise


if __name__ == "__main__":
    main()
