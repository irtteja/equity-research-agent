#!/usr/bin/env python3
"""
Bulk knowledge-base builder for the 888-stock mainboard universe.

For each symbol in priority order (Nifty 50 → Next 50 → Midcap 150 → Smallcap 250 → rest):
  1. Concall transcripts  — last 4 actual transcript PDFs via NSE announcements
  2. Annual report        — most recent AR PDF via NSE announcements
  3. Quarterly results    — last 4 quarters of XBRL-sourced structured data

All PDFs are ingested through the document swarm (extract → classify → save).
Results are saved via results_store (idempotent, skips duplicates).

Progress is checkpointed to memory_store/kb_build_progress.json after each symbol
so the job can be safely interrupted and resumed.

Usage:
    python jobs/build_kb.py                         # full run, 4 workers
    python jobs/build_kb.py --workers 8             # more parallelism
    python jobs/build_kb.py --tier 1                # Nifty 50 only (tier 1)
    python jobs/build_kb.py --symbol RELIANCE       # single symbol
    python jobs/build_kb.py --dry-run               # list what would be fetched
    python jobs/build_kb.py --reset                 # clear progress and restart
    python jobs/build_kb.py --status               # print progress summary
"""
import argparse
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from threading import Lock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_NSELIB_PATH = os.path.expanduser("~/irtteja/nselib")
if _NSELIB_PATH not in sys.path:
    sys.path.insert(0, _NSELIB_PATH)

REPO_DIR      = Path(__file__).parent.parent
DATA_DIR      = REPO_DIR / "data"
PROGRESS_FILE = REPO_DIR / "memory_store" / "kb_build_progress.json"

_CONCALL_CAT = "Analysts/Institutional Investor Meet/Con. Call Updates"
_AR_CATS     = ["Annual Report", "Shareholders meeting"]  # NSE files ARs under both
# URL/headline substrings that identify an actual annual report PDF
_AR_PATTERNS = ["annual_report", "annualreport", "_iar", "_ar_", "integrated_annual",
                "annual-report", "final_iar", "ar20", "annrep"]

# Priority tiers — symbols in earlier tiers are processed first
_TIER_INDICES = [
    ["NIFTY 50"],                                   # tier 1 — 50 stocks
    ["NIFTY NEXT 50"],                              # tier 2 — 50 stocks
    ["NIFTY MIDCAP 150"],                           # tier 3 — 150 stocks
    ["NIFTY SMALLCAP 250"],                         # tier 4 — 250 stocks
    # tier 5 = rest of universe (anything not in tiers 1-4)
]

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Referer":    "https://www.nseindia.com/",
}

# ── Utilities ─────────────────────────────────────────────────────────────── #

def _load_universe() -> list[str]:
    path = DATA_DIR / "universe.json"
    with open(path) as f:
        d = json.load(f)
    return d["symbols"]


def _load_index_membership() -> dict[str, list[str]]:
    """Returns {symbol: [index_name, ...]} from index_universe.json."""
    path = DATA_DIR / "index_universe.json"
    if not path.exists():
        return {}
    with open(path) as f:
        d = json.load(f)
    membership: dict[str, list[str]] = {}
    for idx_name, symbols in d.get("indices", {}).items():
        for sym in symbols:
            membership.setdefault(sym, []).append(idx_name)
    return membership


def _priority_order(symbols: list[str]) -> list[tuple[int, str]]:
    """Return [(tier, symbol), ...] sorted by tier asc then symbol asc."""
    flat_tiers: list[list[str]] = []
    for tier_indices in _TIER_INDICES:
        tier_syms: list[str] = []
        # load each index
        idx_path = DATA_DIR / "index_universe.json"
        if idx_path.exists():
            with open(idx_path) as f:
                idx_data = json.load(f)
            for idx_name in tier_indices:
                for sym in idx_data.get("indices", {}).get(idx_name, []):
                    if sym not in tier_syms:
                        tier_syms.append(sym)
        flat_tiers.append(tier_syms)

    tier_of = {}
    for tier_num, tier_syms in enumerate(flat_tiers, 1):
        for sym in tier_syms:
            if sym not in tier_of:
                tier_of[sym] = tier_num

    result = []
    for sym in symbols:
        result.append((tier_of.get(sym, len(_TIER_INDICES) + 1), sym))
    result.sort(key=lambda x: (x[0], x[1]))
    return result


