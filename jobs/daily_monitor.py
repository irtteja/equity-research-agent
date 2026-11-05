#!/usr/bin/env python3
"""
Daily monitoring job — runs at market close.
Captures macro snapshot, FII/DII flows, NSE announcements + PDF ingestion, block/bulk deals.
Saves everything to memory_store/ (GitHub-backed JSONL), logs checkpoint, commits to GitHub.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_NSELIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor")
if not os.path.isdir(os.path.join(_NSELIB_PATH, "nselib")):
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

    task_id = log_task(TASK_ID, f"Daily market monitoring {date.today().isoformat()}",
             "Macro snapshot, FII/DII, announcements, block deals", priority="high")
    task_id = task_id or TASK_ID
    update_task_status(TASK_ID, "in_progress")

    results = {}

    # 0. Event calendar — upcoming board meetings + results schedule
    print("[0/5] Saving event calendar (upcoming board meetings)...")
    try:
        from nselib import NSE
        cal_dir = os.path.join(REPO_DIR, "memory_store", "event_calendar")
        os.makedirs(cal_dir, exist_ok=True)
        cal_file = os.path.join(cal_dir, f"{date.today().isoformat()}.json")
        with NSE() as nse:
            events = nse.event_calendar() or []
        with open(cal_file, "w") as f:
            json.dump(events, f, indent=2, default=str)
        results_events = sum(
            1 for e in events
            if any(kw in (e.get("purpose", "") or e.get("bm_desc", "") or "").lower()
                   for kw in ("financial result", "quarterly result", "half year", "annual result"))
        )
        print(f"  {len(events)} events saved → {cal_file}")
        print(f"  {results_events} results events in calendar")
        results["event_calendar"] = {"total": len(events), "results_events": results_events, "file": cal_file}
    except Exception as e:
        print(f"  ERROR: {e}")
        results["event_calendar"] = {"error": str(e)}

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

    # 3c. Structured quarterly results — for companies scheduled today
    print("\n[3c] Fetching structured quarterly results (result-day companies)...")
    try:
        cal_events = results.get("event_calendar", {})
        # Pick symbols scheduled to announce results today
        cal_path = os.path.join(REPO_DIR, "memory_store", "event_calendar", f"{date.today().isoformat()}.json")
        result_symbols = []
        if os.path.exists(cal_path):
            with open(cal_path) as f:
                cal_data = json.load(f)
            for ev in cal_data:
                purpose = (ev.get("purpose", "") or ev.get("bm_desc", "") or "").lower()
                if any(kw in purpose for kw in ("financial result", "quarterly result", "half year", "annual result")):
                    sym = ev.get("symbol", ev.get("nse_symbol", ""))
                    if sym:
                        result_symbols.append(sym.upper())
        result_symbols = list(dict.fromkeys(result_symbols))  # deduplicate, preserve order

        if result_symbols:
            print(f"  {len(result_symbols)} companies expected to report today: {result_symbols[:10]}{'...' if len(result_symbols) > 10 else ''}")
            from jobs.fetch_results import fetch_symbol
            from memory.results_store import save_results
            fetched, failed = 0, []
            for sym in result_symbols:
                try:
                    quarters = fetch_symbol(sym)
                    if quarters:
                        saved = save_results(sym, quarters)
                        fetched += saved.get("saved", 0)
                except Exception as e2:
                    failed.append(f"{sym}: {e2}")
            results["structured_results"] = {
                "symbols_expected": len(result_symbols),
                "quarters_saved": fetched,
                "errors": failed[:5],
            }
            print(f"  Saved {fetched} new quarters; {len(failed)} failures")
        else:
            results["structured_results"] = {"status": "no result companies today"}
            print("  No result companies today")
    except Exception as e:
        print(f"  ERROR: {e}")
        results["structured_results"] = {"error": str(e)}

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

    # 5b. Graph cleanup — trim stale PriceSnapshot and TrendSummary dupes
    print("\n[5b] Graph cleanup (stale nodes)...")
    try:
        from jobs.cleanup_graph import cleanup as cleanup_graph
        cleanup_stats = cleanup_graph()
        results["graph_cleanup"] = cleanup_stats
    except Exception as e:
        print(f"  ERROR: {e}")
        results["graph_cleanup"] = {"error": str(e)}

    # 5c. New data sources — delivery, surveillance, participant OI, FII/DII flat file
    print("\n[5c] Ingesting new NSE data sources (market-wide)...")
    try:
        from jobs.ingest_new_sources import (
            ingest_delivery_bhavcopy,
            ingest_surveillance,
            ingest_participant_oi,
        )
        prev_day = date.today() - __import__("datetime").timedelta(days=1)
        results["delivery_bhavcopy"] = ingest_delivery_bhavcopy(prev_day)
        results["surveillance"]      = ingest_surveillance(date.today())
        results["participant_oi"]    = ingest_participant_oi(prev_day)
        print(f"  delivery: {results['delivery_bhavcopy']}")
        print(f"  surveillance: {results['surveillance']}")
        print(f"  participant_oi: {results['participant_oi']}")
    except Exception as e:
        print(f"  ERROR: {e}")
        results["new_sources"] = {"error": str(e)}

    # 5d. Persist FII/DII flows (from step 2) to flat-file time series
    print("\n[5d] Persisting FII/DII flows to time-series store...")
    try:
        from memory.fii_dii_store import save_flows
        raw_fii = results.get("fii_dii", {})
        flows_list = raw_fii if isinstance(raw_fii, list) else raw_fii.get("data", [])
        if flows_list:
            normalised = []
            for f in flows_list:
                cat = str(f.get("category", f.get("clientType", f.get("type", "")))).upper().strip()
                if not cat:
                    continue
                normalised.append({
                    "category": cat,
                    "buy":  f.get("buyValue", f.get("buy", 0)),
                    "sell": f.get("sellValue", f.get("sell", 0)),
                    "net":  f.get("netValue", f.get("net", 0)),
                    "source": "nse",
                })
            fii_save = save_flows(normalised, trade_date=date.today())
            results["fii_dii_store"] = fii_save
            print(f"  saved: {fii_save}")
        else:
            results["fii_dii_store"] = {"status": "no flow data to store"}
            print("  no FII/DII flow data available")
    except Exception as e:
        print(f"  ERROR: {e}")
        results["fii_dii_store"] = {"error": str(e)}

    # 5e. Enqueue analyst synthesis for top signal tickers
    print("\n[5e] Enqueuing analyst synthesis work...")
    try:
        from jobs.swarm_runner import SwarmRunner
        runner = SwarmRunner()
        enqueued = runner.enqueue_analyst_work()
        results["analyst_enqueued"] = enqueued
        print(f"  enqueued {enqueued} tickers for analyst synthesis")
        log_checkpoint(task_id, status="in_progress", progress=f"step 5e: {enqueued} analyst items enqueued")
    except Exception as e:
        print(f"  ERROR: {e}")
        results["analyst_enqueued"] = {"error": str(e)}
        log_checkpoint(task_id, status="in_progress", progress=f"step 5e skipped: {e}")

    # Step 5f: Corp events fetch (announcements + board meetings → graph)
    print("\n[5f] Fetching corporate events...")
    try:
        from jobs.fetch_corp_events import run_all as fetch_events
        ev = fetch_events(save_to_graph=True)
        results["corp_events"] = ev
        print(f"  announcements={ev.get('announcements',0)}  board_meetings={ev.get('board_meetings',0)}")
        log_checkpoint(task_id, status="in_progress", progress=f"step 5f: {ev}")
    except Exception as e:
        print(f"  ERROR: {e}")
        results["corp_events"] = {"error": str(e)}
        log_checkpoint(task_id, status="in_progress", progress=f"step 5f skipped: {e}")

    # 5g. Analysis + synthesis swarms (context enrichment, trend, analyst)
    print("\n[5g] Running analysis + synthesis swarms...")
    try:
        from jobs.orchestrator import run_analysis_phase, run_synthesis_phase
        analysis_r  = run_analysis_phase(workers=4, verbose=True)
        synthesis_r = run_synthesis_phase(verbose=True)
        results["swarm_analysis"]  = analysis_r
        results["swarm_synthesis"] = synthesis_r
        log_checkpoint(task_id, status="in_progress", progress=f"step 5g: analysis+synthesis done")
    except Exception as e:
        print(f"  ERROR: {e}")
        results["swarm_analysis"] = {"error": str(e)}
        log_checkpoint(task_id, status="in_progress", progress=f"step 5g skipped: {e}")

    # 5h. Meta-cognitive cycle (coverage rebuild + gap detection + tool synthesis)
    print("\n[5h] Meta-cognitive cycle (coverage + gap detection + self-improvement)...")
    try:
        from jobs.orchestrator import run_meta_phase
        meta_r = run_meta_phase(synthesize_tools=5, verbose=True)
        results["meta_cognition"] = meta_r
        log_checkpoint(task_id, status="in_progress", progress=f"step 5h: meta done — enqueued={meta_r.get('meta_cognition',{}).get('enqueued',0)}")
    except Exception as e:
        print(f"  ERROR: {e}")
        results["meta_cognition"] = {"error": str(e)}
        log_checkpoint(task_id, status="in_progress", progress=f"step 5h skipped: {e}")

    # Save output
    out_dir = os.path.join(REPO_DIR, "reports", "daily")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f"{date.today().isoformat()}.json")
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n[output] saved to {out_file}")

    # 6. Self-model reflection
    # Claude reads this block, outputs a JSON reflection, which gets applied to self_model.json.
    print("\n[6/6] Self-model reflection...")
    try:
        from memory.self_model import build_reflection_prompt, apply_reflection, as_context

        results["macro_summary"] = results.get("macro", {}).get("summary", "")
        reflection_prompt = build_reflection_prompt(results)
        print(reflection_prompt)
        # Claude reads the above block and outputs JSON.
        # In-session: parse Claude's output and call apply_reflection(parsed_json).
        # Example (Claude does this after reading the block):
        #   reflection = { "session_note": "...", "new_confirmed": [...], ... }
        #   apply_reflection(reflection)
        print("\n[self-model] Current model:")
        print(as_context() or "  (no model yet — will be populated after first reflection)")
    except Exception as e:
        print(f"  ERROR: {e}")

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

    # Export dashboard data (patch live screener + belief state into dashboard.html)
    try:
        from jobs.export_dashboard_data import export_dashboard_data
        export_dashboard_data(patch_html=True, verbose=True)
    except Exception as _exp_err:
        print(f"[daily_monitor] dashboard export skipped: {_exp_err}")

    # Generate per-ticker research reports for all BUY signals
    try:
        from jobs.generate_report import run_all_reports
        n_reports = run_all_reports(limit=200)
        print(f"[daily_monitor] generated {n_reports} research reports")
    except Exception as _rep_err:
        print(f"[daily_monitor] report generation skipped: {_rep_err}")

    # Commit to GitHub
    _git_commit(f"chore: daily monitor {date.today().isoformat()} — {facts_total} facts ingested")

    print(f"\nDone. Status: {status}")


if __name__ == "__main__":
    run()
