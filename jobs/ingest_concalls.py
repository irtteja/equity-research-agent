#!/usr/bin/env python3
"""
Concall transcript auto-ingestion.

Reads the saved announcement JSON for a given date, finds every filing with
category 'Analysts/Institutional Investor Meet/Con. Call Updates' that has a
PDF attachment, downloads each PDF, and feeds it into the document swarm.

Deduplication is content-hash based (handled by memory/doc_store.py) — the
same PDF downloaded on a re-run is silently skipped.

Usage (standalone):
    python jobs/ingest_concalls.py                    # process today's announcement file
    python jobs/ingest_concalls.py --date 2026-10-28  # specific date
    python jobs/ingest_concalls.py --dry-run          # list what would be ingested

Called automatically by fetch_announcements.py at the end of every daily fetch.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import tempfile
import requests
from datetime import date
from pathlib import Path

REPO       = Path(__file__).parent.parent
ANN_DIR    = REPO / "memory_store" / "announcements"
CONCALL_CAT = "analysts/institutional investor meet/con. call updates"


def _load_tracked_symbols() -> set[str]:
    path = REPO / "data" / "tracked_universe.json"
    try:
        with open(path) as f:
            data = json.load(f)
        return {s["symbol"].upper() for s in data.get("universe", [])}
    except Exception:
        return set()  # if universe file missing, allow all

# Filename patterns that indicate an actual transcript/recording vs a schedule intimation.
# We only ingest actual content — intimation/invite PDFs are scheduling notices, not research material.
_CONTENT_PATTERNS = [
    "transcript", "recording", "audio", "concall", "earnings_call",
    "earningcall", "investorcall", "conferencecall", "conference_call",
]
_SKIP_PATTERNS = [
    "intimation", "invite", "invitation", "schedule", "notice",
    "intimation_schedule", "callinvite",
]

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Referer":    "https://www.nseindia.com/",
}


def _load_announcement_file(ann_date: date) -> dict | None:
    path = ANN_DIR / f"{ann_date.isoformat()}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _is_content_pdf(url: str, headline: str) -> bool:
    """
    Return True if this PDF is likely an actual transcript/recording,
    False if it's just a schedule intimation or invite notice.

    Strategy: accept if the filename or headline contains a content keyword;
    reject if it contains only schedule/intimation keywords with no content keyword.
    """
    fname    = url.rstrip("/").split("/")[-1].lower()
    combined = fname + " " + headline.lower()

    has_content  = any(p in combined for p in _CONTENT_PATTERNS)
    has_schedule = any(p in combined for p in _SKIP_PATTERNS)

    if has_content:
        return True          # explicit content keyword wins
    if has_schedule:
        return False         # pure intimation/schedule notice — skip
    return True              # unknown — ingest (better to over-ingest than miss)


def _collect_concall_pdfs(ann_data: dict, tracked: set[str]) -> list[dict]:
    """
    Extract concall PDF filings from a saved announcement JSON.
    Filters to tracked-universe symbols only, and skips pure schedule intimations.
    """
    pdfs = []
    for symbol, rows in ann_data.get("announcements", {}).items():
        if tracked and symbol.upper() not in tracked:
            continue  # skip untracked symbols — keeps daily volume manageable
        for row in rows:
            desc     = (row.get("desc") or "").lower()
            url      = row.get("attchmntFile", "")
            headline = row.get("attchmntText") or ""
            if CONCALL_CAT in desc and url.lower().endswith(".pdf"):
                if _is_content_pdf(url, headline):
                    pdfs.append({
                        "symbol":   symbol,
                        "date":     (row.get("sort_date") or "")[:10],
                        "pdf_url":  url,
                        "headline": headline[:200],
                        "seq_id":   row.get("seq_id", ""),
                    })
    return pdfs


def _download_pdf(url: str, dest_dir: Path) -> Path | None:
    """Download a PDF to dest_dir. Returns local path or None on failure."""
    try:
        filename = url.rstrip("/").split("/")[-1] or "concall.pdf"
        dest = dest_dir / filename
        if dest.exists():
            return dest
        resp = requests.get(url, headers=_NSE_HEADERS, timeout=30, stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        return dest
    except Exception as e:
        print(f"    ✗ download failed {url[-60:]}: {e}")
        return None


def ingest_concalls(
    ann_date: date | None = None,
    dry_run: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Main entry point.

    ann_date: date whose announcement file to process (default: today)
    dry_run:  list candidates without downloading or ingesting
    Returns summary dict: {date, found, ingested, duplicates, errors}
    """
    ann_date = ann_date or date.today()

    ann_data = _load_announcement_file(ann_date)
    if ann_data is None:
        if verbose:
            print(f"  [concall] No announcement file for {ann_date} — skipping")
        return {"date": str(ann_date), "found": 0, "ingested": 0, "duplicates": 0, "errors": 0}

    tracked = _load_tracked_symbols()
    pdfs = _collect_concall_pdfs(ann_data, tracked)

    if verbose:
        tag = " [DRY RUN]" if dry_run else ""
        print(f"\n  [concall] {ann_date}{tag} — {len(pdfs)} concall PDFs found")

    if not pdfs:
        return {"date": str(ann_date), "found": 0, "ingested": 0, "duplicates": 0, "errors": 0}

    if dry_run:
        for p in pdfs:
            print(f"    {p['symbol']:15s}  {p['date']}  {p['pdf_url'][-60:]}")
        return {"date": str(ann_date), "found": len(pdfs), "ingested": 0, "duplicates": 0, "errors": 0}

    # import here to keep startup fast when used as library
    from agents.document_swarm import _process_one, _load_known_symbols

    known_symbols = _load_known_symbols()
    ingested = duplicates = errors = 0

    with tempfile.TemporaryDirectory(prefix="concall_") as tmpdir:
        tmp = Path(tmpdir)
        for item in pdfs:
            symbol  = item["symbol"]
            pdf_url = item["pdf_url"]

            if verbose:
                print(f"    ↓ {symbol:15s}  {pdf_url[-55:]}")

            local = _download_pdf(pdf_url, tmp)
            if local is None:
                errors += 1
                continue

            result = _process_one(local, known_symbols, override_symbol=symbol)
            status = result.get("status", "error")

            if status == "saved":
                ingested += 1
                if verbose:
                    print(f"      ✓ saved  doc_id={result.get('doc_id','')}  "
                          f"type={result.get('doc_type','')}  {result.get('chars',0):,}ch")
            elif status == "duplicate":
                duplicates += 1
                if verbose:
                    print(f"      ~ duplicate (already ingested)")
            else:
                errors += 1
                if verbose:
                    print(f"      ✗ error: {result.get('reason','')}")

    summary = {
        "date":       str(ann_date),
        "found":      len(pdfs),
        "ingested":   ingested,
        "duplicates": duplicates,
        "errors":     errors,
    }
    if verbose:
        print(f"  [concall] done — ingested={ingested}  dupes={duplicates}  errors={errors}")
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Ingest concall transcript PDFs")
    parser.add_argument("--date",    help="Announcement date YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="List without ingesting")
    args = parser.parse_args()

    ann_date = date.fromisoformat(args.date) if args.date else date.today()
    ingest_concalls(ann_date=ann_date, dry_run=args.dry_run, verbose=True)
