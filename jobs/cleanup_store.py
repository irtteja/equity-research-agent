#!/usr/bin/env python3
"""
memory_store cleanup — enforce data retention policy.

Retention rules:
  bhavcopy/             365 days  (raw price CSVs — reconstructable from NSE archives)
  indices_bhavcopy/     365 days  (index OHLCV — same)
  announcements/        365 days  (raw announcement JSON)
  documents/            1095 days (annual reports, transcripts — 3 years)

Never deleted:
  results/              XBRL quarterly results — permanent, structured, small
  self_model.json       agent self-model — permanent
  research.db           insights + task log — permanent
  event_calendar/       permanent (tiny)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).parent.parent
STORE = REPO / "memory_store"

RULES = [
    ("bhavcopy",          365,  "*.csv"),
    ("indices_bhavcopy",  365,  "*.csv"),
    ("announcements",     365,  "*.json"),
]
DOCUMENT_RETENTION_DAYS = 1095  # 3 years


def _prune_flat(folder: str, days: int, glob: str, dry_run: bool) -> dict:
    """Delete files in a flat directory whose stem (YYYY-MM-DD) is older than `days`."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    target = STORE / folder
    if not target.exists():
        return {"skipped": True, "reason": "directory not found"}

    deleted = 0
    freed = 0
    for f in sorted(target.glob(glob)):
        if f.stem <= cutoff:
            size = f.stat().st_size
            if not dry_run:
                f.unlink()
            deleted += 1
            freed += size

    return {"deleted": deleted, "freed_mb": round(freed / 1_048_576, 2)}


def _prune_documents(days: int, dry_run: bool) -> dict:
    """
    Delete document JSON files older than `days`.
    Documents live at: documents/{SYMBOL}/{doc_type}/YYYY-MM-DD_*.json
    Also rebuilds _doc_index.jsonl to remove stale entries.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    doc_root = STORE / "documents"
    if not doc_root.exists():
        return {"skipped": True, "reason": "directory not found"}

    deleted = 0
    freed = 0
    for f in doc_root.rglob("*.json"):
        if f.name == "_doc_index.jsonl" or f.name.startswith("_"):
            continue
        # filename starts with YYYY-MM-DD
        date_prefix = f.name[:10]
        if len(date_prefix) == 10 and date_prefix <= cutoff:
            size = f.stat().st_size
            if not dry_run:
                f.unlink()
            deleted += 1
            freed += size

    # Rebuild _doc_index.jsonl (remove entries pointing to deleted files)
    if not dry_run and deleted > 0:
        _rebuild_doc_index(doc_root)

    return {"deleted": deleted, "freed_mb": round(freed / 1_048_576, 2)}


def _rebuild_doc_index(doc_root: Path):
    """Rebuild _doc_index.jsonl keeping only entries whose file still exists."""
    import json
    index_file = doc_root / "_doc_index.jsonl"
    if not index_file.exists():
        return

    kept = []
    with open(index_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                store_path = entry.get("store_path", "")
                if store_path and Path(store_path).exists():
                    kept.append(line)
            except json.JSONDecodeError:
                continue

    with open(index_file, "w") as f:
        for line in kept:
            f.write(line + "\n")

    print(f"  _doc_index.jsonl rebuilt: {len(kept)} entries remaining")


def run(dry_run: bool = False):
    today = date.today().isoformat()
    print(f"\nmemory_store cleanup — {today}")
    print(f"Mode: {'DRY RUN (no files deleted)' if dry_run else 'LIVE'}\n")

    total_deleted = 0
    total_freed_mb = 0.0

    for folder, days, glob in RULES:
        result = _prune_flat(folder, days, glob, dry_run)
        if result.get("skipped"):
            print(f"  {folder:<24} — skipped ({result['reason']})")
        else:
            d, m = result["deleted"], result["freed_mb"]
            total_deleted += d
            total_freed_mb += m
            print(f"  {folder:<24} — {d} files deleted, {m} MB freed  (>{days}d)")

    result = _prune_documents(DOCUMENT_RETENTION_DAYS, dry_run)
    if result.get("skipped"):
        print(f"  {'documents':<24} — skipped ({result['reason']})")
    else:
        d, m = result["deleted"], result["freed_mb"]
        total_deleted += d
        total_freed_mb += m
        print(f"  {'documents':<24} — {d} files deleted, {m} MB freed  (>{DOCUMENT_RETENTION_DAYS}d)")

    print(f"\nTotal: {total_deleted} files deleted, {round(total_freed_mb, 2)} MB freed")
    if dry_run:
        print("(dry run — nothing actually deleted)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="memory_store retention cleanup")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
