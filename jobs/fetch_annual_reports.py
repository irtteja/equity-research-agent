#!/usr/bin/env python3
"""
Fetch annual report PDFs for tracked NSE stocks.

For each tracked ticker:
  1. Calls nse.annual_reports(symbol) → list of {fileName (PDF URL), fromYr, toYr, ...}
  2. Downloads the latest annual report PDF via nse.download_document()
  3. Saves to memory_store/annual_reports/<TICKER>/<fromYr>-<toYr>.pdf
  4. Saves metadata to memory_store/annual_reports/<TICKER>/index.json

Skips tickers that already have the latest year's PDF.
Safe to re-run — idempotent by year.

Run: python jobs/fetch_annual_reports.py [--ticker SYMBOL] [--workers N] [--limit N]
     python jobs/fetch_annual_reports.py --ticker RELIANCE
"""
import sys
import json
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

OUT_DIR = REPO / "memory_store" / "annual_reports"
WORKERS = 5   # conservative — PDFs are large


def load_universe() -> list[str]:
    prog = REPO / "memory_store" / "kb_build_progress.json"
    if not prog.exists():
        syms_path = REPO / "memory_store" / "symbols.json"
        return json.loads(syms_path.read_text()) if syms_path.exists() else []
    data = json.loads(prog.read_text())
    return sorted(s.upper() for s in data)


def latest_year_on_disk(sym: str) -> str | None:
    """Return latest 'fromYr-toYr' string we already have on disk, or None."""
    sym_dir = OUT_DIR / sym
    if not sym_dir.exists():
        return None
    pdfs = list(sym_dir.glob("*.pdf"))
    if not pdfs:
        return None
    return sorted(pdfs)[-1].stem   # e.g. "2025-2026"


def fetch_one(nse, symbol: str) -> dict:
    try:
        reports = nse.annual_reports(symbol)
        if not reports:
            return {"symbol": symbol, "status": "no_reports"}

        # Latest report is first
        latest = reports[0]
        year_key = f"{latest['fromYr']}-{latest['toYr']}"

        # Already downloaded?
        sym_dir = OUT_DIR / symbol
        pdf_path = sym_dir / f"{year_key}.pdf"
        if pdf_path.exists():
            return {"symbol": symbol, "status": "exists", "year": year_key}

        # Download
        sym_dir.mkdir(parents=True, exist_ok=True)
        downloaded = nse.download_document(
            url=latest["fileName"],
            folder=str(sym_dir),
            filename=f"{year_key}.pdf",
        )

        # Save index
        idx_path = sym_dir / "index.json"
        idx = []
        if idx_path.exists():
            try:
                idx = json.loads(idx_path.read_text())
            except Exception:
                pass
        # Upsert by year_key
        idx = [r for r in idx if f"{r.get('fromYr')}-{r.get('toYr')}" != year_key]
        idx.insert(0, {**latest, "local_path": str(pdf_path.relative_to(REPO))})
        idx_path.write_text(json.dumps(idx, indent=2, default=str))

        size_mb = pdf_path.stat().st_size / 1_048_576 if pdf_path.exists() else 0
        return {"symbol": symbol, "status": "downloaded", "year": year_key, "size_mb": round(size_mb, 2)}

    except Exception as e:
        return {"symbol": symbol, "status": "error", "error": str(e)}


def run(symbols: list[str] | None = None, workers: int = WORKERS, limit: int | None = None):
    sys.path.insert(0, str(Path.home() / "irtteja" / "nselib"))
    from nselib import NSE

    if symbols is None:
        symbols = load_universe()
    if limit:
        symbols = symbols[:limit]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[annual_reports] fetching for {len(symbols)} symbols ({workers} workers)…")
    t0 = time.time()

    downloaded = skipped = errors = 0

    with NSE() as nse:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_one, nse, sym): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                r   = future.result()
                status = r.get("status")
                if status == "downloaded":
                    downloaded += 1
                    print(f"  ✓ {sym}: {r.get('year')} ({r.get('size_mb')}MB)")
                elif status == "exists":
                    skipped += 1
                elif status == "error":
                    errors += 1
                    print(f"  ✗ {sym}: {r.get('error','')[:80]}")

    elapsed = time.time() - t0
    print(f"\n[annual_reports] done in {elapsed:.1f}s: "
          f"{downloaded} downloaded, {skipped} already had, {errors} errors")
    return {"downloaded": downloaded, "skipped": skipped, "errors": errors}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ticker", help="Single ticker to fetch")
    p.add_argument("--workers", type=int, default=WORKERS)
    p.add_argument("--limit", type=int, default=None, help="Only first N symbols (testing)")
    args = p.parse_args()

    syms = [args.ticker.upper()] if args.ticker else None
    run(symbols=syms, workers=args.workers, limit=args.limit)
