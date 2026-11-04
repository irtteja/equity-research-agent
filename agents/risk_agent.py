"""
RiskAgent — portfolio-level and signal-quality risk checks.

Checks:
  sector_concentration()   — flags if >30% of BUY signals are in one sector
  signal_quality_score()   — scores signal reliability by age, belief state, bear case
  liquidity_check()        — flags tickers with low traded value or volume (<threshold)
  run()                    — runs all checks, writes RiskReport node to graph.db

No external API calls. Pure graph queries + heuristic scoring.

Usage:
  python -m agents.risk_agent [--run] [--ticker SYMBOL]
"""
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"

_MIN_TRADED_VALUE  = 5_000_000   # ₹50 lakh daily traded value minimum
_MIN_VOLUME        = 10_000      # shares
_SECTOR_CONC_LIMIT = 0.30        # 30% concentration triggers flag


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


class RiskAgent:
    def sector_concentration(self) -> dict:
        """
        Returns sector concentration analysis over active BUY signals.
        Flags sectors where count / total_signals > _SECTOR_CONC_LIMIT.
        """
        with _conn() as c:
            # Get all BUY ConfluenceSignals with their company sector
            rows = c.execute(
                """SELECT cs.ticker,
                          json_extract(co.properties,'$.sector') as sector
                   FROM nodes cs
                   LEFT JOIN nodes co ON co.node_type='Company' AND co.ticker=cs.ticker
                   WHERE cs.node_type='ConfluenceSignal'
                     AND json_extract(cs.properties,'$.type') LIKE '%BUY%'"""
            ).fetchall()

        total = len(rows)
        if total == 0:
            return {"total_signals": 0, "sectors": {}, "flags": []}

        sector_counts = {}
        unknown_tickers = []
        for r in rows:
            sec = r["sector"] or "Unknown"
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
            if not r["sector"]:
                unknown_tickers.append(r["ticker"])

        flags = []
        for sec, cnt in sector_counts.items():
            pct = cnt / total
            if pct > _SECTOR_CONC_LIMIT:
                flags.append({
                    "sector":    sec,
                    "count":     cnt,
                    "pct":       round(pct * 100, 1),
                    "severity":  "high" if pct > 0.50 else "medium",
                    "message":   f"{sec} accounts for {pct*100:.1f}% of BUY signals ({cnt}/{total})",
                })

        return {
            "total_signals":  total,
            "sectors":        {k: v for k, v in sorted(sector_counts.items(), key=lambda x: -x[1])},
            "flags":          flags,
            "unknown_sector": len(unknown_tickers),
        }

    def signal_quality_score(self, ticker: str = None) -> list[dict]:
        """
        Scores each BUY signal's quality [0-1] based on:
          - belief state (confirmed=1.0, forming=0.6, contested=0.2, retracted=0.0)
          - signal age (fresh < 3 days = bonus, stale > 14 days = penalty)
          - bear case present (bonus if BUY survives devil's advocate)
          - revision stability (fewer downs in belief_log = better)
        Returns list of {ticker, quality_score, state_score, age_score, bear_score, stability_score, flags}
        """
        with _conn() as c:
            q = """SELECT ticker, confidence, state, properties, created_at, updated_at
                   FROM nodes
                   WHERE node_type='ConfluenceSignal'
                     AND json_extract(properties,'$.type') LIKE '%BUY%'"""
            params = []
            if ticker:
                q += " AND ticker=?"
                params.append(ticker.upper())
            q += " ORDER BY confidence DESC"
            sigs = c.execute(q, params).fetchall()

        results = []
        today = date.today()

        for s in sigs:
            tick  = s["ticker"]
            conf  = s["confidence"] or 0.5
            state = s["state"] or "forming"
            flags = []

            # 1. Belief state score
            state_map = {"confirmed": 1.0, "forming": 0.6, "contested": 0.2, "retracted": 0.0}
            state_score = state_map.get(state, 0.5)

            # 2. Age score
            upd = s["updated_at"] or s["created_at"] or today.isoformat()
            try:
                upd_date = date.fromisoformat(upd[:10])
                age_days = (today - upd_date).days
            except Exception:
                age_days = 7
            if age_days <= 3:
                age_score = 1.0
            elif age_days <= 7:
                age_score = 0.8
            elif age_days <= 14:
                age_score = 0.5
            else:
                age_score = 0.2
                flags.append(f"stale ({age_days}d)")

            # 3. Bear case score — did it survive devil's advocate?
            with _conn() as c2:
                bc = c2.execute(
                    """SELECT properties FROM nodes
                       WHERE node_type='BearCase' AND ticker=?
                       ORDER BY created_at DESC LIMIT 1""",
                    (tick,)
                ).fetchone()

            if bc:
                try:
                    bcp = json.loads(bc["properties"]) if bc["properties"] else {}
                    net_conv  = bcp.get("net_conviction", 0)
                    verdict   = bcp.get("verdict", "")
                    bear_score = min(1.0, max(0.0, net_conv))
                    if verdict == "WAIT":
                        flags.append("bear:WAIT")
                except Exception:
                    bear_score = 0.5
            else:
                bear_score = 0.5  # neutral if no bear case run

            # 4. Revision stability
            with _conn() as c2:
                bl = c2.execute(
                    """SELECT old_conf, new_conf FROM belief_log
                       WHERE ticker=? AND node_type='ConfluenceSignal'
                       ORDER BY created_at DESC LIMIT 20""",
                    (tick,)
                ).fetchall()

            if bl:
                drops = sum(1 for r in bl if r["new_conf"] < r["old_conf"])
                rises = sum(1 for r in bl if r["new_conf"] > r["old_conf"])
                total_revisions = len(bl)
                stability_score = max(0.0, 1.0 - (drops / total_revisions) * 0.8)
                if drops > rises:
                    flags.append("declining_conviction")
            else:
                stability_score = 0.7  # no history → moderate score

            # Composite quality score (weighted average)
            quality = (
                state_score     * 0.35 +
                age_score       * 0.20 +
                bear_score      * 0.30 +
                stability_score * 0.15
            )

            results.append({
                "ticker":           tick,
                "quality_score":    round(quality, 3),
                "confidence":       round(conf, 3),
                "state":            state,
                "state_score":      round(state_score, 2),
                "age_score":        round(age_score, 2),
                "bear_score":       round(bear_score, 3),
                "stability_score":  round(stability_score, 3),
                "flags":            flags,
            })

        return sorted(results, key=lambda x: -x["quality_score"])

    def liquidity_check(self) -> dict:
        """
        Flags BUY signal tickers with insufficient traded value or volume.
        Uses most recent PriceSnapshot for traded_value / volume.
        """
        with _conn() as c:
            # Get all BUY signal tickers
            buy_tickers = [
                r["ticker"] for r in c.execute(
                    """SELECT DISTINCT ticker FROM nodes
                       WHERE node_type='ConfluenceSignal'
                         AND json_extract(properties,'$.type') LIKE '%BUY%'
                         AND ticker IS NOT NULL"""
                ).fetchall()
            ]

        low_liq = []
        ok_count = 0

        for tick in buy_tickers:
            with _conn() as c:
                ps = c.execute(
                    """SELECT properties FROM nodes
                       WHERE node_type='PriceSnapshot' AND ticker=?
                       ORDER BY created_at DESC LIMIT 1""",
                    (tick,)
                ).fetchone()
            if not ps:
                low_liq.append({"ticker": tick, "reason": "no_price_data"})
                continue

            try:
                psp = json.loads(ps["properties"]) if ps["properties"] else {}
                traded_val = psp.get("traded_value") or psp.get("totalTradedValue")
                volume     = psp.get("volume") or psp.get("totalTradedQuantity")

                issues = []
                if traded_val is not None and traded_val < _MIN_TRADED_VALUE:
                    issues.append(f"low_value:₹{traded_val/1e5:.1f}L")
                if volume is not None and volume < _MIN_VOLUME:
                    issues.append(f"low_volume:{volume:,}")

                if issues:
                    low_liq.append({
                        "ticker":        tick,
                        "traded_value":  traded_val,
                        "volume":        volume,
                        "issues":        issues,
                    })
                else:
                    ok_count += 1
            except Exception:
                low_liq.append({"ticker": tick, "reason": "parse_error"})

        return {
            "checked":        len(buy_tickers),
            "ok":             ok_count,
            "flagged":        len(low_liq),
            "illiquid":       low_liq[:20],  # cap output
        }

    def run(self) -> dict:
        """
        Run all risk checks. Write RiskReport node. Return summary.
        """
        t0 = datetime.now()

        conc    = self.sector_concentration()
        quality = self.signal_quality_score()
        liq     = self.liquidity_check()

        # Risk summary
        low_quality = [q for q in quality if q["quality_score"] < 0.5]
        flags_count = len(conc["flags"]) + len(liq["illiquid"]) + len(low_quality)

        overall_risk = "low"
        if flags_count >= 10 or conc["flags"]:
            overall_risk = "medium"
        if flags_count >= 20 or any(f["severity"] == "high" for f in conc["flags"]):
            overall_risk = "high"

        result = {
            "analyzed_at":      t0.isoformat(),
            "overall_risk":     overall_risk,
            "total_buy_signals": conc["total_signals"],
            "sector_concentration": {
                "flags":          conc["flags"],
                "top_sectors":    list(conc["sectors"].items())[:5],
            },
            "signal_quality": {
                "total_checked":    len(quality),
                "high_quality":     sum(1 for q in quality if q["quality_score"] >= 0.7),
                "medium_quality":   sum(1 for q in quality if 0.5 <= q["quality_score"] < 0.7),
                "low_quality":      len(low_quality),
                "low_quality_list": [q["ticker"] for q in low_quality[:10]],
            },
            "liquidity": {
                "checked":  liq["checked"],
                "ok":       liq["ok"],
                "flagged":  liq["flagged"],
                "illiquid": [x["ticker"] for x in liq["illiquid"][:10]],
            },
            "flags_total": flags_count,
        }

        # Write RiskReport node to graph.db
        try:
            import sys
            sys.path.insert(0, str(REPO))
            from memory.graph_store import get_store
            gs = get_store()
            gs.upsert_node(
                node_type="RiskReport",
                name=f"RiskReport_{date.today()}",
                ticker=None,
                properties=result,
            )
        except Exception:
            pass

        return result


if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))
    p = argparse.ArgumentParser(description="RiskAgent — portfolio risk checks")
    p.add_argument("--run",    action="store_true", help="Run all checks")
    p.add_argument("--ticker", help="Signal quality for one ticker")
    args = p.parse_args()

    agent = RiskAgent()
    if args.run:
        r = agent.run()
        print(json.dumps(r, indent=2, default=str))
    elif args.ticker:
        q = agent.signal_quality_score(args.ticker)
        print(json.dumps(q, indent=2, default=str))
    else:
        p.print_help()
