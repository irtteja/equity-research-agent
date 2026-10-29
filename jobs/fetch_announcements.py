#!/usr/bin/env python3
"""
Parallel NSE announcement fetcher for the full tracked universe.

Usage:
    python jobs/fetch_announcements.py                    # today
    python jobs/fetch_announcements.py --date 2026-10-28  # specific date
    python jobs/fetch_announcements.py --days-back 3      # last 3 days

Returns a dict (or saves JSON) with all announcements for every symbol that
filed something in the requested window.  Uses ThreadPoolExecutor with 5
workers — benchmarked sweet spot for NSE's server latency (6× faster than
serial, no gain beyond 5 workers).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.expanduser("~/irtteja/nselib"))

import json
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Optional

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIVERSE_PATH = os.path.join(REPO_DIR, "data", "tracked_universe.json")
MAX_WORKERS = 5


def _load_symbols() -> list[str]:
    with open(UNIVERSE_PATH) as f:
        data = json.load(f)
    return [s["symbol"] for s in data.get("universe", [])]


def _fetch_one(args: tuple) -> tuple[str, list[dict], Optional[str]]:
    """Worker: fetch announcements for one symbol. Returns (symbol, rows, error)."""
    symbol, nse, from_dt, to_dt = args
    try:
        rows = nse.announcements(symbol=symbol, from_date=from_dt, to_date=to_dt)
        return symbol, rows, None
    except Exception as e:
        return symbol, [], str(e)


def fetch_announcements_parallel(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    days_back: int = 1,
    symbols: Optional[list[str]] = None,
    workers: int = MAX_WORKERS,
    verbose: bool = True,
) -> dict:
    """
    Fetch NSE announcements for the full universe (or a subset) in parallel.

    Returns:
        {
          "date_range": {"from": "...", "to": "..."},
          "total_symbols": N,
          "symbols_with_announcements": M,
          "total_announcements": K,
          "errors": N_err,
          "announcements": {symbol: [rows], ...},   # only symbols with ≥1 row
          "error_symbols": {symbol: error_str, ...},
        }
    """
    from nselib import NSE

    to_date   = to_date   or date.today()
    from_date = from_date or (to_date - timedelta(days=days_back - 1))

    if symbols is None:
        symbols = _load_symbols()

    if verbose:
        print(f"  Fetching announcements {from_date} → {to_date} "
              f"for {len(symbols)} symbols ({workers} workers)...")

    nse = NSE(os.path.expanduser("~/.nse_cache"))
    args_list = [(sym, nse, from_date, to_date) for sym in symbols]

    results: dict[str, list] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, args): args[0] for args in args_list}
        done = 0
        for fut in as_completed(futures):
            symbol, rows, err = fut.result()
            done += 1
            if err:
                errors[symbol] = err
            elif rows:
                results[symbol] = rows
            if verbose and done % 100 == 0:
                print(f"    {done}/{len(symbols)} done, "
                      f"{len(results)} with filings, {len(errors)} errors")

    total_ann = sum(len(v) for v in results.values())
    if verbose:
        print(f"  Done — {len(results)} symbols with announcements, "
              f"{total_ann} total, {len(errors)} errors")

    return {
        "date_range": {"from": from_date.isoformat(), "to": to_date.isoformat()},
        "total_symbols": len(symbols),
        "symbols_with_announcements": len(results),
        "total_announcements": total_ann,
        "errors": len(errors),
        "announcements": results,
        "error_symbols": errors,
    }


def save_announcements(result: dict, out_dir: Optional[str] = None) -> str:
    """Save fetch result to memory_store/announcements/YYYY-MM-DD.json."""
    if out_dir is None:
        out_dir = os.path.join(REPO_DIR, "memory_store", "announcements")
    os.makedirs(out_dir, exist_ok=True)
    fname = result["date_range"]["to"] + ".json"
    path = os.path.join(out_dir, fname)
    with open(path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parallel NSE announcement fetch")
    parser.add_argument("--date", help="Specific date YYYY-MM-DD (default: today)")
    parser.add_argument("--days-back", type=int, default=1,
                        help="Number of days back from --date (default: 1 = that day only)")
    parser.add_argument("--save", action="store_true", help="Save output to memory_store/announcements/")
    args = parser.parse_args()

    to_dt = date.fromisoformat(args.date) if args.date else date.today()
    from_dt = to_dt - timedelta(days=args.days_back - 1)

    result = fetch_announcements_parallel(
        from_date=from_dt,
        to_date=to_dt,
        verbose=True,
    )

    print(f"\nDate range    : {result['date_range']['from']} → {result['date_range']['to']}")
    print(f"Total symbols : {result['total_symbols']}")
    print(f"With filings  : {result['symbols_with_announcements']}")
    print(f"Announcements : {result['total_announcements']}")
    print(f"Errors        : {result['errors']}")

    if args.save:
        path = save_announcements(result)
        print(f"Saved to      : {path}")
    else:
        # Pretty-print first 20 announcements as a sample
        print("\nSample (first 20 announcements):")
        count = 0
        for sym, rows in sorted(result["announcements"].items()):
            for row in rows:
                print(f"  {sym:15s}  {row.get('sort_date','')[:16]}  "
                      f"{row.get('desc',''):30s}  {(row.get('attchmntText') or '')[:80]}")
                count += 1
                if count >= 20:
                    break
            if count >= 20:
                break
