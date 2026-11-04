#!/usr/bin/env python3
"""
Graph cleanup job — removes stale nodes that grow unboundedly over time.

What it prunes:
  PriceSnapshot nodes older than PRICE_KEEP_DAYS (default 90)
    → also deletes the PRICE_OF edges attached to them
    → price history is preserved in memory_store/bhavcopy/ CSVs

  TrendSummary duplicates — keeps only the latest node per (ticker, metric)
    → old TrendSummary nodes accumulate because TrendAgent appends rather than upserts

Runs: python jobs/cleanup_graph.py
      or import and call cleanup() from daily_monitor or a scheduled trigger.
"""
import sys
import json
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

PRICE_KEEP_DAYS = 90   # keep last 90 days of PriceSnapshot nodes


def cleanup(dry_run: bool = False) -> dict:
    from memory.graph_store import get_store
    gs = get_store()

    cutoff = (date.today() - timedelta(days=PRICE_KEEP_DAYS)).isoformat()
    stats = {}

    with gs._conn() as conn:
        # ── 1. PriceSnapshot nodes older than cutoff ────────────────────────
        old_price_ids = [
            row[0] for row in conn.execute(
                "SELECT id FROM nodes "
                "WHERE node_type='PriceSnapshot' "
                "AND json_extract(properties,'$.date') < ?",
                (cutoff,),
            ).fetchall()
        ]

        if not dry_run and old_price_ids:
            # delete edges first (FK constraint)
            placeholders = ",".join("?" * len(old_price_ids))
            conn.execute(
                f"DELETE FROM edges WHERE source_id IN ({placeholders})",
                old_price_ids,
            )
            conn.execute(
                f"DELETE FROM nodes WHERE id IN ({placeholders})",
                old_price_ids,
            )
            conn.commit()

        stats["price_snapshots_deleted"] = len(old_price_ids)
        stats["price_cutoff"] = cutoff

        # ── 2. TrendSummary duplicates — keep newest per (ticker, metric) ──
        # Find all (ticker, metric) pairs that have more than one TrendSummary
        dupes = conn.execute(
            """
            SELECT ticker,
                   json_extract(properties,'$.metric') AS metric,
                   COUNT(*) AS cnt,
                   MAX(created_at) AS newest_at
            FROM nodes
            WHERE node_type = 'TrendSummary'
            GROUP BY ticker, json_extract(properties,'$.metric')
            HAVING COUNT(*) > 1
            """
        ).fetchall()

        stale_trend_ids = []
        for ticker, metric, cnt, newest_at in dupes:
            old_rows = conn.execute(
                """
                SELECT id FROM nodes
                WHERE node_type = 'TrendSummary'
                  AND ticker = ?
                  AND json_extract(properties,'$.metric') = ?
                  AND created_at < ?
                """,
                (ticker, metric, newest_at),
            ).fetchall()
            stale_trend_ids.extend(row[0] for row in old_rows)

        if not dry_run and stale_trend_ids:
            placeholders = ",".join("?" * len(stale_trend_ids))
            conn.execute(
                f"DELETE FROM edges WHERE source_id IN ({placeholders})",
                stale_trend_ids,
            )
            conn.execute(
                f"DELETE FROM nodes WHERE id IN ({placeholders})",
                stale_trend_ids,
            )
            conn.commit()

        stats["trend_summary_dupes_deleted"] = len(stale_trend_ids)

        # ── 3. Report remaining counts ──────────────────────────────────────
        stats["price_snapshots_remaining"] = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE node_type='PriceSnapshot'"
        ).fetchone()[0]
        stats["trend_summary_remaining"] = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE node_type='TrendSummary'"
        ).fetchone()[0]
        stats["total_nodes"] = conn.execute(
            "SELECT COUNT(*) FROM nodes"
        ).fetchone()[0]

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}[cleanup_graph] PriceSnapshot deleted: {stats['price_snapshots_deleted']} (cutoff {cutoff})")
    print(f"{prefix}[cleanup_graph] TrendSummary dupes deleted: {stats['trend_summary_dupes_deleted']}")
    print(f"{prefix}[cleanup_graph] Remaining → PriceSnapshot: {stats['price_snapshots_remaining']}, TrendSummary: {stats['trend_summary_remaining']}, total nodes: {stats['total_nodes']}")
    return stats


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    args = parser.parse_args()
    stats = cleanup(dry_run=args.dry_run)
    import json
    print(json.dumps(stats, indent=2))