# ── Progress checkpoint ──────────────────────────────────────────────────── #

_progress_lock = Lock()

def _load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_progress(progress: dict):
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _progress_lock:
        PROGRESS_FILE.write_text(json.dumps(progress, indent=2, default=str))


def _mark_done(progress: dict, symbol: str, summary: dict):
    progress[symbol] = {
        "status":   "done",
        "done_at":  date.today().isoformat(),
        **summary,
    }
    _save_progress(progress)


def _mark_failed(progress: dict, symbol: str, reason: str):
    progress[symbol] = {
        "status":  "failed",
        "done_at": date.today().isoformat(),
        "reason":  reason,
    }
    _save_progress(progress)


# ── Concall fetch ────────────────────────────────────────────────────────── #

def _is_actual_transcript(url: str, headline: str) -> bool:
    combined = url.rstrip("/").split("/")[-1].lower() + " " + headline.lower()
    content  = ["transcript", "recording", "audio", "concall", "earnings_call",
                "investorcall", "conferencecall", "conference_call"]
    skip     = ["intimation", "invite", "invitation", "schedule", "notice"]
    if any(p in combined for p in content):
        return True
    if any(p in combined for p in skip):
        return False
    return True  # ingest unknown — better to over-ingest


def _fetch_concall_pdfs(symbol: str, max_count: int = 4) -> list[dict]:
    """Return up to max_count concall PDF metadata dicts for a symbol."""
    from nselib import NSE
    with NSE() as nse:
        rows = nse.announcements(
            symbol=symbol.upper(),
            from_date=date.today() - timedelta(days=548),  # ~18 months = ~6 quarters
            to_date=date.today(),
            category=_CONCALL_CAT,
        )
    results = []
    for r in rows:
        url = r.get("attchmntFile", "")
        headline = r.get("attchmntText", "")
        if url.lower().endswith(".pdf") and _is_actual_transcript(url, headline):
            results.append({
                "symbol":   symbol.upper(),
                "date":     (r.get("sort_date", "") or "")[:10],
                "pdf_url":  url,
                "headline": headline[:200],
                "seq_id":   r.get("seq_id", ""),
            })
        if len(results) >= max_count:
            break
    return results


# ── Annual report fetch ──────────────────────────────────────────────────── #

def _is_annual_report(url: str, headline: str, desc: str) -> bool:
    """Return True if this PDF looks like an actual annual/integrated annual report."""
    combined = (url.lower() + " " + headline.lower() + " " + (desc or "").lower())
    return (
        any(p in combined for p in _AR_PATTERNS)
        or "annual report" in combined
        or "integrated report" in combined
    )


def _fetch_annual_report_pdf(symbol: str) -> dict | None:
    """Return metadata for the most recent annual report PDF, or None."""
    from nselib import NSE

    # Fetch from all relevant categories at once
    all_rows = []
    for cat in _AR_CATS:
        try:
            with NSE() as nse:
                rows = nse.announcements(
                    symbol=symbol.upper(),
                    from_date=date.today() - timedelta(days=548),
                    to_date=date.today(),
                    category=cat,
                )
            all_rows.extend(rows)
        except Exception:
            pass

    # Also do a broad search if we have no hits
    if not all_rows:
        try:
            with NSE() as nse:
                all_rows = nse.announcements(
                    symbol=symbol.upper(),
                    from_date=date.today() - timedelta(days=548),
                    to_date=date.today(),
                )
        except Exception:
            return None

    for r in all_rows:
        url      = r.get("attchmntFile", "") or ""
        headline = r.get("attchmntText", "") or ""
        desc     = r.get("desc", "") or ""
        if url.lower().endswith(".pdf") and _is_annual_report(url, headline, desc):
            return {
                "symbol":   symbol.upper(),
                "date":     (r.get("sort_date", "") or "")[:10],
                "pdf_url":  url,
                "headline": headline[:200],
            }
    return None


# ── PDF download ─────────────────────────────────────────────────────────── #

