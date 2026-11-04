"""
DevilAdvocate — finds the strongest bear arguments for every BUY signal.

For each BUY ConfluenceSignal, queries the graph for bearish evidence:
  - Deteriorating metric trends (TrendSummary)
  - Price near 52W high (PriceSnapshot)
  - Declining confidence trajectory (belief_log)
  - Debt growth or margin compression (MetricValue)
  - Negative keywords in management statements (ManagementStatement)

Writes BearCase nodes to graph.db and adjusts the net conviction score.

No external API calls. Pure graph queries + heuristic scoring.

Usage:
  python -m agents.devil_advocate [--ticker SYMBOL] [--all] [--limit N]
"""
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"

_NEGATIVE_KEYWORDS = [
    "impairment", "write-off", "write-down", "default", "insolvency",
    "investigation", "fraud", "regulatory", "penalty", "lawsuit", "litigation",
    "resigned", "termination", "loss", "decline", "deteriorat", "concern",
    "headwind", "pressure", "slow", "weak", "uncertain", "challenge",
    "below expectation", "miss", "shortfall", "downgrade",
]


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


class DevilAdvocate:
    def analyze(self, ticker: str) -> dict:
        """
        Returns bear case analysis dict for a BUY signal ticker.
        {ticker, bull_score, bear_arguments, bear_score, net_conviction, verdict}
        """
        ticker = ticker.upper()
        bears  = []

        # ── Bull score from ConfluenceSignal ─────────────────────────────────
        with _conn() as c:
            sig = c.execute(
                """SELECT properties, confidence FROM nodes
                   WHERE node_type='ConfluenceSignal' AND ticker=?
                   ORDER BY created_at DESC LIMIT 1""",
                (ticker,)
            ).fetchone()

        if not sig:
            return {"ticker": ticker, "error": "no_confluence_signal"}

        props       = json.loads(sig["properties"]) if sig["properties"] else {}
        bull_score  = sig["confidence"]
        entry_price = props.get("close")
        year_low    = props.get("year_low")
        near_low    = props.get("near_low_pct", 0)

        # ── Bear 1: deteriorating metric trends ───────────────────────────────
        with _conn() as c:
            trends = c.execute(
                """SELECT properties FROM nodes
                   WHERE node_type='TrendSummary' AND ticker=?""",
                (ticker,)
            ).fetchall()

        deteriorating = []
        for t in trends:
            try:
                tp = json.loads(t["properties"]) if t["properties"] else {}
                if tp.get("direction") == "deteriorating":
                    metric = tp.get("metric", "unknown")
                    periods = tp.get("consistent_periods", 1)
                    delta   = tp.get("latest_delta_pct", 0)
                    deteriorating.append(f"{metric} declining {periods} periods ({delta:+.1f}%)")
            except Exception:
                pass

        if deteriorating:
            severity = "high" if len(deteriorating) >= 3 else "medium"
            bears.append({
                "reason":     "Deteriorating fundamentals",
                "severity":   severity,
                "data_point": "; ".join(deteriorating[:3]),
                "weight":     0.15 * min(len(deteriorating), 3),
            })

        # ── Bear 2: price near 52W high (momentum fading risk) ────────────────
        with _conn() as c:
            ps = c.execute(
                """SELECT properties FROM nodes
                   WHERE node_type='PriceSnapshot' AND ticker=?
                   ORDER BY created_at DESC LIMIT 1""",
                (ticker,)
            ).fetchone()

        if ps:
            try:
                psp      = json.loads(ps["properties"]) if ps["properties"] else {}
                high52w  = psp.get("year_high")
                close    = psp.get("close")
                if high52w and close and high52w > 0:
                    pct_from_high = (high52w - close) / high52w * 100
                    if pct_from_high < 5:
                        bears.append({
                            "reason":     "Price near 52W high — limited upside, breakout risk",
                            "severity":   "medium",
                            "data_point": f"{pct_from_high:.1f}% below 52W high of ₹{high52w}",
                            "weight":     0.12,
                        })
                    elif pct_from_high < 10:
                        bears.append({
                            "reason":     "Price within 10% of 52W high",
                            "severity":   "low",
                            "data_point": f"{pct_from_high:.1f}% below 52W high of ₹{high52w}",
                            "weight":     0.05,
                        })
            except Exception:
                pass

        # ── Bear 3: declining conviction (belief_log trajectory) ──────────────
        with _conn() as c:
            bl = c.execute(
                """SELECT old_conf, new_conf, reason FROM belief_log
                   WHERE ticker=? AND node_type='ConfluenceSignal'
                   ORDER BY created_at DESC LIMIT 10""",
                (ticker,)
            ).fetchall()

        if bl:
            drops   = [r for r in bl if r["new_conf"] < r["old_conf"]]
            rises   = [r for r in bl if r["new_conf"] > r["old_conf"]]
            net_dir = len(rises) - len(drops)
            if net_dir < 0:
                total_drop = sum(r["old_conf"] - r["new_conf"] for r in drops)
                bears.append({
                    "reason":     "Declining belief trajectory",
                    "severity":   "medium" if total_drop > 0.05 else "low",
                    "data_point": f"{len(drops)} drops vs {len(rises)} rises in recent log",
                    "weight":     min(0.10, total_drop * 0.5),
                })

        # ── Bear 4: debt growth or margin compression (MetricValue) ───────────
        with _conn() as c:
            mvs = c.execute(
                """SELECT properties FROM nodes
                   WHERE node_type='MetricValue' AND ticker=?
                   ORDER BY created_at DESC LIMIT 20""",
                (ticker,)
            ).fetchall()

        debt_flag   = False
        margin_flag = False
        for mv in mvs:
            try:
                mp = json.loads(mv["properties"]) if mv["properties"] else {}
                metric = (mp.get("metric") or "").lower()
                val    = mp.get("value")
                delta  = mp.get("delta_pct")

                if "debt" in metric and delta and delta > 20:
                    debt_flag = True
                if "margin" in metric and delta and delta < -5:
                    margin_flag = True
            except Exception:
                pass

        if debt_flag:
            bears.append({
                "reason":     "Debt growing significantly",
                "severity":   "high",
                "data_point": "Debt growth >20% detected in recent quarters",
                "weight":     0.15,
            })
        if margin_flag:
            bears.append({
                "reason":     "Margin compression",
                "severity":   "medium",
                "data_point": "Margin declining >5% in recent quarters",
                "weight":     0.10,
            })

        # ── Bear 5: negative management keywords ──────────────────────────────
        with _conn() as c:
            mss = c.execute(
                """SELECT properties FROM nodes
                   WHERE node_type='ManagementStatement' AND ticker=?
                   ORDER BY created_at DESC LIMIT 10""",
                (ticker,)
            ).fetchall()

        neg_hits = []
        for ms in mss:
            try:
                mp   = json.loads(ms["properties"]) if ms["properties"] else {}
                text = (mp.get("statement") or "").lower()
                hits = [kw for kw in _NEGATIVE_KEYWORDS if kw in text]
                if hits:
                    neg_hits.extend(hits[:2])
            except Exception:
                pass

        if neg_hits:
            unique_hits = list(dict.fromkeys(neg_hits))[:5]
            severity    = "high" if len(unique_hits) >= 3 else "medium"
            bears.append({
                "reason":     "Negative management language detected",
                "severity":   severity,
                "data_point": ", ".join(unique_hits),
                "weight":     0.08 * min(len(unique_hits), 3),
            })

        # ── Compute bear score ────────────────────────────────────────────────
        bear_score     = min(0.5, sum(b["weight"] for b in bears))
        net_conviction = round(bull_score - bear_score, 3)

        if net_conviction >= 0.55:
            verdict = "BUY"
        elif net_conviction >= 0.40:
            verdict = "HOLD"
        else:
            verdict = "WAIT"

        result = {
            "ticker":          ticker,
            "bull_score":      round(bull_score, 3),
            "bear_arguments":  bears,
            "bear_score":      round(bear_score, 3),
            "net_conviction":  net_conviction,
            "verdict":         verdict,
            "analyzed_at":     datetime.now().isoformat(),
        }

        # Write BearCase node
        try:
            import sys
            sys.path.insert(0, str(REPO))
            from memory.graph_store import get_store
            gs = get_store()
            gs.upsert_node(
                node_type="BearCase",
                name=f"BearCase_{ticker}_{date.today()}",
                ticker=ticker,
                properties={k: v for k, v in result.items() if k != "ticker"}
            )
        except Exception:
            pass

        return result

    def run_all(self, limit: int = 50) -> list[dict]:
        """Run devil's advocate on all BUY ConfluenceSignal tickers."""
        with _conn() as c:
            tickers = [
                r["ticker"] for r in c.execute(
                    """SELECT DISTINCT ticker FROM nodes
                       WHERE node_type='ConfluenceSignal'
                       AND json_extract(properties,'$.type') LIKE '%BUY%'
                       AND ticker IS NOT NULL
                       ORDER BY confidence DESC LIMIT ?""",
                    (limit,)
                ).fetchall()
            ]

        results = []
        for t in tickers:
            try:
                results.append(self.analyze(t))
            except Exception as e:
                results.append({"ticker": t, "error": str(e)})
        return results


if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))
    p = argparse.ArgumentParser(description="DevilAdvocate — bear case analysis")
    p.add_argument("--ticker", help="Single ticker to analyze")
    p.add_argument("--all",    action="store_true")
    p.add_argument("--limit",  type=int, default=20)
    args = p.parse_args()

    da = DevilAdvocate()
    if args.ticker:
        r = da.analyze(args.ticker)
        print(json.dumps(r, indent=2, default=str))
    elif args.all:
        results = da.run_all(limit=args.limit)
        print(f"Analyzed {len(results)} tickers")
        for r in sorted(results, key=lambda x: x.get("net_conviction", 0)):
            bears = len(r.get("bear_arguments", []))
            print(f"  {r['ticker']:12s} bull={r.get('bull_score','?')} "
                  f"bear={r.get('bear_score','?')} net={r.get('net_conviction','?')} "
                  f"verdict={r.get('verdict','?')} bears={bears}")
    else:
        p.print_help()
