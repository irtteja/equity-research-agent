"""
PriceAgent runner — fetches live daily prices and writes PriceSnapshot nodes.

Pulls live quotes for all tracked NSE stocks via nselib (Nifty 500 bulk +
per-symbol fallback). Flags value signals: stocks with improving fundamentals
trading near their 52-week low.

Run:  python jobs/price_agent_runner.py
      python jobs/price_agent_runner.py --date 2026-11-03

Scheduled: run after market close (after alert_agent_runner).
"""
import sys
import argparse
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.price_agent import PriceAgent
from memory.task_log import log_task, update_task_status, log_checkpoint

TASK_ID = "price_agent_daily"


def main():
    parser = argparse.ArgumentParser(description="Fetch live prices and write to graph")
    parser.add_argument("--date", default=None, help="Date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    as_of   = date.fromisoformat(args.date) if args.date else date.today()
    task_id = log_task(TASK_ID, f"PriceAgent daily snapshot for {as_of}")
    update_task_status(task_id, "in_progress")

    try:
        agent  = PriceAgent()
        result = agent.run(as_of=as_of)

        log_checkpoint(task_id,
            status="completed",
            progress=(
                f"prices={result['prices_written']} "
                f"value_signals={result['value_signals']}"
            ),
        )
        update_task_status(task_id, "completed")
        print(f"[price_agent] done: {result}")

    except Exception as exc:
        log_checkpoint(task_id, status="failed",
            progress="PriceAgent failed", blockers=str(exc))
        update_task_status(task_id, "failed")
        raise


if __name__ == "__main__":
    main()
