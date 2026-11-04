"""
SectorAnalyst — aggregates per-ticker graph signals into sector/industry momentum views.

Reads:
  - Company nodes  (sector, industry, macro properties)
  - TrendSummary nodes (trend direction per ticker/metric)
  - ConfluenceSignal nodes (buy signal score + momentum)

Writes:
  - sector_analysis episodes to graph (one per sector run)

No external API calls. Pure Python heuristics over graph.db.
"""
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB = Path(__file__).parent.parent / "memory_store" / "graph.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def _momentum_label(pct: float) -> str:
    if pct >= 70:
        return "Strong Momentum"
    if pct >= 60:
        return "Improving"
    if pct >= 50:
        return "Mixed"
    if pct >= 40:
        return "Weakening"
    return "Deteriorating"


def _short_hash(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:10]


class SectorAnalyst:
    """Aggregates per-ticker graph data into sector and industry momentum views."""

    def _load_company_map(self) -> dict:
        """Returns {ticker: {sector, industry, macro}} from Company nodes."""
        conn = _conn()
        rows = conn.execute(
            "SELECT ticker, properties FROM nodes WHERE node_type='Company'"
        ).fetchall()
        conn.close()
        result = {}
        for row in rows:
            try:
                props = json.loads(row["properties"])
                result[row["ticker"]] = {
                    "sector":   props.get("sector", "Unknown"),
                    "industry": props.get("industry", "Unknown"),
                    "macro":    props.get("macro", "Unknown"),
                }
            except Exception:
                pass
        return result

    def _load_trends(self) -> dict:
        """Returns {ticker: {improving: int, deteriorating: int}}."""
        conn = _conn()
        rows = conn.execute(
            "SELECT ticker, properties FROM nodes WHERE node_type='TrendSummary'"
        ).fetchall()
        conn.close()
        result: dict = {}
        for row in rows:
            t = row["ticker"]
            if not t:
                continue
            try:
                props = json.loads(row["properties"])
                direction = props.get("trend", props.get("direction", ""))
                if t not in result:
                    result[t] = {"improving": 0, "deteriorating": 0}
                if "improv" in direction:
                    result[t]["improving"] += 1
                elif "deterior" in direction:
                    result[t]["deteriorating"] += 1
            except Exception:
                pass
        return result

    def _load_signals(self) -> dict:
        """Returns {ticker: {score, momentum, streak}}."""
        conn = _conn()
        rows = conn.execute(
            "SELECT ticker, properties FROM nodes WHERE node_type='ConfluenceSignal'"
        ).fetchall()
        conn.close()
        result = {}
        for row in rows:
            try:
                props = json.loads(row["properties"])
                result[row["ticker"]] = {
                    "score":    props.get("score", 0),
                    "momentum": props.get("momentum", 0),
                    "streak":   props.get("streak", 0),
                }
            except Exception:
                pass
        return result

    def _aggregate(
        self,
        group_key: str,  # "sector" or "industry"
        company_map: dict,
        trends: dict,
        signals: dict,
    ) -> list[dict]:
        """Core aggregation logic shared by sector and industry views."""
        groups: dict = {}
        for ticker, info in company_map.items():
            key = info.get(group_key, "Unknown")
            if key not in groups:
                groups[key] = {
                    "name": key,
                    "macro": info.get("macro", "Unknown"),
                    "sector": info.get("sector", "Unknown"),
                    "tickers": [],
                }
            groups[key]["tickers"].append(ticker)

        results = []
        for key, grp in groups.items():
            tickers = grp["tickers"]
            with_trends = [t for t in tickers if t in trends]
            with_sigs   = [t for t in tickers if t in signals]

            improving     = sum(trends[t]["improving"]     for t in with_trends)
            deteriorating = sum(trends[t]["deteriorating"] for t in with_trends)
            total         = improving + deteriorating

            if total == 0:
                continue  # no data — skip

            imp_pct = round(improving / total * 100, 1)
            det_pct = round(deteriorating / total * 100, 1)
            avg_score = (
                round(sum(signals[t]["score"] for t in with_sigs) / len(with_sigs), 3)
                if with_sigs else None
            )

            # top tickers = those with signals, sorted by score asc (lower = stronger buy)
            top = sorted(with_sigs, key=lambda t: signals[t]["score"])[:5]

            results.append({
                group_key:          key,
                "macro":            grp["macro"],
                "sector":           grp.get("sector", key),
                "ticker_count":     len(tickers),
                "tracked_count":    len(with_trends),
                "signal_count":     len(with_sigs),
                "improving_pct":    imp_pct,
                "deteriorating_pct": det_pct,
                "avg_signal_score": avg_score,
                "momentum_label":   _momentum_label(imp_pct),
                "rotation_signal":  "LEADER" if imp_pct >= 68 else ("LAGGARD" if imp_pct < 42 else "NEUTRAL"),
                "top_tickers":      top,
            })

        return sorted(results, key=lambda x: -x["improving_pct"])

    def analyze_all_sectors(self) -> list[dict]:
        """Sector-level momentum, sorted best first."""
        cm = self._load_company_map()
        tr = self._load_trends()
        sg = self._load_signals()
        return self._aggregate("sector", cm, tr, sg)

    def analyze_industries(self, sector: str = None) -> list[dict]:
        """Industry-level momentum. Optionally filter to one sector."""
        cm = self._load_company_map()
        if sector:
            cm = {t: v for t, v in cm.items() if v.get("sector") == sector}
        tr = self._load_trends()
        sg = self._load_signals()
        return self._aggregate("industry", cm, tr, sg)

    def sector_rotation_signal(self) -> dict:
        """Identify sector rotation: leaders, laggards, rotating in/out."""
        sectors = self.analyze_all_sectors()
        # Only consider sectors with meaningful data (>= 10 tracked stocks)
        meaningful = [s for s in sectors if s["tracked_count"] >= 10]

        leaders  = [s["sector"] for s in meaningful if s["improving_pct"] >= 68][:3]
        laggards = [s["sector"] for s in reversed(meaningful) if s["improving_pct"] < 50][:3]

        # rotating_in: strong now (>65) — using SectorTrend nodes if available, else heuristic
        conn = _conn()
        st_rows = conn.execute(
            "SELECT ticker, properties FROM nodes WHERE node_type='SectorTrend'"
        ).fetchall()
        conn.close()

        sector_trend_hist: dict = {}
        for row in st_rows:
            try:
                props = json.loads(row["properties"])
                sec = props.get("sector", row["ticker"])
                sector_trend_hist[sec] = props
            except Exception:
                pass

        rotating_in  = []
        rotating_out = []
        for s in meaningful:
            sec  = s["sector"]
            curr = s["improving_pct"]
            hist = sector_trend_hist.get(sec, {})
            prev = hist.get("prev_improving_pct", curr)  # fallback = same (no rotation)
            if curr > 65 and prev < 55:
                rotating_in.append(sec)
            elif curr < 40:
                rotating_out.append(sec)

        return {
            "leaders":       leaders,
            "laggards":      laggards,
            "rotating_in":   rotating_in,
            "rotating_out":  rotating_out,
            "as_of":         datetime.now().isoformat(),
        }

    def write_sector_episodes(self):
        """Write sector_analysis episodes for each sector into the graph."""
        from memory.graph_store import get_store
        gs = get_store()
        sectors = self.analyze_all_sectors()
        count = 0
        for s in sectors:
            text = (
                f"{s['sector']}: {s['momentum_label']} ({s['improving_pct']}% improving, "
                f"{s['signal_count']} signals, {s['ticker_count']} stocks). "
                f"Rotation: {s['rotation_signal']}. "
                f"Top picks: {', '.join(s['top_tickers']) or 'none'}."
            )
            gs.add_episode(
                episode_type="sector_analysis",
                ticker=s["sector"].replace(" ", "_")[:30],
                text=text,
                source="sector_analyst",
                metadata=s,
            )
            count += 1
        return count
