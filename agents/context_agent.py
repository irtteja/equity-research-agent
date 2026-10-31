"""
ContextAgent — enriches MetricValue nodes with temporal context.

Reads unenriched MetricValue nodes from the graph, finds the prior period's
same metric, computes delta and direction, and creates PRECEDED_BY edges.
Also scans ManagementStatement nodes for corroborating/contradicting evidence.

Called from:
  jobs/context_agent_runner.py (scheduled)
  python -m agents.context_agent (CLI)

Output edges:
  PRECEDED_BY  — current → prior node, properties: {delta, delta_pct, direction}
  CORROBORATES — ManagementStatement → MetricValue (statement supports number)
  CONTRADICTS  — ManagementStatement → MetricValue (statement contradicts number)
"""
import re
from datetime import datetime

DIRECTION_THRESHOLD = 0.02   # < 2% change = stable


def _gs():
    from memory.graph_store import get_store
    return get_store()


def _direction(delta_pct: float) -> str:
    if delta_pct > DIRECTION_THRESHOLD:
        return "improving"
    if delta_pct < -DIRECTION_THRESHOLD:
        return "deteriorating"
    return "stable"


def _sentiment_consistent(statement_sentiment: str, direction: str) -> bool | None:
    """Return True=corroborates, False=contradicts, None=unclear."""
    s = (statement_sentiment or "").lower()
    if not s or s == "neutral":
        return None
    positive = s in ("positive", "bullish", "optimistic")
    negative = s in ("negative", "bearish", "pessimistic", "cautious")
    improving = direction == "improving"
    if positive and improving:
        return True
    if negative and not improving:
        return True
    if positive and direction == "deteriorating":
        return False
    if negative and improving:
        return False
    return None


