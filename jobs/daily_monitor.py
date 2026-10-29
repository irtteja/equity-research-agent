#!/usr/bin/env python3
"""
Daily monitoring job — runs at market close.
Captures macro snapshot, FII/DII flows, NSE announcements + PDF ingestion, block/bulk deals.
Saves everything to memory_store/ (GitHub-backed JSONL), logs checkpoint, commits to GitHub.
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
from tools.announcements import get_upcoming_results_calendar
from memory.research_store import init_db, save_insight
from memory.task_log import init_task_tables, log_task, update_task_status, log_checkpoint, save_macro_snapshot

TASK_ID = f"daily-monitor-{date.today().isoformat()}"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _self_update():
    """Pull latest code from GitHub before running."""
    try:
        result = subprocess.run(
            ["git", "-C", REPO_DIR, "pull", "origin", "master"],
            capture_output=True, text=True, timeout=30
        )
        print(f"[git] {result.stdout.strip() or 'Already up to date.'}")
    except Exception as e:
        print(f"[git] pull failed (continuing anyway): {e}")


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
    _self_update()

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

    # 3. NSE announcement ingestion for tracked universe
    print("\n[3/5] Ingesting NSE announcements for tracked universe...")
    try:
        from memory.ingestion import ingest_company
        import anthropic

        # Load tracked symbols
        universe_path = os.path.join(REPO_DIR, "data", "tracked_universe.json")
        with open(universe_path) as f:
            universe_data = json.load(f)
        symbols = [s["symbol"] for s in universe_data.get("universe", [])]

        client = anthropic.Anthropic()
        ingestion_summary = {"processed": 0, "facts_saved": 0, "errors": 0}

        for symbol in symbols:
            try:
                result = ingest_company(symbol, days_back=1, anthropic_client=client)
                ingestion_summary["processed"] += 1
                ingestion_summary["facts_saved"] += result.get("facts_saved", 0)
                if result.get("facts_saved", 0) > 0:
                    print(f"  {symbol}: {result['facts_saved']} facts from {result.get('announcements_processed',0)} announcements")
            except Exception as e:
                ingestion_summary["errors"] += 1
                print(f"  {symbol}: ERROR {e}")

        results["ingestion"] = ingestion_summary
        print(f"  Processed {ingestion_summary['processed']} companies, "
              f"{ingestion_summary['facts_saved']} facts saved, "
              f"{ingestion_summary['errors']} errors")
    except Exception as e:
        print(f"  ERROR: {e}")
        results["ingestion"] = {"error": str(e)}

    # 5. Block/Bulk deals
    print("\n[5/5] Fetching block/bulk deals...")
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
    facts_total = results.get("ingestion", {}).get("facts_saved", 0)
    log_checkpoint(
        TASK_ID,
        status=status,
        progress=f"Macro, FII/DII, {facts_total} facts ingested, block/bulk deals",
        next_steps="Review material announcements, update PROGRESS.md",
        blockers=", ".join(errors) if errors else "",
    )
    update_task_status(TASK_ID, "completed")

    # Commit to GitHub
    _git_commit(f"chore: daily monitor {date.today().isoformat()} — {facts_total} facts ingested")

    print(f"\nDone. Status: {status}")


if __name__ == "__main__":
    run()
