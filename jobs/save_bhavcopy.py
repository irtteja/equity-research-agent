#!/usr/bin/env python3
"""
Daily bhavcopy save — downloads NSE equity bhavcopy, filters to tracked universe
(EQ + BE series, no SME), saves to memory_store/bhavcopy/YYYY-MM-DD.csv.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.expanduser("~/irtteja/nselib"))

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

REPO_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BHAVCOPY_DIR = REPO_DIR / "memory_store" / "bhavcopy"
UNIVERSE_PATH = REPO_DIR / "data" / "tracked_universe.json"

# Columns worth keeping (drop derivatives/bond-specific fields)
KEEP_COLS = [
    "TradDt", "TckrSymb", "ISIN", "SctySrs",
    "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric", "PrvsClsgPric",
    "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd",
]


def fetch_and_save(dt: date = None) -> dict:
    """
    Download bhavcopy for `dt` (defaults to today), filter to tracked universe,
    save to memory_store/bhavcopy/YYYY-MM-DD.csv.
    Returns summary dict.
    """
    import pandas as pd
    from nselib import NSE

    dt = dt or date.today()
    out_path = BHAVCOPY_DIR / f"{dt.isoformat()}.csv"

    if out_path.exists():
        return {"date": dt.isoformat(), "status": "already_exists", "file": str(out_path.relative_to(REPO_DIR))}

    # Load tracked symbols
    with open(UNIVERSE_PATH) as f:
        universe = json.load(f)
    tracked_symbols = {c["symbol"] for c in universe["universe"]}

    with tempfile.TemporaryDirectory() as tmpdir:
        with NSE() as nse:
            try:
                csv_path = nse.equity_bhavcopy(dt=dt, folder=tmpdir)
            except Exception as e:
                return {"date": dt.isoformat(), "status": "download_failed", "error": str(e)}

        df = pd.read_csv(csv_path)

    # Filter: main board only (EQ + BE), no SME
    df = df[df["SctySrs"].isin(["EQ", "BE"])].copy()

    # Filter to tracked universe
    df["TckrSymb"] = df["TckrSymb"].str.strip()
    tracked_df = df[df["TckrSymb"].isin(tracked_symbols)].copy()

    # Keep only useful columns (drop missing ones gracefully)
    cols = [c for c in KEEP_COLS if c in tracked_df.columns]
    tracked_df = tracked_df[cols]

    # Save
    BHAVCOPY_DIR.mkdir(parents=True, exist_ok=True)
    tracked_df.to_csv(out_path, index=False)

    return {
        "date": dt.isoformat(),
        "status": "saved",
        "rows": len(tracked_df),
        "total_eq_be": len(df),
        "file": str(out_path.relative_to(REPO_DIR)),
    }


def backfill(days: int = 30) -> list[dict]:
    """Download and save bhavcopy for the last N calendar days (skips weekends/holidays)."""
    results = []
    today = date.today()
    for i in range(days):
        dt = today - timedelta(days=i)
        if dt.weekday() >= 5:  # skip weekends
            continue
        r = fetch_and_save(dt)
        results.append(r)
        if r["status"] == "saved":
            print(f"  {dt}: {r['rows']} rows saved")
        elif r["status"] == "already_exists":
            print(f"  {dt}: already exists")
        else:
            print(f"  {dt}: {r['status']} — {r.get('error','')}")
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", type=int, default=0, help="Backfill N calendar days")
    parser.add_argument("--date", type=str, default=None, help="Specific date YYYY-MM-DD")
    args = parser.parse_args()

    if args.backfill:
        print(f"Backfilling {args.backfill} days...")
        backfill(args.backfill)
    else:
        dt = date.fromisoformat(args.date) if args.date else date.today()
        r = fetch_and_save(dt)
        print(r)
