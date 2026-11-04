"""
AnalystAgent — pure-Python heuristic synthesis of per-ticker graph context.

Reads TrendSummary, ConfluenceSignal, ManagementStatement, PriceSnapshot,
and MetricValue nodes from the knowledge graph. Scores and synthesizes them
into a structured analyst note, then writes an analyst_synthesis Episode.

No external API calls. No anthropic.Anthropic() instantiation.
Pure heuristics: scoring rules, keyword matching, momentum arithmetic.

Called from:
  jobs/analyst_runner.py    (parallel ThreadPoolExecutor)
  jobs/swarm_runner.py      (dynamic swarm dispatch)
  python -m agents.analyst_agent --ticker RELIANCE
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"

# Management tone keyword weights
_POSITIVE_WORDS = {
    "strong", "growth", "record", "highest", "expansion", "outperform",
    "robust", "accelerat", "momentum", "confident", "increase", "improve",
    "market share", "win", "guidance raised", "beat", "ahead",
}
_NEGATIVE_WORDS = {
    "decline", "headwind", "pressure", "challeng", "weak", "uncertain",
    "miss", "below", "disappoint", "cautious", "concern", "slowdown",
    "margin compression", "guidance cut", "deferred", "loss",
}
_CAUTIOUS_WORDS = {
    "monitor", "watch", "await", "evaluate", "assess", "near-term",
    "volatile", "macro", "geopolit", "cautious", "moderate",
}


def _gs():
    from memory.graph_store import get_store
    return get_store()


def _conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


class AnalystAgent:
    """
    Synthesizes per-ticker knowledge graph context into structured analyst notes.
    Writes analyst_synthesis Episodes to graph.db.
    """

    def __init__(self):
        self.gs = _gs()

    def process_single(self, item_key: str) -> dict:
        """Worker interface: item_key is ticker. Returns summary dict."""
        try:
            result = self.analyze_ticker(item_key)
            return {"processed": 1, "ticker": item_key, "recommendation": result.get("recommendation")}
        except Exception as exc:
            return {"processed": 0, "ticker": item_key, "error": str(exc)}

    def analyze_ticker(self, ticker: str) -> dict:
        """
        Full synthesis for one ticker.
        Returns structured dict with conviction_score, recommendation, rationale.
        Also writes an analyst_synthesis Episode to the graph.
        """
        ticker = ticker.upper()
        ctx = self._get_ticker_context(ticker)

        if not ctx["has_data"]:
            return {"ticker": ticker, "recommendation": "NO_DATA", "rationale": "Insufficient graph data."}

        # ── Score components ──────────────────────────────────────────────────
        price = ctx.get("price", {})
        signal = ctx.get("confluence_signal", {})
        trends = ctx.get("trends", [])
        statements = ctx.get("management_statements", [])

        # 1. Momentum score (avg of TrendSummary momentum_scores for improving metrics)
        improving = [t for t in trends if t.get("direction") == "improving"]
        deteriorating = [t for t in trends if t.get("direction") == "deteriorating"]
        momentum_metrics = len(improving)
        streak_max = max((t.get("consistent_periods", 0) for t in improving), default=0)
        avg_momentum = (
            sum(t.get("momentum_score", 0) for t in improving) / len(improving)
            if improving else 0.0
        )

        # 2. Price zone score
        near_52w_low = False
        near_52w_high = False
        price_zone = price.get("price_zone", "")
        near_low_pct = price.get("near_52w_low_pct", 999)
        near_high_pct = price.get("near_52w_high_pct", 999)
        if isinstance(near_low_pct, (int, float)) and near_low_pct < 20:
            near_52w_low = True
        if isinstance(near_high_pct, (int, float)) and near_high_pct < 10:
            near_52w_high = True

        # 3. Confluence signal
        has_signal = bool(signal)
        signal_score = signal.get("score", 0) if signal else 0
        signal_momentum = signal.get("momentum", 0) if signal else 0
        signal_streak = signal.get("streak", 0) if signal else 0

        # 4. Management tone
        stmt_texts = [s.get("statement", "") for s in statements[:10]]
        mgmt_tone = self._score_management_tone(stmt_texts)

        # 5. Metrics summary
        metrics_summary = {}
        for t in trends[:8]:
            metrics_summary[t.get("metric", "unknown")] = {
                "direction": t.get("direction"),
                "momentum": t.get("momentum_score"),
                "delta_pct": t.get("latest_delta_pct"),
            }

        # ── Catalysts & risks ─────────────────────────────────────────────────
        catalysts = []
        risks = []

        if near_52w_low:
            catalysts.append(f"Near 52W low (+{near_low_pct:.1f}% from low) — potential mean reversion")
        if momentum_metrics >= 3:
            catalysts.append(f"{momentum_metrics} metrics improving with avg momentum {avg_momentum:.2f}")
        if streak_max >= 3:
            catalysts.append(f"Strongest improving streak: {streak_max} consecutive quarters")
        if mgmt_tone in ("positive",):
            catalysts.append("Management commentary positive — bullish guidance language")
        if signal_momentum >= 0.9:
            catalysts.append(f"High momentum signal ({signal_momentum:.2f}) on {signal.get('metric','')}")

        if near_52w_high:
            risks.append("Near 52W high — limited upside, reversal risk")
        if len(deteriorating) >= 3:
            risks.append(f"{len(deteriorating)} metrics deteriorating — broad fundamental weakness")
        if mgmt_tone == "negative":
            risks.append("Management commentary cautious/negative — guidance risk")
        if signal_score > 0 and signal_momentum < 0.5:
            risks.append(f"Low momentum ({signal_momentum:.2f}) despite confluence — signal may be stale")
        if streak_max == 0 and momentum_metrics > 0:
            risks.append("No sustained improving streaks — improvement may be one-time")

        # ── Conviction score (0–1) ────────────────────────────────────────────
        conviction = 0.0
        if has_signal:
            conviction += 0.30 * signal_score
        if near_52w_low:
            conviction += 0.20
        conviction += 0.20 * min(avg_momentum, 1.0)
        conviction += 0.15 * min(momentum_metrics / 5.0, 1.0)
        conviction += 0.10 * min(streak_max / 4.0, 1.0)
        if mgmt_tone == "positive":
            conviction += 0.05
        elif mgmt_tone == "negative":
            conviction -= 0.05
        conviction = round(max(0.0, min(1.0, conviction)), 3)

        # ── Recommendation ────────────────────────────────────────────────────
        recommendation = self._compute_recommendation({
            "has_signal": has_signal,
            "near_52w_low": near_52w_low,
            "avg_momentum": avg_momentum,
            "momentum_metrics": momentum_metrics,
            "streak_max": streak_max,
            "signal_momentum": signal_momentum,
            "mgmt_tone": mgmt_tone,
            "conviction": conviction,
            "deteriorating": len(deteriorating),
        })

        # ── Rationale (2–3 sentences) ─────────────────────────────────────────
        close_str = f"₹{price.get('close', '?')}" if price else "?"
        rationale_parts = []
        if has_signal:
            rationale_parts.append(
                f"{ticker} carries a BUY_CONFLUENCE signal (score {signal_score:.2f}) "
                f"with {signal_streak}Q improving {signal.get('metric','metric')}."
            )
        if near_52w_low and close_str != "?":
            rationale_parts.append(
                f"Price at {close_str} is {near_low_pct:.1f}% above its 52W low, "
                f"in the {price_zone.replace('_',' ')} zone."
            )
        if momentum_metrics > 0:
            rationale_parts.append(
                f"{momentum_metrics} of {len(trends)} tracked metrics are improving "
                f"(avg momentum {avg_momentum:.2f}, max streak {streak_max}Q); "
                f"management tone is {mgmt_tone}."
            )
        if not rationale_parts:
            rationale_parts.append(f"{ticker}: {len(trends)} metrics tracked, {momentum_metrics} improving, no strong signal.")

        rationale = " ".join(rationale_parts)

        result = {
            "ticker": ticker,
            "timestamp": datetime.now().isoformat(),
            "conviction_score": conviction,
            "signal_type": signal.get("type", "NONE") if signal else "NONE",
            "catalysts": catalysts,
            "risks": risks,
            "metrics_summary": metrics_summary,
            "management_tone": mgmt_tone,
            "near_52w_low": near_52w_low,
            "near_52w_high": near_52w_high,
            "momentum_metrics": momentum_metrics,
            "streak_max": streak_max,
            "recommendation": recommendation,
            "rationale": rationale,
        }

        # ── Write to episodes ─────────────────────────────────────────────────
        try:
            self.gs.add_episode(
                episode_type="analyst_synthesis",
                ticker=ticker,
                text=rationale,
                source="agents/analyst_agent.py",
                metadata=result,
            )
        except Exception:
            pass  # don't fail on write errors

        return result

    def bulk_analyze(self, tickers: list[str] = None, limit: int = 50) -> list[dict]:
        """
        Analyze top tickers by signal presence.
        If tickers=None, picks tickers that have ConfluenceSignal nodes.
        """
        if tickers is None:
            tickers = self._tickers_with_signals(limit)
        results = []
        for ticker in tickers[:limit]:
            try:
                r = self.analyze_ticker(ticker)
                results.append(r)
            except Exception as exc:
                results.append({"ticker": ticker, "error": str(exc)})
        return results

    # ── Context builder ───────────────────────────────────────────────────────

    def _get_ticker_context(self, ticker: str) -> dict:
        """Pull all graph nodes for a ticker into a context dict."""
        ctx = {"ticker": ticker, "has_data": False}
        with _conn() as c:
            # Trends
            rows = c.execute(
                "SELECT properties FROM nodes WHERE node_type='TrendSummary' AND ticker=?",
                (ticker,),
            ).fetchall()
            ctx["trends"] = [json.loads(r[0]) for r in rows]

            # Confluence signals
            rows = c.execute(
                "SELECT properties FROM nodes WHERE node_type='ConfluenceSignal' AND ticker=? "
                "ORDER BY created_at DESC LIMIT 1",
                (ticker,),
            ).fetchall()
            ctx["confluence_signal"] = json.loads(rows[0][0]) if rows else {}

            # Price snapshot
            rows = c.execute(
                "SELECT properties FROM nodes WHERE node_type='PriceSnapshot' AND ticker=? "
                "ORDER BY created_at DESC LIMIT 1",
                (ticker,),
            ).fetchall()
            ctx["price"] = json.loads(rows[0][0]) if rows else {}

            # Management statements (last 10)
            rows = c.execute(
                "SELECT properties FROM nodes WHERE node_type='ManagementStatement' AND ticker=? "
                "ORDER BY created_at DESC LIMIT 10",
                (ticker,),
            ).fetchall()
            ctx["management_statements"] = [json.loads(r[0]) for r in rows]

        ctx["has_data"] = bool(ctx["trends"] or ctx["confluence_signal"] or ctx["price"])
        return ctx

    # ── Scoring helpers ───────────────────────────────────────────────────────

    def _score_management_tone(self, statements: list[str]) -> str:
        """Keyword-based tone scoring: positive / cautious / negative / neutral."""
        if not statements:
            return "neutral"
        pos = neg = caut = 0
        for text in statements:
            lower = text.lower()
            pos  += sum(1 for w in _POSITIVE_WORDS  if w in lower)
            neg  += sum(1 for w in _NEGATIVE_WORDS  if w in lower)
            caut += sum(1 for w in _CAUTIOUS_WORDS  if w in lower)
        total = pos + neg + caut
        if total == 0:
            return "neutral"
        if pos >= neg * 1.5 and pos > caut:
            return "positive"
        if neg >= pos * 1.5:
            return "negative"
        if caut >= pos and caut >= neg:
            return "cautious"
        return "neutral"

    def _compute_recommendation(self, ctx: dict) -> str:
        """
        BUY:   confluence + momentum >=0.7 + near 52W low + 2+ improving quarters
        HOLD:  confluence but weak momentum or not near low
        WATCH: some improvement, no confluence
        AVOID: deteriorating metrics dominant
        """
        c = ctx
        if c["has_signal"] and c["signal_momentum"] >= 0.7 and c["near_52w_low"] and c["streak_max"] >= 2:
            return "BUY"
        if c["has_signal"] and c["conviction"] >= 0.45:
            return "HOLD"
        if c["deteriorating"] >= 4 and not c["has_signal"]:
            return "AVOID"
        if c["momentum_metrics"] >= 2:
            return "WATCH"
        return "HOLD"

    def _tickers_with_signals(self, limit: int = 50) -> list[str]:
        """Return tickers that have ConfluenceSignal nodes, ordered by score desc."""
        with _conn() as c:
            rows = c.execute(
                "SELECT ticker, json_extract(properties,'$.score') as score "
                "FROM nodes WHERE node_type='ConfluenceSignal' "
                "ORDER BY score DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r[0] for r in rows if r[0]]


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))

    p = argparse.ArgumentParser(description="Run AnalystAgent synthesis")
    p.add_argument("--ticker", help="Analyze one ticker")
    p.add_argument("--bulk",   action="store_true", help="Analyze top 50 signal tickers")
    p.add_argument("--limit",  type=int, default=50)
    args = p.parse_args()

    agent = AnalystAgent()
    if args.ticker:
        result = agent.analyze_ticker(args.ticker.upper())
        print(json.dumps(result, indent=2, default=str))
    elif args.bulk:
        results = agent.bulk_analyze(limit=args.limit)
        for r in results:
            rec = r.get("recommendation", "?")
            conv = r.get("conviction_score", 0)
            print(f"  {r['ticker']:15s} {rec:6s}  conviction={conv:.3f}  {r.get('rationale','')[:80]}")
    else:
        p.print_help()
