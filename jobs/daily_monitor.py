#!/usr/bin/env python3
"""
Daily monitoring job — runs at market close.
Captures macro snapshot, FII/DII flows, BSE announcements, block/bulk deals.
Saves everything to memory DB, logs checkpoint, commits to GitHub.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_NSELIB_PATH = os.path.expanduser("~/irtteja/nselib")
if _NSELIB_PATH not in sys.path:
    sys.path.insert(0, _NSELIB_PATH)

import json
import subprocess
from datetime import date, datetime

from tools.macro import get_full_macro_snapshot, get_fii_dii_flows, get_block_bulk_deals
from tools.announcements import get_all_bse_announcements_today, get_upcoming_results_calendar
from memory.research_store import init_db, save_insight
from memory.task_log import init_task_tables, log_task, update_task_status, log_checkpoint, save_macro_snapshot

TASK_ID = f"daily-monitor-{date.today().isoformat()}"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git_commit(message: str):
    try:
        subprocess.run(["git", "add", "-A"], cwd=REPO_DIR, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=REPO_DIR, check=True)
        subprocess.run(["git", "push", "-u", "origin", "master"], cwd=REPO_DIR, check=True)
        print(f"[git] committed and pushed: {message}")
    except subprocess.CalledProcessError as e:
        print(f"[git] push failed: {e}")


def _summarise_macro(snap: dict) -> str:
    lines = [f"Macro snapshot {snap.get('date', '')}"]
    for section in ["global_indices", "commodities", "currencies"]:
        data = snap.get(section, {})
        movers = []
        for name, v in data.items():
            pct = v.get("change_pct")
            if pct is not None:
                movers.append((name, pct, v.get("last")))
        movers.sort(key=lambda x: abs(x[1] or 0), reverse=True)
        lines.append(f"\n{section.replace('_', ' ').title()}:")
        for name, pct, last in movers[:5]:
            arrow = "▲" if pct > 0 else "▼"
            lines.append(f"  {name}: {last} ({arrow}{abs(pct):.2f}%)")
    return "\n".join(lines)


def run():
    init_db()
    init_task_tables()

    print(f"\n{'='*60}")
    print(f"Daily Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    print(f"{'='*60}\n")

    log_task(TASK_ID, f"Daily market monitoring {date.today().isoformat()}",
             "Macro snapshot, FII/DII, announcements, block deals", priority="high")
    update_task_status(TASK_ID, "in_progress")

    results = {}

    # 1. Macro snapshot
    print("[1/4] Fetching macro snapshot...")
    try:
        snap = get_full_macro_snapshot()
        results["macro"] = snap
        summary = _summarise_macro(snap)
        save_macro_snapshot(date.today().isoformat(), snap, summary)
        print(summary)
    except Exception as e:
        print(f"  ERROR: {e}")
        results["macro"] = {"error": str(e)}

    # 2. FII/DII flows
    print("\n[2/4] Fetching FII/DII flows...")
    try:
        fii = get_fii_dii_flows()
        results["fii_dii"] = fii
        print(json.dumps(fii, indent=2, default=str)[:800])
    except Exception as e:
        print(f"  ERROR: {e}")
        results["fii_dii"] = {"error": str(e)}

    # 3. BSE announcements
    print("\n[3/4] Fetching BSE announcements...")
    try:
        announces = get_all_bse_announcements_today()
        results["announcements"] = announces
        count = announces.get("total", 0)
        print(f"  Found {count} announcements today")
        for a in announces.get("announcements", [])[:10]:
            print(f"  [{a.get('category','')}] {a.get('company','')} — {a.get('headline','')[:80]}")
    except Exception as e:
        print(f"  ERROR: {e}")
        results["announcements"] = {"error": str(e)}

    # 4. Block/Bulk deals
    print("\n[4/4] Fetching block/bulk deals...")
    try:
        deals = get_block_bulk_deals()
        results["deals"] = deals
        print(json.dumps(deals, indent=2, default=str)[:600])
    except Exception as e:
        print(f"  ERROR: {e}")
        results["deals"] = {"error": str(e)}

    # Save output
    out_dir = os.path.join(REPO_DIR, "reports", "daily")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f"{date.today().isoformat()}.json")
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n[output] saved to {out_file}")

    # Checkpoint
    errors = [k for k, v in results.items() if isinstance(v, dict) and "error" in v]
    status = "completed" if not errors else "completed_with_errors"
    log_checkpoint(
        TASK_ID,
        status=status,
        progress=f"Macro, FII/DII, {results.get('announcements', {}).get('total', 0)} announcements, block/bulk deals",
        next_steps="Review material announcements, update PROGRESS.md",
        blockers=", ".join(errors) if errors else "",
    )
    update_task_status(TASK_ID, "completed")

    # Commit to GitHub
    _git_commit(f"chore: daily monitor {date.today().isoformat()} — {results.get('announcements', {}).get('total', 0)} announcements")

    print(f"\nDone. Status: {status}")


if __name__ == "__main__":
    run()
