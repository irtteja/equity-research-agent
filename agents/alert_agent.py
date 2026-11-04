"""
AlertAgent — monitors belief_log and ConfluenceSignal nodes for meaningful state changes.

Detects:
  signal_flip_bear  — ConfluenceSignal dropped to contested/retracted
  signal_flip_bull  — ConfluenceSignal recovered from contested
  new_buy_signal    — new BUY ConfluenceSignal formed
  conviction_surge  — confidence jumped >0.10 in one cycle
  conviction_drop   — confidence dropped >0.10 in one cycle
  stale_signal      — BUY signal not updated in >7 days

Writes AlertEvent nodes to graph.db.
Called by jobs/orchestrator.py run_synthesis_phase().

Usage:
  python -m agents.alert_agent [--run] [--list] [--hours N]
"""
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


class AlertAgent:
    ALERT_TYPES = {
        "signal_flip_bear": "ConfluenceSignal dropped to contested/retracted",
        "signal_flip_bull": "ConfluenceSignal recovered from contested",
        "new_buy_signal":   "New BUY ConfluenceSignal formed",
        "conviction_surge": "Confidence jumped >0.10 in one cycle",
        "conviction_drop":  "Confidence dropped >0.10 in one cycle",
        "stale_signal":     "BUY signal not updated in >7 days",
    }

    def run(self, hours_back: int = 3) -> dict:
        """
        Scan recent belief_log and signal states for alerts.
        Returns summary dict.
        """
        alerts = []
        cutoff = (datetime.now() - timedelta(hours=hours_back)).isoformat()

        # ── 1. belief_log-driven alerts ──────────────────────────────────────
        with _conn() as c:
            log_rows = c.execute(
                """SELECT ticker, node_type, old_conf, new_conf, old_state, new_state, reason, created_at
                   FROM belief_log
                   WHERE node_type='ConfluenceSignal' AND created_at >= ?
                   ORDER BY created_at DESC""",
                (cutoff,)
            ).fetchall()

        for r in log_rows:
            delta = r["new_conf"] - r["old_conf"]
            ticker = r["ticker"]
            if not ticker:
                continue

            # State flip: forming/confirmed → contested/retracted
            if r["new_state"] in ("contested", "retracted") and r["old_state"] not in ("contested", "retracted"):
                alerts.append(self._make_alert(
                    ticker, "signal_flip_bear",
                    f"{ticker} signal dropped to {r['new_state']} (conf {r['old_conf']:.3f}→{r['new_conf']:.3f})",
                    "critical"
                ))

            # State flip: contested → forming/confirmed
            elif r["old_state"] in ("contested", "retracted") and r["new_state"] not in ("contested", "retracted"):
                alerts.append(self._make_alert(
                    ticker, "signal_flip_bull",
                    f"{ticker} signal recovered to {r['new_state']} (conf {r['old_conf']:.3f}→{r['new_conf']:.3f})",
                    "warning"
                ))

            # Large conviction moves
            elif delta >= 0.10:
                alerts.append(self._make_alert(
                    ticker, "conviction_surge",
                    f"{ticker} conviction surged +{delta:.3f} ({r['reason']})",
                    "info"
                ))
            elif delta <= -0.10:
                alerts.append(self._make_alert(
                    ticker, "conviction_drop",
                    f"{ticker} conviction dropped {delta:.3f} ({r['reason']})",
                    "warning"
                ))

        # ── 2. New BUY signals (created in last hours_back) ──────────────────
        with _conn() as c:
            new_buys = c.execute(
                """SELECT ticker, properties, created_at FROM nodes
                   WHERE node_type='ConfluenceSignal'
                   AND created_at >= ?
                   AND json_extract(properties,'$.type') LIKE '%BUY%'""",
                (cutoff,)
            ).fetchall()

        for r in new_buys:
            # Check if this ticker already had a signal (not truly new)
            with _conn() as c:
                older = c.execute(
                    """SELECT COUNT(*) FROM nodes
                       WHERE node_type='ConfluenceSignal' AND ticker=?
                       AND created_at < ?""",
                    (r["ticker"], cutoff)
                ).fetchone()[0]
            if older == 0:
                alerts.append(self._make_alert(
                    r["ticker"], "new_buy_signal",
                    f"New BUY signal for {r['ticker']}",
                    "info"
                ))

        # ── 3. Stale BUY signals (not updated in >7 days) ───────────────────
        stale_cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        with _conn() as c:
            stale = c.execute(
                """SELECT ticker, updated_at FROM nodes
                   WHERE node_type='ConfluenceSignal'
                   AND json_extract(properties,'$.type') LIKE '%BUY%'
                   AND (updated_at IS NULL OR updated_at < ?)""",
                (stale_cutoff,)
            ).fetchall()

        for r in stale[:20]:  # cap to avoid alert flood
            alerts.append(self._make_alert(
                r["ticker"], "stale_signal",
                f"{r['ticker']} BUY signal not refreshed in >7 days",
                "warning"
            ))

        # Write all alerts to graph.db
        written = 0
        for a in alerts:
            try:
                self._write_alert(**a)
                written += 1
            except Exception:
                pass

        return {
            "alerts_generated": len(alerts),
            "alerts_written":   written,
            "critical":  sum(1 for a in alerts if a["severity"] == "critical"),
            "warning":   sum(1 for a in alerts if a["severity"] == "warning"),
            "info":      sum(1 for a in alerts if a["severity"] == "info"),
        }

    def _make_alert(self, ticker, alert_type, message, severity) -> dict:
        return {
            "ticker":     ticker,
            "alert_type": alert_type,
            "message":    message,
            "severity":   severity,
        }

    def _write_alert(self, ticker: str, alert_type: str, message: str, severity: str):
        """Write AlertEvent node to graph.db."""
        try:
            import sys
            sys.path.insert(0, str(REPO))
            from memory.graph_store import get_store
            gs = get_store()
            gs.upsert_node(
                node_type="AlertEvent",
                name=f"Alert_{ticker}_{alert_type}_{date.today()}",
                ticker=ticker,
                properties={
                    "alert_type":   alert_type,
                    "message":      message,
                    "severity":     severity,
                    "triggered_at": datetime.now().isoformat(),
                }
            )
        except Exception:
            pass

    def get_active_alerts(self, hours_back: int = 24) -> list[dict]:
        cutoff = (datetime.now() - timedelta(hours=hours_back)).isoformat()
        with _conn() as c:
            rows = c.execute(
                """SELECT ticker, properties, created_at FROM nodes
                   WHERE node_type='AlertEvent' AND created_at >= ?
                   ORDER BY created_at DESC""",
                (cutoff,)
            ).fetchall()
        alerts = []
        for r in rows:
            try:
                props = json.loads(r["properties"]) if r["properties"] else {}
                props["ticker"]     = r["ticker"]
                props["created_at"] = r["created_at"]
                alerts.append(props)
            except Exception:
                pass
        return alerts

    def get_critical_alerts(self) -> list[dict]:
        with _conn() as c:
            rows = c.execute(
                """SELECT ticker, properties, created_at FROM nodes
                   WHERE node_type='AlertEvent'
                   AND json_extract(properties,'$.severity')='critical'
                   ORDER BY created_at DESC LIMIT 50"""
            ).fetchall()
        alerts = []
        for r in rows:
            try:
                props = json.loads(r["properties"]) if r["properties"] else {}
                props["ticker"]     = r["ticker"]
                props["created_at"] = r["created_at"]
                alerts.append(props)
            except Exception:
                pass
        return alerts


if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))
    p = argparse.ArgumentParser(description="AlertAgent")
    p.add_argument("--run",   action="store_true")
    p.add_argument("--list",  action="store_true")
    p.add_argument("--hours", type=int, default=24)
    args = p.parse_args()

    agent = AlertAgent()
    if args.run:
        result = agent.run(hours_back=args.hours)
        print(json.dumps(result, indent=2))
    elif args.list:
        alerts = agent.get_active_alerts(hours_back=args.hours)
        print(f"{len(alerts)} alerts in last {args.hours}h")
        for a in alerts[:20]:
            print(f"  [{a.get('severity','?'):8s}] {a.get('ticker','?'):12s} {a.get('alert_type','?'):20s} {a.get('message','')}")
    else:
        p.print_help()
