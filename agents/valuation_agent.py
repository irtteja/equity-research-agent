"""
valuation_agent.py — runs valuation scoring on tracked tickers.

Calls memory/valuation_engine.py for each ticker, writes ValuationScore nodes.
Gets ticker list from ConfluenceSignal nodes; uses results/ JSONL files for data.

Called by: jobs/orchestrator.py (Phase 2 — analysis)
CLI: python -m agents.valuation_agent [--limit N] [--ticker TICKER]
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

DB_PATH = REPO / "memory_store" / "graph.db"
RES_DIR = REPO / "memory_store" / "results"


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _get_tickers(limit: int | None = None) -> list[str]:
    """Return tickers from ConfluenceSignal nodes that have results JSONL files."""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT ticker FROM nodes WHERE node_type='ConfluenceSignal' AND ticker IS NOT NULL"
        ).fetchall()
        tickers = [r[0] for r in rows if (RES_DIR / f"{r[0]}.jsonl").exists()]
    if not tickers:
        tickers = [f.stem for f in RES_DIR.glob("*.jsonl") if not f.stem.startswith("_")]
    if limit:
        tickers = tickers[:limit]
    return tickers


class ValuationAgent:
    """Compute valuation scores for tracked tickers."""

    def process_ticker(self, ticker: str) -> dict:
        from memory.valuation_engine import run_ticker
        return run_ticker(ticker)

    def run_all(self, tickers: list[str] | None = None, limit: int = 500) -> dict:
        """Run valuation scoring on all/given tickers. Returns summary."""
        t0 = time.time()
        if tickers is None:
            tickers = _get_tickers(limit)

        results = {"ok": 0, "skipped": 0, "error": 0}
        verdicts = {}
        for ticker in tickers:
            r = self.process_ticker(ticker)
            status = r.get("status", "error")
            results[status] = results.get(status, 0) + 1
            if status == "ok":
                v = r.get("verdict", "unknown")
                verdicts[v] = verdicts.get(v, 0) + 1

        try:
            from memory.agent_perf import record_run
            record_run(
                agent_id="valuation_agent_v1",
                items_processed=results["ok"],
                items_failed=results["error"],
                duration_s=time.time() - t0,
                queue_name="analysis",
            )
        except Exception:
            pass

        return {
            "tickers":  len(tickers),
            "ok":       results["ok"],
            "skipped":  results["skipped"],
            "error":    results["error"],
            "verdicts": verdicts,
            "duration": round(time.time() - t0, 1),
        }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Valuation scoring agent")
    p.add_argument("--limit",  type=int, default=200, help="Max tickers to process")
    p.add_argument("--ticker", help="Single ticker")
    args = p.parse_args()

    agent = ValuationAgent()
    if args.ticker:
        r = agent.process_ticker(args.ticker)
        print(json.dumps(r, indent=2, default=str))
    else:
        r = agent.run_all(limit=args.limit)
        print(json.dumps(r, indent=2, default=str))
