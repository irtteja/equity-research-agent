#!/usr/bin/env python3
"""
Operational memory compaction — prune logs, DB rows, JSONL files, and old reports.

Complements jobs/cleanup_store.py (which handles raw market data files).
This module handles derived / operational data that accumulates over time.

Retention rules enforced here:
  logs/tasks.log              keep last 500 entries (completed tasks older than 90d removed)
  memory_store/self_improve_log.jsonl   keep last 50 entries
  memory/research.db          tasks completed >90d → delete
                              checkpoints >90d → delete
                              knowledge_base insights >365d → summarise subject and delete originals
                              macro_snapshots >90d → delete (reconstructable)
  reports/daily/              keep last 90 days
  reports/triage_test.json    one-shot test artifact — delete if >7d old
  memory_store/index/         stale TF-IDF index files if their source doc is gone

Run modes:
  python jobs/prune_memory.py            # full compaction
  python jobs/prune_memory.py --dry-run  # show what would be deleted, touch nothing
  python jobs/prune_memory.py --logs     # logs only
  python jobs/prune_memory.py --db       # DB only
  python jobs/prune_memory.py --reports  # reports only

Results printed to stdout and appended to memory_store/self_improve_log.jsonl.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

REPO  = Path(__file__).parent.parent
STORE = REPO / "memory_store"
LOG   = STORE / "self_improve_log.jsonl"

# ── Retention constants ─────────────────────────────────────────────────────

TASK_LOG_MAX_ENTRIES       = 500    # hard cap on tasks.log lines
TASK_LOG_COMPLETED_DAYS    = 90     # completed tasks older than this are dropped
SELF_IMPROVE_LOG_MAX       = 50     # keep last N self_improve runs
DB_TASKS_COMPLETED_DAYS    = 90     # completed tasks in research.db
DB_CHECKPOINTS_DAYS        = 90     # checkpoints older than this
DB_MACRO_DAYS              = 90     # macro snapshots (reconstructable)
DB_KNOWLEDGE_BASE_DAYS     = 365    # raw insight rows older than 1yr
DAILY_REPORTS_DAYS         = 90     # reports/daily/ JSON files


# ── Utilities ───────────────────────────────────────────────────────────────

def _log_result(entry: dict):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    entry["ts"] = datetime.now().isoformat()
    with open(LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _trim_jsonl(path: Path, keep: int, dry_run: bool) -> dict:
    """Keep only the last `keep` lines of a JSONL file."""
    if not path.exists():
        return {"ok": True, "reason": "file not found"}
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    if len(lines) <= keep:
        return {"kept": len(lines), "removed": 0}
    to_remove = len(lines) - keep
    if not dry_run:
        path.write_text("\n".join(lines[-keep:]) + "\n")
    return {"kept": keep, "removed": to_remove}


def _trim_tasks_log(path: Path, max_entries: int, completed_days: int, dry_run: bool) -> dict:
    """
    Prune tasks.log: remove completed-task entries older than completed_days,
    then hard-cap to max_entries lines if still over.
    """
    if not path.exists():
        return {"ok": True, "reason": "file not found"}

    cutoff = (date.today() - timedelta(days=completed_days)).isoformat()
    lines = path.read_text().splitlines()
    kept, removed = [], 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            status = entry.get("status", "")
            ts     = entry.get("ts", "9999")[:10]  # YYYY-MM-DD prefix
            # drop completed/failed tasks older than cutoff
            if status in ("completed", "failed") and ts < cutoff:
                removed += 1
                continue
        except (json.JSONDecodeError, KeyError):
            pass  # keep malformed lines — don't silently destroy unknown data
        kept.append(line)

    # hard cap
    if len(kept) > max_entries:
        overflow = len(kept) - max_entries
        kept = kept[-max_entries:]
        removed += overflow

    if not dry_run:
        path.write_text("\n".join(kept) + "\n")

    return {"kept": len(kept), "removed": removed}


# ── Pruners ─────────────────────────────────────────────────────────────────

def prune_logs(dry_run: bool = False) -> dict:
    print("\n[LOGS] Pruning operational logs...")
    results = {}

    # tasks.log
    p = REPO / "logs" / "tasks.log"
    r = _trim_tasks_log(p, TASK_LOG_MAX_ENTRIES, TASK_LOG_COMPLETED_DAYS, dry_run)
    results["tasks_log"] = r
    print(f"  tasks.log          kept={r.get('kept',0)}  removed={r.get('removed',0)}")

    # self_improve_log.jsonl
    p2 = STORE / "self_improve_log.jsonl"
    r2 = _trim_jsonl(p2, SELF_IMPROVE_LOG_MAX, dry_run)
    results["self_improve_log"] = r2
    print(f"  self_improve_log   kept={r2.get('kept',0)}  removed={r2.get('removed',0)}")

    return results


def prune_db(dry_run: bool = False) -> dict:
    print("\n[DB] Pruning research.db...")
    db_path = REPO / "memory" / "research.db"
    if not db_path.exists():
        print("  research.db not found — skip")
        return {"ok": True}

    conn = sqlite3.connect(str(db_path))
    results = {}
    cutoff_90  = (datetime.now() - timedelta(days=DB_TASKS_COMPLETED_DAYS)).isoformat()
    cutoff_365 = (datetime.now() - timedelta(days=DB_KNOWLEDGE_BASE_DAYS)).isoformat()

    def _count_and_delete(table: str, where: str, params: tuple) -> int:
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
        if n and not dry_run:
            conn.execute(f"DELETE FROM {table} WHERE {where}", params)
        return n

    # tasks: completed/failed older than 90d
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = {t[0] for t in tables}

    if "tasks" in table_names:
        n = _count_and_delete(
            "tasks",
            "status IN ('completed','failed') AND created_at < ?",
            (cutoff_90,)
        )
        results["tasks_removed"] = n
        print(f"  tasks              removed={n} (completed/failed >90d)")

    if "checkpoints" in table_names:
        n = _count_and_delete(
            "checkpoints",
            "created_at < ?",
            (cutoff_90,)
        )
        results["checkpoints_removed"] = n
        print(f"  checkpoints        removed={n} (>90d)")

    if "macro_snapshots" in table_names:
        n = _count_and_delete(
            "macro_snapshots",
            "date < ?",
            (cutoff_90,)
        )
        results["macro_removed"] = n
        print(f"  macro_snapshots    removed={n} (>90d)")

    if "knowledge_base" in table_names:
        n = _count_and_delete(
            "knowledge_base",
            "created_at < ?",
            (cutoff_365,)
        )
        results["knowledge_base_removed"] = n
        print(f"  knowledge_base     removed={n} (>365d)")

    if not dry_run:
        conn.execute("VACUUM")
        conn.commit()
    conn.close()

    return results


def prune_reports(dry_run: bool = False) -> dict:
    print("\n[REPORTS] Pruning old reports...")
    results = {}
    cutoff = date.today() - timedelta(days=DAILY_REPORTS_DAYS)

    # reports/daily/ — date-named JSON files
    daily_dir = REPO / "reports" / "daily"
    removed_daily = 0
    if daily_dir.exists():
        for f in daily_dir.glob("*.json"):
            try:
                file_date = date.fromisoformat(f.stem)
                if file_date < cutoff:
                    if not dry_run:
                        f.unlink()
                    removed_daily += 1
            except ValueError:
                pass  # non-date filenames left alone
    results["daily_reports_removed"] = removed_daily
    print(f"  reports/daily      removed={removed_daily} (>{DAILY_REPORTS_DAYS}d)")

    # triage_test.json — one-off test artifact, delete if >7d old
    triage = REPO / "reports" / "triage_test.json"
    if triage.exists():
        age = (datetime.now() - datetime.fromtimestamp(triage.stat().st_mtime)).days
        if age > 7:
            if not dry_run:
                triage.unlink()
            results["triage_test_removed"] = True
            print(f"  triage_test.json   removed (age={age}d)")

    # stale index files whose source document no longer exists
    index_dir = STORE / "index"
    removed_index = 0
    if index_dir.exists():
        doc_dir = STORE / "documents"
        for idx_file in index_dir.glob("*.json"):
            # index files named after document IDs; check if the source still exists
            source = doc_dir / (idx_file.stem + ".json")
            if not source.exists():
                if not dry_run:
                    idx_file.unlink()
                removed_index += 1
        results["stale_index_removed"] = removed_index
        if removed_index:
            print(f"  stale index files  removed={removed_index}")

    return results


# ── Summary ──────────────────────────────────────────────────────────────────

def run(logs: bool = True, db: bool = True, reports: bool = True, dry_run: bool = False):
    from datetime import date as _date
    tag = " [DRY RUN]" if dry_run else ""
    print(f"\n{'='*60}")
    print(f"  prune_memory — {_date.today().isoformat()}{tag}")
    print(f"{'='*60}")

    summary = {"dry_run": dry_run}

    if logs:
        summary["logs"] = prune_logs(dry_run)
    if db:
        summary["db"] = prune_db(dry_run)
    if reports:
        summary["reports"] = prune_reports(dry_run)

    total_removed = sum(
        v if isinstance(v, int) else 0
        for section in summary.values() if isinstance(section, dict)
        for v in section.values() if isinstance(v, int)
    )
    print(f"\n{'='*60}")
    print(f"  Total items pruned: {total_removed}{'  (dry run — nothing deleted)' if dry_run else ''}")

    _log_result({"type": "prune_memory_run", "dry_run": dry_run, "summary": summary, "total_removed": total_removed})
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Prune operational logs, DB rows, and old reports")
    parser.add_argument("--dry-run",  action="store_true", help="Show what would be deleted without deleting")
    parser.add_argument("--logs",     action="store_true")
    parser.add_argument("--db",       action="store_true")
    parser.add_argument("--reports",  action="store_true")
    args = parser.parse_args()

    all_mode = not (args.logs or args.db or args.reports)
    run(
        logs    = all_mode or args.logs,
        db      = all_mode or args.db,
        reports = all_mode or args.reports,
        dry_run = args.dry_run,
    )
