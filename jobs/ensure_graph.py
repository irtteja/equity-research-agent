#!/usr/bin/env python3
"""
Ensure graph.db is populated before running analysis jobs.

On a fresh container, graph.db starts empty. This script:
  1. Checks if graph.db has sufficient Company+MetricValue nodes
  2. If empty, runs backfill_graph.py to rebuild from results/*.jsonl
  3. Then runs trend_agent_runner.py to rebuild TrendSummary nodes

Fast no-op if graph already has >=100 Company nodes (~2s).
Full rebuild from 873 result files takes ~5-10 minutes.

Run at the start of any fresh-session triggered job that needs graph data.
"""
import sys
import subprocess
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

MIN_COMPANIES = 100


def _count_nodes(node_type: str) -> int:
    try:
        from memory.graph_store import get_store
        gs = get_store()
        with gs._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE node_type=?", (node_type,)
            ).fetchone()[0]
    except Exception:
        return 0


def needs_rebuild() -> bool:
    return _count_nodes("Company") < MIN_COMPANIES


def run_step(label: str, script: str) -> bool:
    print(f"\n[ensure_graph] {label}…")
    r = subprocess.run([sys.executable, str(REPO / "jobs" / script)], cwd=REPO)
    if r.returncode != 0:
        print(f"[ensure_graph] {label} exited with code {r.returncode} (non-fatal)")
        return False
    return True


def main():
    if not needs_rebuild():
        companies = _count_nodes("Company")
        metrics   = _count_nodes("MetricValue")
        trends    = _count_nodes("TrendSummary")
        print(f"[ensure_graph] graph.db ready: {companies} companies, "
              f"{metrics} metric values, {trends} trend summaries — skipping rebuild")
        return

    print("[ensure_graph] graph.db is empty or sparse — rebuilding from committed source data")
    print(f"[ensure_graph] source: memory_store/results/*.jsonl")

    run_step("Step 1/2: backfill XBRL metrics", "backfill_graph.py")
    run_step("Step 2/2: rebuild trend summaries", "trend_agent_runner.py")

    companies = _count_nodes("Company")
    metrics   = _count_nodes("MetricValue")
    trends    = _count_nodes("TrendSummary")
    print(f"\n[ensure_graph] rebuild complete: {companies} companies, "
          f"{metrics} metric values, {trends} trend summaries")


if __name__ == "__main__":
    main()
