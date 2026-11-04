"""
dynamics_runner.py — CLI runner for the living knowledge graph dynamics cycle.

Wraps memory/graph_dynamics.py:run_dynamics_cycle() with:
  - agent_perf recording
  - belief state report
  - CLI flags for targeted runs

Usage:
  python jobs/dynamics_runner.py --cycle [--ticker RELIANCE] [--limit 200] [--verbose]
  python jobs/dynamics_runner.py --report
  python jobs/dynamics_runner.py --belief RELIANCE
  python jobs/dynamics_runner.py --prune-only [--ticker RELIANCE]

Called by jobs/orchestrator.py run_meta_phase() as:
  from jobs.dynamics_runner import run_cycle
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))


def run_cycle(
    tickers: list[str] = None,
    limit: int = 200,
    verbose: bool = False,
    unlearn_days: float = 14.0,
) -> dict:
    """
    Run one full graph dynamics cycle and record performance.
    Returns summary dict with keys: tickers, learned, relearned, pruned,
    compressed, edges_formed, errors.
    """
    from memory.graph_dynamics import run_dynamics_cycle
    from memory.agent_perf import record_run

    t0 = time.time()
    try:
        result = run_dynamics_cycle(
            tickers=tickers,
            limit=limit,
            unlearn_days=unlearn_days,
            verbose=verbose,
        )
        duration = time.time() - t0
        record_run(
            agent_id="graph_dynamics_v1",
            items_processed=result.get("tickers_processed", 0),
            items_failed=len(result.get("errors", [])),
            duration_s=duration,
            queue_name="meta",
            notes=json.dumps({
                k: result[k] for k in ("learned", "relearned", "pruned", "compressed", "edges_formed")
                if k in result
            }, default=str),
        )
        return result
    except Exception as exc:
        duration = time.time() - t0
        record_run(
            agent_id="graph_dynamics_v1",
            items_processed=0,
            items_failed=1,
            duration_s=duration,
            queue_name="meta",
            notes=f"cycle_error: {exc}",
        )
        return {"error": str(exc), "tickers_processed": 0}


def print_belief_report(ticker: str = None):
    """Print a belief state distribution report for all tickers or a specific one."""
    import sqlite3
    db_path = REPO / "memory_store" / "graph.db"
    with sqlite3.connect(str(db_path), timeout=30) as c:
        c.row_factory = sqlite3.Row

        print("\n── Belief State Distribution ─────────────────────────────")
        rows = c.execute(
            """SELECT belief_state, COUNT(*) as n, AVG(confidence) as avg_conf,
                      MIN(confidence) as min_conf, MAX(confidence) as max_conf
               FROM nodes
               WHERE belief_state IS NOT NULL
               GROUP BY belief_state ORDER BY n DESC"""
        ).fetchall()
        for r in rows:
            print(f"  {r['belief_state']:12s}  count={r['n']:6d}  "
                  f"avg={r['avg_conf']:.3f}  min={r['min_conf']:.3f}  max={r['max_conf']:.3f}")

        print("\n── Confidence by Node Type ────────────────────────────────")
        rows = c.execute(
            """SELECT node_type, COUNT(*) as n, AVG(confidence) as avg_conf
               FROM nodes WHERE confidence IS NOT NULL
               GROUP BY node_type ORDER BY avg_conf DESC LIMIT 20"""
        ).fetchall()
        for r in rows:
            bar_len = int((r["avg_conf"] or 0) * 30)
            bar = "█" * bar_len + "░" * (30 - bar_len)
            print(f"  {r['node_type']:25s}  {bar}  {r['avg_conf']:.3f}  (n={r['n']})")

        if ticker:
            print(f"\n── Belief Log — {ticker} (last 15) ─────────────────────")
            rows = c.execute(
                """SELECT created_at, node_type, old_conf, new_conf, old_state, new_state, reason
                   FROM belief_log WHERE ticker=? ORDER BY created_at DESC LIMIT 15""",
                (ticker.upper(),),
            ).fetchall()
            for r in rows:
                delta = (r["new_conf"] or 0) - (r["old_conf"] or 0)
                sign = "▲" if delta > 0 else "▼"
                print(f"  {r['created_at'][:16]}  {r['node_type']:20s}  "
                      f"{r['old_conf']:.3f}→{r['new_conf']:.3f} {sign}{abs(delta):.3f}  "
                      f"{r['old_state']}→{r['new_state']}  [{r['reason']}]")

        print("\n── Edge Weight Distribution ───────────────────────────────")
        rows = c.execute(
            """SELECT edge_type, COUNT(*) as n, AVG(weight) as avg_w
               FROM edges WHERE weight IS NOT NULL
               GROUP BY edge_type ORDER BY n DESC LIMIT 15"""
        ).fetchall()
        for r in rows:
            print(f"  {r['edge_type']:30s}  count={r['n']:5d}  avg_weight={r['avg_w']:.3f}")

        print()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Living knowledge graph dynamics runner")
    p.add_argument("--cycle",      action="store_true", help="Run full dynamics cycle")
    p.add_argument("--ticker",     help="Restrict to one ticker")
    p.add_argument("--limit",      type=int, default=200, help="Max tickers to process")
    p.add_argument("--unlearn-days", type=float, default=14.0,
                   help="Days threshold for unlearn_stale (default 14)")
    p.add_argument("--verbose",    action="store_true")
    p.add_argument("--report",     action="store_true", help="Print belief state report")
    p.add_argument("--belief",     metavar="TICKER", help="Belief log for one ticker")
    p.add_argument("--prune-only", action="store_true", help="Run only prune_graph()")
    args = p.parse_args()

    if args.cycle or args.prune_only:
        if args.prune_only:
            from memory.graph_dynamics import prune_graph
            t0 = time.time()
            r = prune_graph(ticker=args.ticker)
            print(json.dumps({"prune": r, "duration_s": round(time.time() - t0, 2)}, indent=2, default=str))
        else:
            tickers = [args.ticker.upper()] if args.ticker else None
            r = run_cycle(
                tickers=tickers,
                limit=args.limit,
                verbose=args.verbose,
                unlearn_days=args.unlearn_days,
            )
            print(json.dumps(r, indent=2, default=str))

    elif args.report or args.belief:
        print_belief_report(ticker=args.belief or args.ticker)

    else:
        p.print_help()
