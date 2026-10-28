#!/usr/bin/env python3
"""
Weekly deep research job — runs every Sunday.
Does full research on tracked universe, updates PROGRESS.md, commits to GitHub.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_NSELIB_PATH = os.path.expanduser("~/irtteja/nselib")
if _NSELIB_PATH not in sys.path:
    sys.path.insert(0, _NSELIB_PATH)

import subprocess
from datetime import date, datetime

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _load_tracked_universe():
    """Load full Nifty Total Market universe from data/tracked_universe.json."""
    universe_path = os.path.join(REPO_DIR, "data", "tracked_universe.json")
    try:
        import json as _json
        with open(universe_path) as f:
            u = _json.load(f)
        return [
            {"symbol": s["symbol"], "name": s.get("company", s["symbol"]), "cap_tier": s.get("cap_tier", ""), "sector": s.get("sector", "")}
            for s in u.get("universe", [])
        ]
    except Exception as e:
        print(f"[warn] Could not load universe: {e}. Falling back to defaults.")
        return [
            {"symbol": "RELIANCE", "name": "Reliance Industries"},
            {"symbol": "TCS",      "name": "TCS"},
            {"symbol": "HDFCBANK", "name": "HDFC Bank"},
            {"symbol": "INFY",     "name": "Infosys"},
            {"symbol": "ICICIBANK","name": "ICICI Bank"},
        ]

TRACKED = _load_tracked_universe()

INDICES_TO_WATCH = [
    "NIFTY 50", "NIFTY BANK", "NIFTY IT", "NIFTY MIDCAP 150", "NIFTY SMALLCAP 250"
]


def _git_commit(message: str):
    try:
        subprocess.run(["git", "add", "-A"], cwd=REPO_DIR, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=REPO_DIR, check=True)
        subprocess.run(["git", "push", "-u", "origin", "master"], cwd=REPO_DIR, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[git] {e}")


def run():
    from memory.research_store import init_db
    from memory.task_log import init_task_tables, log_task, update_task_status, log_checkpoint
    from tools.macro import get_full_macro_snapshot
    from tools.announcements import get_upcoming_results_calendar

    init_db()
    init_task_tables()

    task_id = f"weekly-research-{date.today().isoformat()}"
    log_task(task_id, f"Weekly research {date.today().isoformat()}",
             f"Deep research on {len(TRACKED)} tracked stocks + index review")
    update_task_status(task_id, "in_progress")

    print(f"\n{'='*60}")
    print(f"Weekly Research — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    completed = []
    failed = []

    # Index snapshots
    print("[Indices] Fetching index data...")
    from tools.indian_markets import nse_index_data
    for idx in INDICES_TO_WATCH:
        try:
            data = nse_index_data(idx)
            print(f"  {idx}: {data.get('quote', {})}")
            completed.append(f"index:{idx}")
        except Exception as e:
            failed.append(f"index:{idx}: {e}")

    # Stock research
    print("\n[Stocks] Running research on tracked universe...")
    from tools.indian_markets import nse_equity_quote, nse_financial_results, nse_shareholding
    from tools.technical import run_technical_analysis

    out_dir = os.path.join(REPO_DIR, "reports", "weekly")
    os.makedirs(out_dir, exist_ok=True)

    import json
    for stock in TRACKED:
        sym = stock["symbol"]
        print(f"\n  Researching {stock['name']} ({sym})...")
        stock_data = {"symbol": sym, "date": date.today().isoformat()}
        try:
            stock_data["quote"] = nse_equity_quote(sym)
        except Exception as e:
            stock_data["quote_error"] = str(e)
        try:
            stock_data["financial_results"] = nse_financial_results(sym)
        except Exception as e:
            stock_data["results_error"] = str(e)
        try:
            stock_data["shareholding"] = nse_shareholding(sym)
        except Exception as e:
            stock_data["shareholding_error"] = str(e)
        try:
            stock_data["technicals"] = run_technical_analysis(f"{sym}.NS")
        except Exception as e:
            stock_data["technicals_error"] = str(e)

        fpath = os.path.join(out_dir, f"{date.today().isoformat()}-{sym}.json")
        with open(fpath, "w") as f:
            json.dump(stock_data, f, indent=2, default=str)
        completed.append(sym)
        print(f"    saved to {fpath}")

    # Results calendar
    print("\n[Calendar] Upcoming results...")
    try:
        cal = get_upcoming_results_calendar()
        cal_path = os.path.join(out_dir, f"{date.today().isoformat()}-results-calendar.json")
        with open(cal_path, "w") as f:
            json.dump(cal, f, indent=2, default=str)
        completed.append("results_calendar")
    except Exception as e:
        failed.append(f"results_calendar: {e}")

    # Update PROGRESS.md
    progress_path = os.path.join(REPO_DIR, "PROGRESS.md")
    with open(progress_path, "a") as f:
        f.write(f"\n## {date.today().isoformat()} — Weekly Research\n")
        f.write(f"- Researched: {', '.join(stock['symbol'] for stock in TRACKED)}\n")
        f.write(f"- Indices tracked: {', '.join(INDICES_TO_WATCH)}\n")
        if failed:
            f.write(f"- Failures: {', '.join(failed)}\n")
        f.write(f"- Reports saved to reports/weekly/\n")

    log_checkpoint(task_id,
        status="completed" if not failed else "completed_with_errors",
        progress=f"Researched {len(completed)} items",
        next_steps="Review weekly reports, identify stocks for deeper research",
        blockers="; ".join(failed) if failed else "")
    update_task_status(task_id, "completed")

    _git_commit(f"chore: weekly research {date.today().isoformat()} — {len(TRACKED)} stocks")
    print(f"\nDone. {len(completed)} completed, {len(failed)} failed.")


if __name__ == "__main__":
    run()