def _download_pdf(url: str, dest_dir: Path) -> Path | None:
    import requests
    try:
        filename = url.rstrip("/").split("/")[-1] or "doc.pdf"
        dest = dest_dir / filename
        if dest.exists():
            return dest
        resp = requests.get(url, headers=_NSE_HEADERS, timeout=45, stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        return dest
    except Exception as e:
        return None


# ── Quarterly results ────────────────────────────────────────────────────── #

def _fetch_and_save_results(symbol: str) -> dict:
    """Fetch last 4 quarters of XBRL results and save via results_store."""
    from nselib import NSE
    from memory.results_store import save_results
    with NSE() as nse:
        data = nse.financial_results_detail(symbol.upper())
    if not data:
        return {"saved": 0, "skipped": 0, "error": "empty_response"}
    if isinstance(data, dict):
        quarters = data.get("results", data.get("data", [data]))
    elif isinstance(data, list):
        quarters = data
    else:
        quarters = []
    # keep at most 4 most-recent quarters
    quarters = quarters[:4]
    if not quarters:
        return {"saved": 0, "skipped": 0, "error": "no_quarters"}
    out = save_results(symbol.upper(), quarters)
    return {"saved": out["saved"], "skipped": out["skipped"], "error": None}


# ── Per-symbol KB build ──────────────────────────────────────────────────── #

def build_symbol(
    symbol: str,
    known_symbols: set,
    dry_run: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Full KB build for one symbol.
    known_symbols: pre-loaded NSE symbol set (pass once from run() to avoid per-thread reloads).
    Returns summary dict with counts for each content type.
    """
    from agents.document_swarm import _process_one
    summary = {
        "concalls_found": 0, "concalls_ingested": 0,
        "ar_found": 0, "ar_ingested": 0,
        "results_saved": 0, "results_skipped": 0,
        "errors": [],
    }

    # ── 1. Concall transcripts ─────────────────────────────────────────────
    try:
        pdfs = _fetch_concall_pdfs(symbol)
        summary["concalls_found"] = len(pdfs)
        if verbose:
            print(f"    concalls: {len(pdfs)} found")
        if not dry_run and pdfs:
            with tempfile.TemporaryDirectory(prefix=f"concall_{symbol}_") as tmpdir:
                tmp = Path(tmpdir)
                for item in pdfs:
                    local = _download_pdf(item["pdf_url"], tmp)
                    if local is None:
                        summary["errors"].append(f"concall download failed: {item['pdf_url'][-50:]}")
                        continue
                    res = _process_one(local, known_symbols, override_symbol=symbol)
                    if res["status"] == "saved":
                        summary["concalls_ingested"] += 1
    except Exception as e:
        summary["errors"].append(f"concall fetch: {e}")

    time.sleep(0.4)

    # ── 2. Annual report ───────────────────────────────────────────────────
    try:
        ar = _fetch_annual_report_pdf(symbol)
        if ar:
            summary["ar_found"] = 1
            if verbose:
                print(f"    annual report: found {ar['date']}")
            if not dry_run:
                with tempfile.TemporaryDirectory(prefix=f"ar_{symbol}_") as tmpdir:
                    tmp = Path(tmpdir)
                    local = _download_pdf(ar["pdf_url"], tmp)
                    if local:
                        res = _process_one(local, known_symbols, override_symbol=symbol)
                        if res["status"] == "saved":
                            summary["ar_ingested"] = 1
                    else:
                        summary["errors"].append(f"ar download failed: {ar['pdf_url'][-50:]}")
        else:
            if verbose:
                print(f"    annual report: not found")
    except Exception as e:
        summary["errors"].append(f"ar fetch: {e}")

    time.sleep(0.4)

    # ── 3. Quarterly results (XBRL structured) ─────────────────────────────
    try:
        res = _fetch_and_save_results(symbol)
        summary["results_saved"]   = res["saved"]
        summary["results_skipped"] = res["skipped"]
        if res["error"]:
            summary["errors"].append(f"results: {res['error']}")
        elif verbose:
            print(f"    results: saved={res['saved']} skipped={res['skipped']}")
    except Exception as e:
        summary["errors"].append(f"results fetch: {e}")

    return summary


# ── Main run ─────────────────────────────────────────────────────────────── #

def print_status():
    progress = _load_progress()
    done    = sum(1 for v in progress.values() if v.get("status") == "done")
    failed  = sum(1 for v in progress.values() if v.get("status") == "failed")
    all_sym = _load_universe()
    total   = len(all_sym)
    remain  = total - done - failed
    print(f"KB Build Progress")
    print(f"  Total universe: {total}")
    print(f"  Done:           {done}")
    print(f"  Failed:         {failed}")
    print(f"  Remaining:      {remain}")
    if failed:
        print(f"\n  Failed symbols:")
        for sym, v in sorted(progress.items()):
            if v.get("status") == "failed":
                print(f"    {sym}: {v.get('reason','')}")


def run(
    workers:    int  = 4,
    tier:       int | None = None,
    symbol:     str | None = None,
    dry_run:    bool = False,
    verbose:    bool = True,
    reset:      bool = False,
):
    progress = {}
    if reset:
        _save_progress({})
        print("Progress reset.")
    else:
        progress = _load_progress()

    all_symbols = _load_universe()

    if symbol:
        ordered = [(0, symbol.upper())]
    else:
        ordered = _priority_order(all_symbols)
        if tier is not None:
            ordered = [(t, s) for t, s in ordered if t == tier]

    # Skip already done
    to_process = [(t, s) for t, s in ordered if progress.get(s, {}).get("status") != "done"]

    if dry_run:
        print(f"DRY RUN — {len(to_process)} symbols to process")
        for t, s in to_process[:20]:
            print(f"  tier={t}  {s}")
        if len(to_process) > 20:
            print(f"  ... and {len(to_process)-20} more")
        return

    print(f"\n{'='*60}")
    print(f"KB Build — {len(to_process)} symbols ({len(ordered)-len(to_process)} already done)")
    print(f"Workers: {workers}  |  Dry run: {dry_run}")
    print(f"{'='*60}\n")

    done_count = failed_count = 0
    total = len(to_process)

    # Pre-load known NSE symbols once — shared across all workers
    print("Loading NSE symbol list…")
    from agents.document_swarm import _load_known_symbols
    known_symbols = _load_known_symbols()
    print(f"  {len(known_symbols)} symbols loaded\n")

    def _worker(item):
        tier_num, sym = item
        try:
            if verbose:
                print(f"  [{sym}] tier={tier_num} — starting")
            summary = build_symbol(sym, known_symbols, dry_run=dry_run, verbose=verbose)
            _mark_done(progress, sym, summary)
            if verbose:
                errs = summary.get("errors", [])
                print(f"  [{sym}] done — "
                      f"concalls={summary['concalls_ingested']}/{summary['concalls_found']}  "
                      f"ar={summary['ar_ingested']}/{summary['ar_found']}  "
                      f"results={summary['results_saved']}  "
                      f"errors={len(errs)}")
            return ("done", sym, summary)
        except Exception as e:
            _mark_failed(progress, sym, str(e))
            return ("failed", sym, str(e))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_worker, item): item for item in to_process}
        for i, future in enumerate(as_completed(futures), 1):
            status, sym, detail = future.result()
            if status == "done":
                done_count += 1
            else:
                failed_count += 1
                print(f"  [FAILED] {sym}: {detail}")
            if i % 10 == 0 or i == total:
                print(f"\n  Progress: {i}/{total} — done={done_count} failed={failed_count}\n")

    print(f"\n{'='*60}")
    print(f"KB Build complete — done={done_count}  failed={failed_count}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk KB builder for NSE universe")
    parser.add_argument("--workers",  type=int,  default=4,   help="Parallel workers (default 4)")
    parser.add_argument("--tier",     type=int,  default=None, help="Process one tier only (1-5)")
    parser.add_argument("--symbol",              default=None, help="Single symbol override")
    parser.add_argument("--dry-run",  action="store_true",    help="List without fetching")
    parser.add_argument("--reset",    action="store_true",    help="Clear progress and restart")
    parser.add_argument("--status",   action="store_true",    help="Print progress summary")
    parser.add_argument("--verbose",  action="store_true",    help="Per-file detail output")
    args = parser.parse_args()

    if args.status:
        print_status()
    else:
        run(
            workers=args.workers,
            tier=args.tier,
            symbol=args.symbol,
            dry_run=args.dry_run,
            verbose=args.verbose or bool(args.symbol),
            reset=args.reset,
        )
