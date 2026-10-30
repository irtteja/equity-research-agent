#!/usr/bin/env python3
"""
Nightly results fetch job — runs at 1 AM IST.

1. Read today's event_calendar JSON for companies scheduled to report results
2. Parallel-fetch financial_results_detail() for each scheduled company
3. Save structured XBRL data to memory_store/results/<TICKER>.jsonl via results_store
4. Flag companies that haven't filed yet (for 8 AM retry)
5. Commit to GitHub

Parallel fetch saturates 10 workers; ~5s for 50 companies.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_NSELIB_PATH = os.path.expanduser("~/irtteja/nselib")
if _NSELIB_PATH not in sys.path:
    sys.path.insert(0, _NSELIB_PATH)

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

from memory.results_store import save_results, get_latest

REPO_DIR   = Path(__file__).parent.parent
CAL_DIR    = REPO_DIR / "memory_store" / "event_calendar"
WORKERS    = 10
MAX_RETRY_FLAG_FILE = REPO_DIR / "memory_store" / "results" / "_pending_retry.json"


def _git_commit(message: str):
    try:
        subprocess.run(["git", "add", "-A"], cwd=REPO_DIR, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=REPO_DIR, check=True)
        subprocess.run(["git", "push", "-u", "origin", "master"], cwd=REPO_DIR, check=True)
        print(f"[git] {message}")
    except subprocess.CalledProcessError as e:
        print(f"[git] push failed: {e}")


def load_calendar(for_date: date) -> list[dict]:
    """Load the event_calendar JSON saved by daily_monitor for the given date."""
    path = CAL_DIR / f"{for_date.isoformat()}.json"
    if not path.exists():
        print(f"  [calendar] no file for {for_date} — trying yesterday")
        path = CAL_DIR / f"{(for_date - timedelta(days=1)).isoformat()}.json"
    if not path.exists():
        print(f"  [calendar] no event_calendar JSON found — fetching live")
        return []
    events = json.loads(path.read_text())
    print(f"  [calendar] loaded {len(events)} events from {path.name}")
    return events


def fetch_live_calendar() -> list[dict]:
    """Fallback: fetch event_calendar() directly from NSE."""
    from nselib import NSE
    with NSE() as nse:
        return nse.event_calendar() or []


def filter_results_events(events: list[dict], target_date: date) -> list[str]:
    """
    Return symbols scheduled for financial results on or before target_date.
    Accepts both exact date match and 'already due' (date <= target).
    """
    symbols = []
    purpose_keywords = ("financial result", "quarterly result", "half year", "annual result",
                        "financial results", "un-audited", "audited")
    for ev in events:
        ev_date_str = ev.get("date", "") or ev.get("bm_date", "")
        purpose = (ev.get("purpose", "") or ev.get("bm_desc", "") or "").lower()

        if not any(kw in purpose for kw in purpose_keywords):
            continue

        try:
            # NSE calendar returns dates as "31-Oct-2026" or ISO "2026-10-31"
            from datetime import datetime
            raw = ev_date_str.strip()
            if raw and raw[2:3] == "-":
                ev_date = datetime.strptime(raw[:11], "%d-%b-%Y").date()
            else:
                ev_date = date.fromisoformat(raw[:10])
        except Exception:
            continue

        if ev_date <= target_date:
            sym = ev.get("symbol", "") or ev.get("sym", "")
            if sym:
                symbols.append(sym.upper())

    seen = set()
    unique = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def fetch_one(symbol: str) -> dict:
    """Fetch financial_results_detail() for one ticker. Returns {symbol, quarters, error}."""
    try:
        from nselib import NSE
        with NSE() as nse:
            data = nse.financial_results_detail(symbol)
        if not data:
            return {"symbol": symbol, "quarters": [], "error": "empty_response"}

        # Normalise: data may be a list of dicts or a dict with a results key
        if isinstance(data, dict):
            quarters = data.get("results", data.get("data", [data]))
        elif isinstance(data, list):
            quarters = data
        else:
            quarters = []

        return {"symbol": symbol, "quarters": quarters, "error": None}
    except Exception as e:
        return {"symbol": symbol, "quarters": [], "error": str(e)}


def run(retry_mode: bool = False):
    """
    retry_mode=True  → process symbols from _pending_retry.json (8 AM run)
    retry_mode=False → process yesterday's calendar events (1 AM run)
    """
    today     = date.today()
    yesterday = today - timedelta(days=1)
    target    = yesterday  # results announced "today" have period_end ≤ yesterday

    print(f"\n{'='*60}")
    print(f"Fetch Results — {'RETRY' if retry_mode else 'NIGHTLY'} — {today.isoformat()}")
    print(f"{'='*60}\n")

    # ── Resolve symbol list ───────────────────────────────────────────────────
    if retry_mode:
        if not MAX_RETRY_FLAG_FILE.exists():
            print("  No pending retry file — nothing to do.")
            return
        pending = json.loads(MAX_RETRY_FLAG_FILE.read_text())
        symbols = pending.get("symbols", [])
        print(f"  Retry mode: {len(symbols)} symbols from previous run")
    else:
        events = load_calendar(yesterday) or load_calendar(today)
        if not events:
            print("  [calendar] fetching live…")
            events = fetch_live_calendar()

        symbols = filter_results_events(events, target)
        print(f"  {len(symbols)} companies scheduled for results by {target}")

    if not symbols:
        print("  Nothing to fetch.")
        return

    # ── Parallel fetch ────────────────────────────────────────────────────────
    print(f"\n  Fetching financial_results_detail() for {len(symbols)} symbols "
          f"({WORKERS} workers)…")

    saved_total   = 0
    skipped_total = 0
    errors        = []
    not_filed     = []  # symbols where data was empty (not yet filed)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_one, sym): sym for sym in symbols}
        for future in as_completed(futures):
            sym    = futures[future]
            result = future.result()

            if result["error"]:
                errors.append({"symbol": sym, "error": result["error"]})
                print(f"  ✗ {sym}: {result['error']}")
                continue

            quarters = result["quarters"]
            if not quarters:
                not_filed.append(sym)
                print(f"  - {sym}: no data (not yet filed?)")
                continue

            save_out = save_results(sym, quarters)
            saved_total   += save_out["saved"]
            skipped_total += save_out["skipped"]

            latest = get_latest(sym)
            period = latest.get("period_end", "?") if latest else "?"
            print(f"  ✓ {sym}: saved={save_out['saved']} skipped={save_out['skipped']} "
                  f"latest={period}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n  Done: {saved_total} quarters saved, {skipped_total} already stored, "
          f"{len(errors)} errors, {len(not_filed)} not yet filed")

    if errors:
        print(f"\n  Errors:")
        for e in errors:
            print(f"    {e['symbol']}: {e['error']}")

    # ── Persist not-filed list for 8 AM retry ────────────────────────────────
    MAX_RETRY_FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not retry_mode and not_filed:
        MAX_RETRY_FLAG_FILE.write_text(json.dumps({
            "date": today.isoformat(),
            "symbols": not_filed,
            "reason": "not_filed_at_1am",
        }, indent=2))
        print(f"\n  {len(not_filed)} symbols written to retry list for 8 AM run")
    elif retry_mode:
        # Clean up after retry
        if MAX_RETRY_FLAG_FILE.exists():
            MAX_RETRY_FLAG_FILE.unlink()
        print(f"  Retry complete — pending file removed")

    # ── Commit ────────────────────────────────────────────────────────────────
    mode_tag = "retry" if retry_mode else "nightly"
    _git_commit(
        f"results({mode_tag}): {today.isoformat()} — "
        f"{saved_total}q saved, {len(not_filed)} pending"
    )


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--retry", action="store_true", help="8 AM retry for not-yet-filed companies")
    args = p.parse_args()
    run(retry_mode=args.retry)
