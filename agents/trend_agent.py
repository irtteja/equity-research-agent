"""
TrendAgent — detects metric acceleration/deceleration from PRECEDED_BY edge chains.

Reads enriched MetricValue nodes that have PRECEDED_BY edges (written by ContextAgent),
walks the chain back N periods, and computes:
  - direction consistency  (improving/deteriorating N periods in a row)
  - acceleration           (rate of change increasing or decreasing)
  - momentum score         (composite: direction × recency × magnitude)

Writes TrendSummary nodes to the graph. Agents downstream (AlertAgent) read these.

Node: TrendSummary
  id:         trend-{ticker}-{metric}
  properties: metric, ticker, periods_analyzed, direction, consistent_periods,
              latest_delta_pct, acceleration, momentum_score, summary_text

Edge: HAS_TREND  Company → TrendSummary
Edge: TREND_OF   TrendSummary → latest MetricValue node
"""
from datetime import datetime


CONSISTENT_THRESHOLD = 2    # N consecutive same-direction periods = trend
STRONG_MOMENTUM      = 0.6  # momentum score above this = strong trend


def _gs():
    from memory.graph_store import get_store
    return get_store()


def _momentum_score(deltas: list[float]) -> float:
    """
    Composite score in [-1, 1]. Positive = improving trend.
    Weights recent periods more heavily; larger deltas score higher.
    """
    if not deltas:
        return 0.0
    n = len(deltas)
    weighted = sum(d * (i + 1) for i, d in enumerate(deltas))
    max_possible = sum(abs(d) * (i + 1) for i, d in enumerate(deltas)) or 1
    raw = weighted / max_possible
    return round(max(-1.0, min(1.0, raw)), 3)


def _acceleration(deltas: list[float]) -> str:
    """
    Compare average of recent half vs older half of deltas.
    Returns 'accelerating', 'decelerating', or 'steady'.
    """
    if len(deltas) < 3:
        return "steady"
    mid  = len(deltas) // 2
    old  = sum(abs(d) for d in deltas[:mid]) / mid
    new  = sum(abs(d) for d in deltas[mid:]) / (len(deltas) - mid)
    if new > old * 1.25:
        return "accelerating"
    if new < old * 0.75:
        return "decelerating"
    return "steady"


