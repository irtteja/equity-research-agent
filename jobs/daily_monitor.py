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
    print("[1/5] Fetching macro snapshot...")
    try:
        snap = get_full_macro_snapshot()
        results["macro"] = snap
        summary = _summarise_macro(snap)
        vix = snap.get("india_vix", {})
        if vix and not vix.get("error"):
            summary += f"\n\nINDIA VIX: {vix.get('last')} ({vix.get('change_pct'):+.2f}%)  [52w: {vix.get('year_low')}–{vix.get('year_high')}]"
        save_macro_snapshot(date.today().isoformat(), snap, summary)
        print(summary)
    except Exception as e:
        print(f"  ERROR: {e}")
        results["macro"] = {"error": str(e)}

    # 2. FII/DII flows
    print("\n[2/5] Fetching FII/DII flows...")
    try:
        fii = get_fii_dii_flows()
        results["fii_dii"] = fii
        print(json.dumps(fii, indent=2, default=str)[:800])
    except Exception as e:
        print(f"  ERROR: {e}")
        results["fii_dii"] = {"error": str(e)}

    # 3. NSE announcement ingestion for tracked universe
    # Phase A: parallel fetch. Phase B: prepare items for Claude to analyze in-session.
    # No API key needed — Claude reads the prep output and does extraction directly.
    print("\n[3/5] Fetching + preparing NSE announcements for tracked universe...")
    try:
        from jobs.fetch_announcements import fetch_announcements_parallel, save_announcements

        # Phase A: fast parallel fetch across all 873 symbols
        fetch_result = fetch_announcements_parallel(
            to_date=date.today(),
            days_back=1,
            verbose=True,
        )
        save_announcements(fetch_result)  # persist raw data to memory_store/announcements/

        # Phase B: rule-based triage for all symbols that filed today
        # Returns all items: ready(deep), light, skip, ignore, error
        active_symbols = list(fetch_result["announcements"].keys())
        print(f"  {len(active_symbols)} symbols with filings today — running rule triage...")

        from memory.ingestion import prepare_announcement, escalate_items
        from memory.store import get_profile
        from memory.triage import format_for_claude_review

        all_prep = []
        prep_errors = 0
        for symbol in active_symbols:
            try:
                anns = fetch_result["announcements"][symbol]
                profile = get_profile(symbol)
                for ann in anns:
                    item = prepare_announcement(symbol, ann, profile)
                    all_prep.append(item)
            except Exception as e:
                prep_errors += 1
                print(f"  {symbol}: triage ERROR {e}")

        by_status = {s: [x for x in all_prep if x.get("status") == s] for s in ("ready", "skip", "light", "ignore", "error")}
        # "ready" = deep (PDF downloaded + text extracted), "light" = light triage items
        deep_items  = by_status["ready"]
        light_items = by_status["light"]
        skip_items  = by_status["skip"]

        print(f"  Rule triage: {len(deep_items)} deep, {len(light_items)} light, "
              f"{len(skip_items)} skip, {len(by_status['ignore'])} ignore, {len(by_status['error'])} errors")

        # Phase B.5: Dynamic review — Claude scans ALL skip+light attchmntTexts in-session
        # and escalates anything that looks material to the deep pile.
        # No PDFs downloaded yet for skip/light, so this costs zero extra fetches.
        review_block = format_for_claude_review(skip_items + light_items)
        print(f"\n{review_block}")
        # Claude reads the above block and outputs:
        #   ESCALATE <idx> <idx> ... REASON: <one line>
        # for any items that should be re-triaged as deep.
        # Claude then re-calls prepare_announcement with force_deep=True for those indices.
        # (Escalation is handled in-session; indices reference the combined skip+light list above.)
        _skip_light_combined = skip_items + light_items  # available for in-session escalation

        ready = deep_items  # start with rule-identified deep items

        ingestion_summary = {
            "fetch_errors": fetch_result["errors"],
            "total_announcements": fetch_result["total_announcements"],
            "symbols_active": len(active_symbols),
            "rule_deep": len(deep_items),
            "rule_light": len(light_items),
            "rule_skip": len(skip_items),
            "ready_for_analysis": len(ready),
            "prep_errors": prep_errors,
        }
        results["ingestion"] = ingestion_summary
        results["ingestion"]["prep_items"] = ready

        print(f"\n  {fetch_result['total_announcements']} announcements fetched; "
              f"{len(ready)} deep (rule) + dynamic escalations pending Claude review")

        # Print deep items for Claude to extract in-session
        if ready:
            print(f"\n[CLAUDE_ANALYSIS] {len(ready)} deep items. Analyzing now...\n")
            for item in ready:
                print(f"\n{'='*60}")
                print(f"TICKER: {item['ticker']}  |  STRATEGY: {item.get('strategy','deep').upper()}")
                print(f"DATE: {item['ann_date']}  |  CATEGORY: {item['category']}")
                print(f"attchmntText: {item['attchmnt_text']}")
                print(f"PDF text ({len(item['text'])} chars, {item['pdf_pages']} pages):")
                print(item['text'][:8000])
                print(f"{'='*60}\n")

    except Exception as e:
        print(f"  ERROR: {e}")
        results["ingestion"] = {"error": str(e)}

    # 4. Save bhavcopy (equity + indices)
    print("\n[4/5] Saving NSE bhavcopy (equity + indices) for today...")
    try:
        from jobs.save_bhavcopy import fetch_and_save as save_equity_bhav
        from jobs.save_indices_bhavcopy import fetch_and_save as save_index_bhav
        bhav_result = save_equity_bhav(date.today())
        idx_bhav_result = save_index_bhav(date.today())
        results["bhavcopy"] = bhav_result
        results["indices_bhavcopy"] = idx_bhav_result
        print(f"  equity: {bhav_result}")
        print(f"  indices: {idx_bhav_result}")
    except Exception as e:
        print(f"  ERROR: {e}")
        results["bhavcopy"] = {"error": str(e)}

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
    bhav_rows = results.get("bhavcopy", {}).get("rows", 0)
    log_checkpoint(
        TASK_ID,
        status=status,
        progress=f"Macro, FII/DII, {facts_total} facts ingested, bhavcopy {bhav_rows} rows, block/bulk deals",
        next_steps="Review material announcements, update PROGRESS.md",
        blockers=", ".join(errors) if errors else "",
    )
    update_task_status(TASK_ID, "completed")

    # Commit to GitHub
    _git_commit(f"chore: daily monitor {date.today().isoformat()} — {facts_total} facts ingested")

    print(f"\nDone. Status: {status}")


if __name__ == "__main__":
    run()
