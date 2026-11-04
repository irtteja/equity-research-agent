"""
Proactive alert engine — scans graph.db and fires AlertEvent nodes when key
heuristic/rule-based conditions are met.

Alert types (all rule-based, NO Claude API):
  1. price_near_52w_low        — high    TechnicalSignal.pos_52w < 0.10 AND BUY_CONFLUENCE
  2. conviction_drop           — medium  conviction_history: score dropped from ≥0.60 to <0.55
  3. quality_vs_price_diverge  — medium  BusinessQuality=high AND pos_52w < 0.20
  4. peer_pe_discount          — medium  PeerComparison.vs_peer_avg.pe_premium_pct < -20
  5. management_tone_caution   — low     ConcallInsight.tone=defensive AND BUY_CONFLUENCE
  6. strong_buy_conviction      — info    score ≥ 0.70 AND quality_score ≥ 0.70

AlertEvent nodes are upserted into graph.db nodes table with
  node_type='AlertEvent', name='{ticker}_AlertEvent_{alert_type}_{date}'.
One alert per type per ticker per day (deduplication by name).

Usage:
  python memory/alert_engine.py              # run all, limit=200
  python memory/alert_engine.py --ticker IGL # single ticker
  python memory/alert_engine.py --limit 50   # cap tickers scanned
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path

REPO = Path(__file__).parent.parent
DB   = REPO / "memory_store" / "graph.db"


# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB), timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _node_props(conn, node_type: str, ticker: str) -> dict | None:
    """Return properties dict for the latest node of (node_type, ticker), or None."""
    row = conn.execute(
        "SELECT properties FROM nodes WHERE node_type=? AND ticker=? "
        "ORDER BY created_at DESC LIMIT 1",
        (node_type, ticker.upper()),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return {}


def _already_fired(conn, ticker: str, alert_type: str, today: str) -> bool:
    """True if an AlertEvent of this type was already written for this ticker today."""
    name = f"{ticker}_AlertEvent_{alert_type}_{today}"
    row = conn.execute(
        "SELECT id FROM nodes WHERE node_type='AlertEvent' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _write_alert(conn, ticker: str, alert_type: str, severity: str,
                 message: str, data: dict, today: str):
    """
    Upsert an AlertEvent node.  Uses MD5 of 'AlertEvent:{name}' for the id,
    matching the pattern used by graph_store.upsert_node().
    """
    import hashlib
    name    = f"{ticker}_AlertEvent_{alert_type}_{today}"
    node_id = hashlib.md5(f"AlertEvent:{name}".encode()).hexdigest()[:16]
    props   = {
        "alert_type":   alert_type,
        "severity":     severity,
        "message":      message,
        "ticker":       ticker,
        "triggered_at": datetime.now().isoformat(),
        "data":         data,
    }
    existing = conn.execute("SELECT id FROM nodes WHERE id=?", (node_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE nodes SET properties=? WHERE id=?",
            (json.dumps(props, default=str), node_id),
        )
    else:
        conn.execute(
            "INSERT INTO nodes (id, node_type, name, ticker, properties, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (node_id, "AlertEvent", name, ticker.upper(),
             json.dumps(props, default=str), datetime.now().isoformat()),
        )
    conn.commit()


# ── Individual alert conditions ───────────────────────────────────────────────

def _check_price_near_52w_low(ticker: str, conn, today: str) -> dict | None:
    """
    Alert: TechnicalSignal.pos_52w < 0.10 (< 10th percentile) AND BUY_CONFLUENCE.
    pos_52w is stored as a 0-1 decimal; threshold 0.10 = 10th percentile.
    """
    ts = _node_props(conn, "TechnicalSignal", ticker)
    if not ts:
        return None
    pos_52w = ts.get("pos_52w")
    if pos_52w is None or pos_52w >= 0.10:
        return None

    cs = _node_props(conn, "ConfluenceSignal", ticker)
    if not cs or cs.get("type") != "BUY_CONFLUENCE":
        return None

    percentile = round(pos_52w * 100, 1)
    return {
        "alert_type": "price_near_52w_low",
        "severity":   "high",
        "message":    (
            f"{ticker} is at {percentile:.0f}th percentile of 52W range "
            f"— potential entry near low"
        ),
        "data": {"pos_52w": pos_52w, "pos_52w_pct": percentile,
                 "confluence_score": cs.get("score")},
    }


def _check_conviction_drop(ticker: str, conn, today: str) -> dict | None:
    """
    Alert: conviction_history last 2 records — score dropped from >= 0.60 to < 0.55.
    Requires at least 2 historical records; skips if only 1 (first day).
    """
    rows = conn.execute(
        "SELECT conviction_score FROM conviction_history "
        "WHERE ticker=? ORDER BY record_date DESC LIMIT 2",
        (ticker.upper(),),
    ).fetchall()
    if len(rows) < 2:
        return None

    curr_score = rows[0][0]
    prev_score = rows[1][0]

    if curr_score is None or prev_score is None:
        return None
    if curr_score >= 0.55 or prev_score < 0.60:
        return None

    return {
        "alert_type": "conviction_drop",
        "severity":   "medium",
        "message":    (
            f"{ticker} conviction dropped from {prev_score*100:.0f}% "
            f"to {curr_score*100:.0f}%"
        ),
        "data": {"prev_score": prev_score, "curr_score": curr_score},
    }


def _check_quality_vs_price_divergence(ticker: str, conn, today: str) -> dict | None:
    """
    Alert: BusinessQuality.overall_quality = 'high' AND TechnicalSignal.pos_52w < 0.20.
    High-quality business trading near 52W lows — potential quality-at-discount entry.
    """
    bq = _node_props(conn, "BusinessQuality", ticker)
    if not bq or bq.get("overall_quality") != "high":
        return None

    ts = _node_props(conn, "TechnicalSignal", ticker)
    if not ts:
        return None
    pos_52w = ts.get("pos_52w")
    if pos_52w is None or pos_52w >= 0.20:
        return None

    percentile = round(pos_52w * 100, 1)
    return {
        "alert_type": "quality_vs_price_divergence",
        "severity":   "medium",
        "message":    (
            f"{ticker} high-quality business at {percentile:.0f}th percentile "
            f"of 52W range — quality at a discount"
        ),
        "data": {
            "pos_52w": pos_52w,
            "pos_52w_pct": percentile,
            "overall_quality": bq.get("overall_quality"),
            "quality_score": bq.get("quality_score"),
        },
    }


def _check_peer_pe_discount(ticker: str, conn, today: str) -> dict | None:
    """
    Alert: PeerComparison.vs_peer_avg.pe_premium_pct < -20
    (trading 20%+ P/E discount to sector peers).
    """
    pc = _node_props(conn, "PeerComparison", ticker)
    if not pc:
        return None
    vs_avg = pc.get("vs_peer_avg") or {}
    pe_premium = vs_avg.get("pe_premium_pct")
    if pe_premium is None or pe_premium >= -20:
        return None

    return {
        "alert_type": "peer_pe_discount",
        "severity":   "medium",
        "message":    (
            f"{ticker} trades at {abs(pe_premium):.0f}% P/E discount "
            f"to sector peers"
        ),
        "data": {
            "pe_premium_pct": pe_premium,
            "peer_avg_pe": vs_avg.get("peer_avg_pe"),
            "peer_verdict": pc.get("peer_verdict"),
        },
    }


def _check_management_tone_caution(ticker: str, conn, today: str) -> dict | None:
    """
    Alert: ConcallInsight.tone = 'defensive' AND BUY_CONFLUENCE.
    Management defensive tone while signal is BUY — monitor guidance closely.
    """
    ci = _node_props(conn, "ConcallInsight", ticker)
    if not ci or ci.get("tone") != "defensive":
        return None

    cs = _node_props(conn, "ConfluenceSignal", ticker)
    if not cs or cs.get("type") != "BUY_CONFLUENCE":
        return None

    return {
        "alert_type": "management_tone_caution",
        "severity":   "low",
        "message":    (
            f"{ticker} management tone is defensive "
            f"— monitor guidance closely"
        ),
        "data": {
            "tone": ci.get("tone"),
            "guidance": (ci.get("guidance") or [])[:3],
            "confluence_score": cs.get("score"),
        },
    }


def _check_strong_buy_conviction(ticker: str, conn, today: str) -> dict | None:
    """
    Alert: ConfluenceSignal.score >= 0.70 AND BusinessQuality.quality_score >= 0.70.
    High-conviction BUY with high business quality.
    """
    cs = _node_props(conn, "ConfluenceSignal", ticker)
    if not cs or cs.get("type") != "BUY_CONFLUENCE":
        return None
    conf = cs.get("score") or 0
    if conf < 0.70:
        return None

    bq = _node_props(conn, "BusinessQuality", ticker)
    if not bq:
        return None
    quality = bq.get("quality_score") or 0
    if quality < 0.70:
        return None

    return {
        "alert_type": "strong_buy_conviction",
        "severity":   "info",
        "message":    (
            f"{ticker} high-conviction BUY with high business quality "
            f"(conf={conf*100:.0f}%, quality={quality:.2f})"
        ),
        "data": {
            "confluence_score": conf,
            "quality_score": quality,
            "overall_quality": bq.get("overall_quality"),
        },
    }


# ── AlertEngine ───────────────────────────────────────────────────────────────

_CHECKS = [
    _check_price_near_52w_low,
    _check_conviction_drop,
    _check_quality_vs_price_divergence,
    _check_peer_pe_discount,
    _check_management_tone_caution,
    _check_strong_buy_conviction,
]


class AlertEngine:
    """
    Scans graph.db for alert conditions and writes AlertEvent nodes.

    One AlertEvent per (ticker, alert_type, date) — deduplication prevents
    duplicate alerts on repeated runs within the same day.
    """

    def scan_ticker(self, ticker: str) -> list[dict]:
        """
        Evaluate all 6 conditions for one ticker.
        Returns list of alert dicts that passed their conditions
        (whether or not they were written — caller can check 'written' key).
        """
        today = date.today().isoformat()
        alerts = []
        try:
            with _conn() as conn:
                for check_fn in _CHECKS:
                    try:
                        result = check_fn(ticker, conn, today)
                        if result is None:
                            continue
                        alert_type = result["alert_type"]
                        if _already_fired(conn, ticker, alert_type, today):
                            result["written"] = False
                            result["skip_reason"] = "already_fired_today"
                        else:
                            _write_alert(
                                conn, ticker, alert_type,
                                result["severity"], result["message"],
                                result["data"], today,
                            )
                            result["written"] = True
                        alerts.append(result)
                    except Exception as e:
                        alerts.append({
                            "alert_type": getattr(check_fn, "__name__", "unknown"),
                            "ticker": ticker,
                            "error": str(e),
                            "written": False,
                        })
        except Exception as e:
            alerts.append({"ticker": ticker, "error": str(e), "written": False})
        return alerts

    def run_all(self, limit: int = 200) -> dict:
        """
        Scan all ConfluenceSignal tickers for alert conditions.

        Returns summary dict:
          {
            "fired": int,         # alerts written
            "skipped": int,       # already fired today
            "errors": int,        # check exceptions
            "per_type": {...},    # counts by alert_type
            "tickers_scanned": int,
          }
        """
        today = date.today().isoformat()
        fired = skipped = errors = 0
        per_type: dict[str, int] = {}

        try:
            with _conn() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT ticker FROM nodes "
                    "WHERE node_type='ConfluenceSignal' AND ticker IS NOT NULL "
                    "AND ticker != '' LIMIT ?",
                    (limit,),
                ).fetchall()
                tickers = [r[0] for r in rows]
        except Exception as e:
            return {"fired": 0, "skipped": 0, "errors": 1,
                    "per_type": {}, "tickers_scanned": 0,
                    "fatal": str(e)}

        for ticker in tickers:
            alerts = self.scan_ticker(ticker)
            for a in alerts:
                if "error" in a and "alert_type" in a and a.get("written") is False \
                        and a.get("skip_reason") is None:
                    errors += 1
                elif a.get("written"):
                    fired += 1
                    atype = a.get("alert_type", "unknown")
                    per_type[atype] = per_type.get(atype, 0) + 1
                elif not a.get("written") and a.get("skip_reason") == "already_fired_today":
                    skipped += 1

        return {
            "fired": fired,
            "skipped": skipped,
            "errors": errors,
            "per_type": per_type,
            "tickers_scanned": len(tickers),
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(
        description="Proactive alert engine — 6 rule-based alert types"
    )
    parser.add_argument("--ticker", metavar="TICKER",
                        help="Scan a single ticker instead of all")
    parser.add_argument("--limit", type=int, default=200,
                        help="Max tickers to scan (default 200)")
    args = parser.parse_args()

    engine = AlertEngine()

    if args.ticker:
        ticker = args.ticker.upper()
        print(f"\nScanning {ticker}...\n")
        alerts = engine.scan_ticker(ticker)
        if not alerts:
            print("  No alerts triggered.")
        for a in alerts:
            status = "FIRED" if a.get("written") else \
                     ("SKIP" if a.get("skip_reason") else "ERROR")
            print(f"  [{status}] {a.get('alert_type','?')} ({a.get('severity','?')})")
            if a.get("message"):
                print(f"         {a['message']}")
            if a.get("error"):
                print(f"         ERROR: {a['error']}")
    else:
        print(f"\nRunning AlertEngine.run_all(limit={args.limit})...\n")
        result = engine.run_all(limit=args.limit)
        print(f"  Tickers scanned : {result['tickers_scanned']}")
        print(f"  Alerts fired    : {result['fired']}")
        print(f"  Already fired   : {result['skipped']}")
        print(f"  Errors          : {result['errors']}")
        print()
        print("  Alerts per type:")
        for atype, count in sorted(result.get("per_type", {}).items()):
            print(f"    {atype:40s} {count}")
        if not result["per_type"]:
            print("    (none)")

        # Individual tests for RELIANCE and IGL
        for test_ticker in ("RELIANCE", "IGL"):
            print(f"\n  --- {test_ticker} detail ---")
            alerts = engine.scan_ticker(test_ticker)
            if not alerts:
                print("    No alerts triggered.")
            for a in alerts:
                status = "FIRED" if a.get("written") else \
                         ("SKIP" if a.get("skip_reason") else "ERROR")
                print(f"    [{status}] {a.get('alert_type','?')} ({a.get('severity','?')})")
                if a.get("message"):
                    print(f"           {a['message']}")


if __name__ == "__main__":
    _cli()
