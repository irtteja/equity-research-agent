#!/usr/bin/env python3
"""
NSE announcement fetcher — uses bulk CSV endpoint (all_announcements) instead of
per-symbol parallel calls.  Single HTTP request returns all filings for a date
range; previously timed out on the 752-symbol per-symbol loop.

Usage:
    python jobs/fetch_announcements.py                    # today
    python jobs/fetch_announcements.py --date 2026-10-28  # specific date
    python jobs/fetch_announcements.py --days-back 3      # last 3 days

Returns a dict (or saves JSON) with all announcements for every symbol that
filed something in the requested window.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_NSELIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor")
if not os.path.isdir(os.path.join(_NSELIB_PATH, "nselib")):
    _NSELIB_PATH = os.path.expanduser("~/irtteja/nselib")
sys.path.insert(0, _NSELIB_PATH)

import json
import argparse
from datetime import date, timedelta
from typing import Optional

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIVERSE_PATH = os.path.join(REPO_DIR, "data", "tracked_universe.json")
# kept for callers that still pass workers= — ignored now
MAX_WORKERS = 5


def _load_symbols() -> list[str]:
    with open(UNIVERSE_PATH) as f:
        data = json.load(f)
    return [s["symbol"] for s in data.get("universe", [])]


def fetch_announcements_parallel(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    days_back: int = 1,
    symbols: Optional[list[str]] = None,
    workers: int = MAX_WORKERS,   # ignored — kept for API compat
    verbose: bool = True,
) -> dict:
    """
    Fetch NSE announcements via bulk CSV endpoint (NSE.all_announcements).

    One HTTP call returns all filings; result is reshaped to per-symbol dict.
    Much faster than the old per-symbol parallel loop which timed out at ~120s.

    Returns:
        {
          "date_range": {"from": "...", "to": "..."},
          "total_symbols": N,
          "symbols_with_announcements": M,
          "total_announcements": K,
          "errors": 0,
          "announcements": {symbol: [rows], ...},   # only symbols with ≥1 row
          "error_symbols": {},
        }
    """
    from nselib import NSE

    to_date   = to_date   or date.today()
    from_date = from_date or (to_date - timedelta(days=days_back - 1))

    # Optional universe filter — load once so we can count total_symbols
    universe_symbols: Optional[set[str]] = None
    if symbols is not None:
        universe_symbols = set(symbols)
    else:
        try:
            universe_symbols = set(_load_symbols())
        except Exception:
            universe_symbols = None   # fall back to unfiltered bulk

    if verbose:
        n = len(universe_symbols) if universe_symbols else "all"
        print(f"  Fetching announcements {from_date} → {to_date} "
              f"(bulk call, universe={n})...")

    nse = NSE(os.path.expanduser("~/.nse_cache"))
    rows = nse.all_announcements(from_date=from_date, to_date=to_date, index="equities")

    # Group by symbol, optionally filtering to tracked universe
    results: dict[str, list] = {}
    skipped = 0
    for row in rows:
        sym = row.get("SYMBOL", "").strip()
        if not sym:
            continue
        if universe_symbols is not None and sym not in universe_symbols:
            skipped += 1
            continue
        results.setdefault(sym, []).append(row)

    total_ann = sum(len(v) for v in results.values())
    if verbose:
        print(f"  Done — {len(results)} symbols with announcements, "
              f"{total_ann} total (skipped {skipped} non-universe rows)")

    return {
        "date_range": {"from": from_date.isoformat(), "to": to_date.isoformat()},
        "total_symbols": len(universe_symbols) if universe_symbols else 0,
        "symbols_with_announcements": len(results),
        "total_announcements": total_ann,
        "errors": 0,
        "announcements": results,
        "error_symbols": {},
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
        # auto-ingest concall transcript PDFs found in today's announcements
        from jobs.ingest_concalls import ingest_concalls
        ingest_concalls(ann_date=to_dt, verbose=True)
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
