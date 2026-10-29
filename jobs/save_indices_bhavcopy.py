#!/usr/bin/env python3
"""
Daily index bhavcopy save — downloads NSE index-level OHLCV + valuation data,
saves to memory_store/indices_bhavcopy/YYYY-MM-DD.csv.

Columns: Index Name, Open, High, Low, Close, Points Change, Change(%), Volume,
         Turnover (Rs. Cr.), P/E, P/B, Div Yield
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.expanduser("~/irtteja/nselib"))

import tempfile
from datetime import date, timedelta
from pathlib import Path

REPO_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = REPO_DIR / "memory_store" / "indices_bhavcopy"


def fetch_and_save(dt: date = None) -> dict:
    import pandas as pd
    from nselib import NSE

    dt = dt or date.today()
    out_path = OUT_DIR / f"{dt.isoformat()}.csv"

    if out_path.exists():
        return {"date": dt.isoformat(), "status": "already_exists", "file": str(out_path.relative_to(REPO_DIR))}

    with tempfile.TemporaryDirectory() as tmpdir:
        with NSE() as nse:
            try:
                csv_path = nse.indices_bhavcopy(dt=dt, folder=tmpdir)
            except Exception as e:
                return {"date": dt.isoformat(), "status": "download_failed", "error": str(e)}

        df = pd.read_csv(csv_path)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    return {
        "date": dt.isoformat(),
        "status": "saved",
        "rows": len(df),
        "file": str(out_path.relative_to(REPO_DIR)),
    }


def backfill(days: int = 30) -> list[dict]:
    results = []
    today = date.today()
    for i in range(days):
        dt = today - timedelta(days=i)
        if dt.weekday() >= 5:
            continue
        r = fetch_and_save(dt)
        results.append(r)
        if r["status"] == "saved":
            print(f"  {dt}: {r['rows']} indices saved")
        elif r["status"] == "already_exists":
            print(f"  {dt}: already exists")
        else:
            print(f"  {dt}: {r['status']} — {r.get('error','')}")
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", type=int, default=0)
    parser.add_argument("--date", type=str, default=None)
    args = parser.parse_args()

    if args.backfill:
        print(f"Backfilling {args.backfill} days...")
        backfill(args.backfill)
    else:
        dt = date.fromisoformat(args.date) if args.date else date.today()
        r = fetch_and_save(dt)
        print(r)
