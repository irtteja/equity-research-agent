"""
CoverageAgent — assesses per-ticker knowledge graph coverage.

Rebuilds the ticker_coverage table for a set of tickers (or all tickers).
Writes CoverageScore nodes to graph.db for each ticker.
Called by orchestrator after ingestion cycles and by daily_monitor.

No external API calls. Pure graph queries.

Called from:
  jobs/orchestrator.py  (Phase: meta)
  python -m agents.coverage_agent [--ticker X] [--all] [--rebuild]
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"


def _gs():
    from memory.graph_store import get_store
    return get_store()


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


class CoverageAgent:
    """
    Builds and maintains the ticker_coverage map.
    Scores each ticker and writes CoverageScore graph nodes.
    """

    def __init__(self):
        from memory.coverage_map import FULL_TYPES
        self.full_types = FULL_TYPES
        self.gs = _gs()

    def process_single(self, item_key: str) -> dict:
        """Worker interface: item_key is ticker."""
        try:
            result = self.assess_ticker(item_key)
            return {"processed": 1, "ticker": item_key, "score": result["score"]}
        except Exception as exc:
            return {"processed": 0, "ticker": item_key, "error": str(exc)}

    def assess_ticker(self, ticker: str) -> dict:
        """
        Compute coverage score for one ticker.
        Writes a CoverageScore node to graph.db.
        Returns {ticker, score, types_present, types_missing, staleness_avg}.
        """
        from memory.coverage_map import coverage_score, get_coverage, missing_types, rebuild_coverage

        ticker = ticker.upper()
        # Rebuild coverage for this ticker
        rebuild_coverage([ticker])

        cov   = get_coverage(ticker)
        score = coverage_score(ticker)
        miss  = missing_types(ticker)

        stale_vals = [v["staleness_days"] for v in cov.values() if v["staleness_days"] is not None]
        avg_stale  = round(sum(stale_vals) / len(stale_vals), 1) if stale_vals else 999.0

        result = {
            "ticker":          ticker,
            "score":           score,
            "types_present":   sorted(cov.keys()),
            "types_missing":   sorted(miss),
            "node_count":      sum(v["count"] for v in cov.values()),
            "staleness_avg_d": avg_stale,
            "ts":              datetime.now().isoformat(),
        }

        # Write CoverageScore node
        try:
            self.gs.upsert_node(
                node_type="CoverageScore",
                name=f"coverage_{ticker}",
                ticker=ticker,
                properties=result,
            )
        except Exception:
            pass

        return result

    def run_all(self, tickers: list[str] | None = None, limit: int = 500) -> dict:
        """
        Assess coverage for all tickers (or given list).
        Returns summary dict.
        """
        from memory.coverage_map import rebuild_coverage

        if tickers is None:
            with _conn() as c:
                rows = c.execute(
                    "SELECT DISTINCT ticker FROM nodes WHERE ticker IS NOT NULL LIMIT ?",
                    (limit,),
                ).fetchall()
            tickers = [r[0] for r in rows]

        # First do a bulk rebuild for all tickers at once (faster)
        rebuild_coverage(tickers)

        results = []
        for t in tickers[:limit]:
            try:
                r = self.assess_ticker(t)
                results.append(r)
            except Exception as exc:
                results.append({"ticker": t, "error": str(exc)})

        ok     = [r for r in results if "score" in r]
        scores = [r["score"] for r in ok]
        avg    = round(sum(scores) / len(scores), 3) if scores else 0

        return {
            "total":      len(tickers),
            "assessed":   len(ok),
            "avg_score":  avg,
            "below_0.3":  sum(1 for s in scores if s < 0.3),
            "below_0.5":  sum(1 for s in scores if s < 0.5),
            "ts":         datetime.now().isoformat(),
        }

    def rebuild_full(self) -> int:
        """Full rebuild of ticker_coverage table from scratch."""
        from memory.coverage_map import rebuild_coverage
        return rebuild_coverage()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))
    p = argparse.ArgumentParser(description="Coverage agent")
    p.add_argument("--ticker",  help="Assess one ticker")
    p.add_argument("--all",     action="store_true", help="Assess all tickers")
    p.add_argument("--rebuild", action="store_true", help="Full coverage table rebuild")
    p.add_argument("--limit",   type=int, default=500)
    args = p.parse_args()

    agent = CoverageAgent()
    if args.ticker:
        r = agent.assess_ticker(args.ticker.upper())
        print(json.dumps(r, indent=2, default=str))
    elif args.all:
        r = agent.run_all(limit=args.limit)
        print(json.dumps(r, indent=2, default=str))
    elif args.rebuild:
        n = agent.rebuild_full()
        print(f"Rebuilt {n} (ticker, node_type) rows")
    else:
        p.print_help()
