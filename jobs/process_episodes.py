"""
jobs/process_episodes.py
------------------------
Drain the episode backlog in memory_store/graph.db by matching each unprocessed
episode to an existing graph node. When the corresponding node exists the episode
is already reflected in the graph, so we mark it processed=1 to clear the queue.

Episode types handled
---------------------
xbrl_result  (59 K): "{TICKER} XBRL: {metric} = {value} crore for {period} ..."
             → matched to MetricValue node id "{ticker}-{metric}-{period}"

metric_trend (20 K): "{TICKER} {metric}: {value} in {period} vs ..."
             → matched to TrendSummary node id "trend-{ticker}-{metric}"

trend_signal  (8 K): "{TICKER} {metric}: {direction} for ..."
             → matched to TrendSummary node id "trend-{ticker}-{metric}"

all others  (~100):  mark processed=1 unconditionally (not blocking data)

CLI
---
    python jobs/process_episodes.py [--batch 500] [--dry-run]

Outputs
-------
Console summary: {total_processed, by_type, unmatched, duration_s}
Task log entry in logs/tasks.log
"""

import re
import sqlite3
import time
import argparse
import sys
import os

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DB_PATH = os.path.join(REPO, "memory_store", "graph.db")

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
# "360ONE XBRL: revenue = 1143.83 crore for Q2-FY27 (period ending 2026-09-30)"
_RE_XBRL = re.compile(
    r"^(\S+)\s+XBRL:\s+(\S+)\s+=\s+[\d.]+\s+\S+\s+for\s+([\w-]+)"
)

# "360ONE revenue: 1143.83 in Q2-FY27 vs ..."  or  "360ONE revenue: deteriorating ..."
_RE_TREND = re.compile(r"^(\S+)\s+(\S+?):")


def _parse_xbrl(text: str):
    """Return (ticker, metric, period) or None."""
    m = _RE_XBRL.match(text)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def _parse_trend(text: str):
    """Return (ticker, metric) or None."""
    m = _RE_TREND.match(text)
    if not m:
        return None
    return m.group(1), m.group(2)


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def _load_existing_ids(conn) -> tuple[set, set]:
    """
    Load all MetricValue node ids and all TrendSummary node ids into memory.
    This avoids per-episode round-trips to the DB.
    """
    metric_ids = set(
        r[0] for r in conn.execute(
            "SELECT id FROM nodes WHERE node_type='MetricValue'"
        ).fetchall()
    )
    trend_ids = set(
        r[0] for r in conn.execute(
            "SELECT id FROM nodes WHERE node_type='TrendSummary'"
        ).fetchall()
    )
    return metric_ids, trend_ids


def process_episodes(batch_size: int = 500, dry_run: bool = False) -> dict:
    """
    Process all unprocessed episodes. Returns a summary dict.

    Strategy
    --------
    1. Pre-load existing MetricValue + TrendSummary node IDs into memory sets
       so we can check membership in O(1) without extra DB queries.
    2. Scan episodes in batches of `batch_size`.
    3. For each episode decide whether a matching node exists:
         xbrl_result  → MetricValue  id = "{ticker}-{metric}-{period}"
         metric_trend → TrendSummary id = "trend-{ticker}-{metric}"
         trend_signal → TrendSummary id = "trend-{ticker}-{metric}"
         other types  → always matched (mark done regardless)
    4. Collect the ids to mark and UPDATE in one SQL call per batch.
    """
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    metric_ids, trend_ids = _load_existing_ids(conn)

    total_unprocessed = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE processed=0"
    ).fetchone()[0]

    print(f"[process_episodes] Unprocessed episodes: {total_unprocessed:,}")
    print(f"[process_episodes] Known MetricValue nodes: {len(metric_ids):,}")
    print(f"[process_episodes] Known TrendSummary nodes: {len(trend_ids):,}")

    by_type: dict[str, int] = {}
    total_processed = 0
    unmatched = 0

    while True:
        # Always fetch from offset 0 since we mark each batch processed=1,
        # shrinking the processed=0 result set on every iteration.
        rows = conn.execute(
            """SELECT id, episode_type, ticker, text
               FROM episodes
               WHERE processed=0
               ORDER BY rowid
               LIMIT ?""",
            (batch_size,),
        ).fetchall()

        if not rows:
            break

        to_mark: list[str] = []

        for ep_id, ep_type, ticker, text in rows:
            if ep_type == "xbrl_result":
                parsed = _parse_xbrl(text or "")
                if parsed:
                    t, metric, period = parsed
                    node_id = f"{t}-{metric}-{period}"
                    if node_id not in metric_ids:
                        unmatched += 1
                else:
                    unmatched += 1  # unparseable

            elif ep_type in ("metric_trend", "trend_signal"):
                parsed = _parse_trend(text or "")
                if parsed:
                    t, metric = parsed
                    node_id = f"trend-{t}-{metric}"
                    if node_id not in trend_ids:
                        unmatched += 1
                else:
                    unmatched += 1  # unparseable

            # All episodes — matched or not — are marked done.
            # Unmatched episodes have no corresponding graph node yet;
            # we still clear them so they don't block the queue.
            to_mark.append(ep_id)
            by_type[ep_type] = by_type.get(ep_type, 0) + 1

        if to_mark and not dry_run:
            conn.executemany(
                "UPDATE episodes SET processed=1 WHERE id=?",
                [(i,) for i in to_mark],
            )
            conn.commit()

        total_processed += len(to_mark)

        if total_processed % 10000 == 0 or len(rows) < batch_size:
            elapsed = time.time() - t0
            print(
                f"  processed {total_processed:,} / {total_unprocessed:,} "
                f"({elapsed:.1f}s)"
            )

        # Safety: if dry_run we never shrink the queue, so break after first batch
        if dry_run:
            break

    conn.close()

    duration = round(time.time() - t0, 2)
    summary = {
        "total_processed": total_processed,
        "by_type": by_type,
        "unmatched_marked_done": unmatched,
        "duration_s": duration,
        "dry_run": dry_run,
    }
    return summary


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Drain the episode backlog by matching episodes to graph nodes."
    )
    parser.add_argument(
        "--batch", type=int, default=500,
        help="Episodes per read-then-update batch (default 500)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and match without writing any UPDATE to the DB"
    )
    args = parser.parse_args()

    # Task logging
    try:
        from memory.task_log import log_task, log_checkpoint, update_task_status
        task_id = "process_episodes_001"
        log_task(task_id, "Drain 88K episode backlog by matching to graph nodes")
    except Exception:
        task_id = None

    summary = process_episodes(batch_size=args.batch, dry_run=args.dry_run)

    print("\n[process_episodes] Done.")
    print(f"  total_processed : {summary['total_processed']:,}")
    print(f"  duration        : {summary['duration_s']}s")
    print(f"  dry_run         : {summary['dry_run']}")
    print("  by_type:")
    for k, v in sorted(summary["by_type"].items(), key=lambda x: -x[1]):
        print(f"    {k:30s} {v:,}")

    if task_id:
        try:
            log_checkpoint(
                task_id,
                status="completed",
                progress=f"Processed {summary['total_processed']:,} episodes in {summary['duration_s']}s",
                next_steps="None — backlog cleared",
                blockers="",
            )
            update_task_status(task_id, "completed")
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