class TrendAgent:
    """
    Detects trends from PRECEDED_BY edge chains. One pass per ticker+metric pair.
    """

    def __init__(self):
        self.gs = _gs()

    def process_all(self, min_periods: int = 3, limit: int = 100) -> dict:
        """
        Find all (ticker, metric) pairs with ≥ min_periods enriched nodes,
        compute trends, write TrendSummary nodes.
        Returns summary: {pairs_analyzed, trends_written, strong_trends}.
        """
        pairs = self._find_pairs(limit=limit)
        pairs_analyzed = trends_written = strong = 0

        for ticker, metric in pairs:
            chain = self._walk_chain(ticker, metric)
            if len(chain) < min_periods:
                continue
            pairs_analyzed += 1
            result = self._write_trend(ticker, metric, chain)
            if result:
                trends_written += 1
                if abs(result.get("momentum_score", 0)) >= STRONG_MOMENTUM:
                    strong += 1

        return {"pairs_analyzed": pairs_analyzed, "trends_written": trends_written,
                "strong_trends": strong}

    # ── Chain walking ──────────────────────────────────────────────────────────

    def _find_pairs(self, limit: int) -> list[tuple[str, str]]:
        """Return distinct (ticker, metric) from enriched MetricValue nodes."""
        with self.gs._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ticker, json_extract(properties,'$.metric') as metric "
                "FROM nodes WHERE node_type='MetricValue' AND enriched=1 "
                "AND ticker != '' AND json_extract(properties,'$.metric') != '' "
                "LIMIT ?",
                (limit,),
            ).fetchall()
        return [(r["ticker"], r["metric"]) for r in rows]

    def _walk_chain(self, ticker: str, metric: str, max_depth: int = 8) -> list[dict]:
        """
        Walk PRECEDED_BY chain for ticker+metric from most recent node.
        Returns list of {node_id, period, value, delta_pct, direction} newest-first.
        """
        history = self.gs.get_metric_history(ticker, metric, n=max_depth)
        if not history:
            return []

        chain = []
        for node in history:
            p = node["properties"]
            entry = {
                "node_id":   node["id"],
                "period":    p.get("period", "?"),
                "value":     p.get("value"),
                "delta_pct": None,
                "direction": None,
            }
            edges = self.gs.get_edges(node["id"], edge_type="PRECEDED_BY", direction="out")
            if edges:
                ep = edges[0]["properties"]
                entry["delta_pct"] = ep.get("delta_pct")
                entry["direction"] = ep.get("direction")
            chain.append(entry)

        return chain

    # ── Trend computation + write ──────────────────────────────────────────────

    def _write_trend(self, ticker: str, metric: str, chain: list[dict]) -> dict | None:
        """Compute trend from chain and upsert a TrendSummary node."""
        deltas     = [c["delta_pct"] for c in chain if c["delta_pct"] is not None]
        directions = [c["direction"] for c in chain if c["direction"]]

        if not deltas:
            return None

        # Direction consistency: count leading streak of same direction
        latest_dir = directions[0] if directions else "stable"
        streak = 0
        for d in directions:
            if d == latest_dir:
                streak += 1
            else:
                break

        momentum     = _momentum_score(deltas)
        acceleration = _acceleration(deltas)
        latest_delta = deltas[0] if deltas else 0.0

        # Human-readable summary
        strength = "strong" if abs(momentum) >= STRONG_MOMENTUM else "moderate" if abs(momentum) >= 0.3 else "weak"
        summary  = (
            f"{ticker} {metric}: {latest_dir} for {streak} consecutive periods "
            f"(latest Δ {latest_delta:+.1f}%, {acceleration}, {strength} momentum)"
        )

        node_id = f"trend-{ticker.upper()}-{metric}".replace(" ", "_")

        self.gs.add_node(
            node_id=node_id,
            node_type="TrendSummary",
            name=f"{ticker.upper()} {metric} trend",
            ticker=ticker,
            properties={
                "metric":             metric,
                "ticker":             ticker.upper(),
                "periods_analyzed":   len(chain),
                "direction":          latest_dir,
                "consistent_periods": streak,
                "latest_delta_pct":   latest_delta,
                "acceleration":       acceleration,
                "momentum_score":     momentum,
                "summary_text":       summary,
                "updated_at":         datetime.now().isoformat(),
            },
        )

        self.gs.add_episode(
            episode_type="trend_signal",
            text=summary,
            source="agents/trend_agent.py",
            ticker=ticker,
            metadata={
                "node_id":        node_id,
                "momentum_score": momentum,
                "direction":      latest_dir,
                "streak":         streak,
                "acceleration":   acceleration,
            },
        )

        # HAS_TREND edge from Company node
        self.gs.add_node(
            node_id=f"company-{ticker.upper()}",
            node_type="Company",
            name=ticker.upper(),
            ticker=ticker,
            properties={"ticker": ticker.upper()},
        )
        self.gs.add_edge("HAS_TREND", f"company-{ticker.upper()}", node_id)

        # TREND_OF edge to most recent MetricValue
        if chain:
            self.gs.add_edge("TREND_OF", node_id, chain[0]["node_id"])

        return {
            "node_id":        node_id,
            "direction":      latest_dir,
            "streak":         streak,
            "momentum_score": momentum,
            "acceleration":   acceleration,
            "summary":        summary,
        }

    # ── Surface strong trends ──────────────────────────────────────────────────

    def strong_trends(self, min_momentum: float = STRONG_MOMENTUM) -> list[dict]:
        """Return TrendSummary nodes with |momentum_score| >= min_momentum."""
        nodes = self.gs.search_nodes("trend", node_type="TrendSummary", limit=200)
        out = []
        for n in nodes:
            p = n["properties"]
            if abs(p.get("momentum_score", 0)) >= min_momentum:
                out.append({
                    "ticker":       p.get("ticker", "?"),
                    "metric":       p.get("metric", "?"),
                    "direction":    p.get("direction", "?"),
                    "streak":       p.get("consistent_periods", 0),
                    "delta_pct":    p.get("latest_delta_pct", 0),
                    "momentum":     p.get("momentum_score", 0),
                    "acceleration": p.get("acceleration", "?"),
                    "summary":      p.get("summary_text", ""),
                })
        return sorted(out, key=lambda x: -abs(x["momentum"]))


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys, json
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))

    p = argparse.ArgumentParser(description="Run TrendAgent")
    p.add_argument("--min-periods", type=int, default=3, help="Min periods to compute trend")
    p.add_argument("--strong",      action="store_true",  help="Show only strong trends")
    p.add_argument("--ticker",      help="Filter by ticker")
    args = p.parse_args()

    agent = TrendAgent()
    result = agent.process_all(min_periods=args.min_periods)
    print(f"TrendAgent: pairs={result['pairs_analyzed']} trends={result['trends_written']} strong={result['strong_trends']}")

    trends = agent.strong_trends(min_momentum=0.3 if not args.strong else STRONG_MOMENTUM)
    if args.ticker:
        trends = [t for t in trends if t["ticker"].upper() == args.ticker.upper()]

    if trends:
        print(f"\nTrends ({len(trends)}):")
        for t in trends:
            icon = "▲" if t["direction"] == "improving" else "▼" if t["direction"] == "deteriorating" else "─"
            print(f"  {icon} {t['ticker']} {t['metric']}: {t['streak']}p streak, "
                  f"Δ{t['delta_pct']:+.1f}%, momentum={t['momentum']:+.2f} ({t['acceleration']})")
    else:
        print("  No trends above threshold yet.")
