#!/usr/bin/env python3
"""
Graph cleanup job — removes stale nodes that grow unboundedly over time.

What it prunes:
  PriceSnapshot       nodes older than PRICE_KEEP_DAYS (default 90)
  DeliveryMetric      nodes older than DELIVERY_KEEP_DAYS (default 90)
  ParticipantOI       nodes older than PARTOI_KEEP_DAYS (default 90)
  SurveillanceFlag    nodes older than SURV_KEEP_DAYS (default 180)
  InsiderTrade        nodes older than INSIDER_KEEP_DAYS (default 365)
  PledgeData          nodes older than PLEDGE_KEEP_DAYS (default 365)
  SASTFiling          nodes older than SAST_KEEP_DAYS (default 365)
  CorporateAction     nodes older than CORPACTION_KEEP_DAYS (default 365)
  TrendSummary        duplicates — keeps only the latest per (ticker, metric)

  Durable nodes (never pruned):
    MetricValue, ManagementStatement, Gap, Patch, Company, Filing, FinancialResult

Raw data for pruned nodes is preserved in memory_store/ flat files.

Runs: python jobs/cleanup_graph.py
      or import and call cleanup() from daily_monitor or a scheduled trigger.
"""
import sys
import json
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

PRICE_KEEP_DAYS      = 90
DELIVERY_KEEP_DAYS   = 90
PARTOI_KEEP_DAYS     = 90
SURV_KEEP_DAYS       = 180
INSIDER_KEEP_DAYS    = 365
PLEDGE_KEEP_DAYS     = 365
SAST_KEEP_DAYS       = 365
CORPACTION_KEEP_DAYS = 365


def _delete_old_by_property(
    conn,
    node_type: str,
    prop_key: str,       # JSON property that holds the date
    cutoff: str,         # YYYY-MM-DD
    dry_run: bool,
) -> int:
    """Delete nodes of node_type where properties->prop_key < cutoff. Returns count."""
    ids = [
        row[0] for row in conn.execute(
            f"SELECT id FROM nodes "
            f"WHERE node_type=? AND json_extract(properties,'$.{prop_key}') < ?",
            (node_type, cutoff),
        ).fetchall()
    ]
    if not dry_run and ids:
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM edges WHERE source_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM edges WHERE target_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM nodes WHERE id IN ({placeholders})", ids)
        conn.commit()
    return len(ids)


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

        # ── 2. Time-boxed new node types ───────────────────────────────────
        cutoff_delivery   = (date.today() - timedelta(days=DELIVERY_KEEP_DAYS)).isoformat()
        cutoff_partoi     = (date.today() - timedelta(days=PARTOI_KEEP_DAYS)).isoformat()
        cutoff_surv       = (date.today() - timedelta(days=SURV_KEEP_DAYS)).isoformat()
        cutoff_insider    = (date.today() - timedelta(days=INSIDER_KEEP_DAYS)).isoformat()
        cutoff_pledge     = (date.today() - timedelta(days=PLEDGE_KEEP_DAYS)).isoformat()
        cutoff_sast       = (date.today() - timedelta(days=SAST_KEEP_DAYS)).isoformat()
        cutoff_corpaction = (date.today() - timedelta(days=CORPACTION_KEEP_DAYS)).isoformat()

        stats["delivery_deleted"]    = _delete_old_by_property(conn, "DeliveryMetric",   "trade_date",   cutoff_delivery,   dry_run)
        stats["partoi_deleted"]      = _delete_old_by_property(conn, "ParticipantOI",    "trade_date",   cutoff_partoi,     dry_run)
        stats["surv_deleted"]        = _delete_old_by_property(conn, "SurveillanceFlag", "trade_date",   cutoff_surv,       dry_run)
        stats["insider_deleted"]     = _delete_old_by_property(conn, "InsiderTrade",     "trade_date",   cutoff_insider,    dry_run)
        stats["pledge_deleted"]      = _delete_old_by_property(conn, "PledgeData",       "record_date",  cutoff_pledge,     dry_run)
        stats["sast_deleted"]        = _delete_old_by_property(conn, "SASTFiling",       "filing_date",  cutoff_sast,       dry_run)
        stats["corpaction_deleted"]  = _delete_old_by_property(conn, "CorporateAction",  "action_date",  cutoff_corpaction, dry_run)

        # ── 4. TrendSummary duplicates — keep newest per (ticker, metric) ──
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

        # ── 5. Report remaining counts ──────────────────────────────────────
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
    print(f"{prefix}[cleanup_graph] DeliveryMetric deleted: {stats['delivery_deleted']}  ParticipantOI: {stats['partoi_deleted']}  SurveillanceFlag: {stats['surv_deleted']}")
    print(f"{prefix}[cleanup_graph] InsiderTrade: {stats['insider_deleted']}  PledgeData: {stats['pledge_deleted']}  SAST: {stats['sast_deleted']}  CorporateAction: {stats['corpaction_deleted']}")
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