class ContextAgent:
    """
    Enriches MetricValue nodes with PRECEDED_BY edges.
    Each call processes up to `limit` unenriched nodes.
    """

    def __init__(self):
        self.gs = _gs()

    def process_pending(self, limit: int = 20) -> dict:
        """
        Process unenriched MetricValue nodes.
        Returns summary: {processed, edges_created, skipped}.
        """
        nodes = self.gs.pending_enrichment(node_type="MetricValue", limit=limit)
        processed = skipped = edges_created = 0

        for node in nodes:
            try:
                n = self._enrich_node(node)
                edges_created += n
                processed += 1
            except Exception as exc:
                skipped += 1
                _log(f"ContextAgent: skip {node['id']}: {exc}")
            self.gs.mark_enriched(node["id"])

        return {"processed": processed, "edges_created": edges_created, "skipped": skipped}

    # ── Core enrichment ───────────────────────────────────────────────────────

    def _enrich_node(self, node: dict) -> int:
        """
        For one MetricValue node: find prior period, create PRECEDED_BY edge.
        Returns number of edges created.
        """
        props   = node["properties"]
        ticker  = node.get("ticker", "")
        metric  = props.get("metric", "")
        period  = props.get("period", "")
        value   = props.get("value")
        node_id = node["id"]

        if not ticker or not metric or value is None:
            return 0

        # Fetch history ordered by period_date desc (most recent first)
        history = self.gs.get_metric_history(ticker, metric, n=8)

        # Find the first node that is an earlier period (not self)
        prior_node = None
        for h in history:
            if h["id"] == node_id:
                continue
            # Accept if it has a period_date before ours, or just take the first other
            prior_period_date = h["properties"].get("period_date", "")
            our_period_date   = props.get("period_date", "")
            if our_period_date and prior_period_date and prior_period_date >= our_period_date:
                continue  # same or newer — skip
            prior_node = h
            break

        edges = 0

        if prior_node:
            prior_value = prior_node["properties"].get("value")
            if prior_value is not None and float(prior_value) != 0:
                delta     = float(value) - float(prior_value)
                delta_pct = delta / abs(float(prior_value))
                direction = _direction(delta_pct)
            else:
                delta = delta_pct = 0.0
                direction = "stable"

            self.gs.add_edge(
                "PRECEDED_BY",
                node_id,
                prior_node["id"],
                properties={
                    "delta":      round(delta, 4),
                    "delta_pct":  round(delta_pct * 100, 2),
                    "direction":  direction,
                    "ticker":     ticker,
                    "metric":     metric,
                    "period":     period,
                    "prior_period": prior_node["properties"].get("period", ""),
                },
            )
            edges += 1

            self.gs.add_episode(
                episode_type="metric_trend",
                text=(
                    f"{ticker.upper()} {metric}: {value} in {period} "
                    f"vs {prior_value} in {prior_node['properties'].get('period','?')} "
                    f"({'+' if delta >= 0 else ''}{round(delta_pct*100,1)}%, {direction})"
                ),
                source="agents/context_agent.py",
                ticker=ticker,
                metadata={
                    "node_id":     node_id,
                    "prior_id":    prior_node["id"],
                    "delta_pct":   round(delta_pct * 100, 2),
                    "direction":   direction,
                },
            )

        # ── Statement corroboration ───────────────────────────────────────────
        edges += self._link_statements(node, direction if prior_node else None)

        return edges

    def _link_statements(self, node: dict, direction: str | None) -> int:
        """
        Find ManagementStatement nodes for same ticker + same period,
        create CORROBORATES or CONTRADICTS edges.
        """
        if direction is None:
            return 0

        ticker = node.get("ticker", "")
        period = node["properties"].get("period", "")
        metric = node["properties"].get("metric", "")

        if not ticker or not period:
            return 0

        # Search statement nodes for same ticker+period
        stmts = self.gs.search_nodes(
            query=period,
            node_type="ManagementStatement",
            ticker=ticker,
            limit=10,
        )

        edges = 0
        for stmt in stmts:
            sp = stmt["properties"]
            stmt_period = sp.get("period", "")
            if stmt_period and stmt_period != period:
                continue

            sentiment   = sp.get("sentiment", "")
            consistent  = _sentiment_consistent(sentiment, direction)
            if consistent is None:
                continue

            edge_type = "CORROBORATES" if consistent else "CONTRADICTS"
            self.gs.add_edge(
                edge_type,
                stmt["id"],
                node["id"],
                properties={
                    "ticker":    ticker,
                    "metric":    metric,
                    "period":    period,
                    "sentiment": sentiment,
                    "direction": direction,
                },
            )
            edges += 1

        return edges

    # ── Trend summary ─────────────────────────────────────────────────────────

    def trend_summary(self, ticker: str, metric: str, n: int = 6) -> list[dict]:
        """
        Return last N metric values with trend direction, most recent first.
        Each entry: {period, value, unit, delta_pct, direction}
        """
        history = self.gs.get_metric_history(ticker, metric, n=n)
        out = []
        for node in history:
            p = node["properties"]
            entry = {
                "period":    p.get("period", "?"),
                "value":     p.get("value"),
                "unit":      p.get("unit", ""),
                "delta_pct": None,
                "direction": None,
            }
            edges = self.gs.get_edges(node["id"], edge_type="PRECEDED_BY", direction="out")
            if edges:
                ep = edges[0]["properties"]
                entry["delta_pct"] = ep.get("delta_pct")
                entry["direction"] = ep.get("direction")
            out.append(entry)
        return out


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log(msg: str):
    try:
        from memory.task_log import log_checkpoint
        log_checkpoint("context_agent", status="info", progress=msg)
    except Exception:
        print(msg)


# ── CLI / runner ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys, json
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))

    p = argparse.ArgumentParser(description="Run ContextAgent enrichment pass")
    p.add_argument("--limit", type=int, default=50, help="Max nodes to enrich per run")
    p.add_argument("--trend", help="Show trend: TICKER:METRIC")
    args = p.parse_args()

    agent = ContextAgent()

    if args.trend:
        ticker, metric = args.trend.split(":", 1)
        rows = agent.trend_summary(ticker, metric)
        print(f"\n{ticker.upper()} — {metric} ({len(rows)} periods):")
        for r in rows:
            delta_str = f"  Δ {r['delta_pct']:+.1f}% ({r['direction']})" if r['delta_pct'] is not None else ""
            print(f"  {r['period']}: {r['value']} {r['unit']}{delta_str}")
    else:
        result = agent.process_pending(limit=args.limit)
        print(f"ContextAgent: processed={result['processed']}  "
              f"edges_created={result['edges_created']}  skipped={result['skipped']}")
