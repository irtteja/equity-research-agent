"""
Meta-cognitive engine — the system observing and reasoning about itself.

Responsibilities:
  1. observe()          — read graph.db for coverage, staleness, agent perf, queue depth
  2. identify_gaps()    — produce prioritised list of knowledge gaps
  3. enqueue_gap_work() — push re-ingestion / re-analysis tasks to work_queue
  4. write_snapshot()   — store MetaCogSnapshot node in graph.db
  5. run_meta_cycle()   — full loop: observe → gaps → enqueue → snapshot

No external API calls. Pure graph queries + heuristic gap scoring.

Called by:
  jobs/orchestrator.py  (after each swarm cycle)
  python -m memory.meta_cognition --report
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"

# Staleness thresholds (days) per node type
STALE_THRESHOLDS = {
    "PriceSnapshot":    1,
    "TrendSummary":     3,
    "ConfluenceSignal": 3,
    "CorporateEvent":   1,
    "DeliveryMetric":   3,
    "InsiderTrade":     7,
    "CorporateAction":  7,
    "ManagementStatement": 14,
    "MetricValue":      7,
}


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _gs():
    from memory.graph_store import get_store
    return get_store()


# ── Observation ──────────────────────────────────────────────────────────────

def observe() -> dict:
    """
    Comprehensive system self-observation.
    Returns a snapshot dict of graph health, coverage gaps, agent performance.
    """
    from memory.coverage_map import get_gaps, get_stale, missing_types
    from memory.agent_perf import all_stats, worst_performing

    obs = {"ts": datetime.now().isoformat(), "ok": True, "errors": []}

    # ── Graph stats ───────────────────────────────────────────────────────────
    try:
        with _conn() as c:
            obs["graph"] = {
                "total_nodes":    c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
                "total_edges":    c.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
                "total_episodes": c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
                "node_types":     {
                    r[0]: r[1]
                    for r in c.execute(
                        "SELECT node_type, COUNT(*) FROM nodes GROUP BY node_type"
                    ).fetchall()
                },
            }
    except Exception as e:
        obs["errors"].append(f"graph_stats: {e}")

    # ── Queue depths ─────────────────────────────────────────────────────────
    try:
        with _conn() as c:
            obs["queues"] = {
                r[0]: r[1]
                for r in c.execute(
                    "SELECT queue, COUNT(*) FROM work_queue WHERE status='pending' GROUP BY queue"
                ).fetchall()
            }
    except Exception as e:
        obs["queues"] = {}
        obs["errors"].append(f"queue_depth: {e}")

    # ── Coverage gaps ────────────────────────────────────────────────────────
    try:
        obs["coverage_gaps"] = get_gaps(min_coverage=0.35, limit=100)
    except Exception as e:
        obs["coverage_gaps"] = []
        obs["errors"].append(f"coverage_gaps: {e}")

    # ── Staleness per node type ───────────────────────────────────────────────
    try:
        stale = {}
        for nt, threshold in STALE_THRESHOLDS.items():
            tickers = get_stale(nt, days_threshold=threshold, limit=50)
            if tickers:
                stale[nt] = {"count": len(tickers), "sample": tickers[:5], "threshold_days": threshold}
        obs["stale"] = stale
    except Exception as e:
        obs["stale"] = {}
        obs["errors"].append(f"stale: {e}")

    # ── Agent performance ────────────────────────────────────────────────────
    try:
        obs["agent_perf"] = all_stats(days_back=7)
        obs["agent_problems"] = worst_performing(days_back=7, min_runs=3)
    except Exception as e:
        obs["agent_perf"] = []
        obs["agent_problems"] = []
        obs["errors"].append(f"agent_perf: {e}")

    # ── Unused nselib surface ────────────────────────────────────────────────
    try:
        obs["unused_nselib"] = _find_unused_nselib()
    except Exception as e:
        obs["unused_nselib"] = []
        obs["errors"].append(f"unused_nselib: {e}")

    return obs


def _find_unused_nselib() -> list[str]:
    """
    Find nselib NSE methods not referenced in any tools/, agents/, or jobs/ file.
    Returns list of method names.
    """
    import re
    try:
        import sys as _sys
        _nselib_path = str(REPO / "vendor" / "nselib")
        if _nselib_path not in _sys.path:
            _sys.path.insert(0, _nselib_path)
        from nselib import NSE
        all_methods = [m for m in dir(NSE) if not m.startswith("_")]
    except Exception:
        return []

    # scan source files for method name references
    code_dirs = [REPO / "tools", REPO / "agents", REPO / "jobs", REPO / "generated_tools"]
    referenced = set()
    for d in code_dirs:
        if not d.exists():
            continue
        for py in d.rglob("*.py"):
            try:
                text = py.read_text(errors="ignore")
                for m in all_methods:
                    if re.search(rf'\b{re.escape(m)}\b', text):
                        referenced.add(m)
            except Exception:
                pass

    return sorted(set(all_methods) - referenced)


# ── Gap identification ────────────────────────────────────────────────────────

def identify_gaps(obs: dict) -> list[dict]:
    """
    Turn observation dict into prioritised gap list.
    Each gap: {type, ticker, node_type, priority, reason}
    priority 1=highest, 3=lowest.
    """
    gaps = []

    # Coverage gaps → re-ingestion
    for item in obs.get("coverage_gaps", []):
        gaps.append({
            "type":      "coverage",
            "ticker":    item["ticker"],
            "node_type": None,
            "priority":  2 if item["score"] < 0.2 else 3,
            "reason":    f"coverage_score={item['score']:.2f}",
        })

    # Stale nodes → re-fetch
    for nt, info in obs.get("stale", {}).items():
        for ticker in info["sample"]:
            gaps.append({
                "type":      "stale",
                "ticker":    ticker,
                "node_type": nt,
                "priority":  1 if nt in ("PriceSnapshot", "CorporateEvent") else 2,
                "reason":    f"{nt} stale (>{info['threshold_days']}d)",
            })

    # Agent problems → flag but don't enqueue (operator attention needed)
    for ap in obs.get("agent_problems", []):
        gaps.append({
            "type":      "agent_failure",
            "ticker":    None,
            "node_type": None,
            "agent_id":  ap["agent_id"],
            "priority":  2,
            "reason":    f"success_rate={ap['success_rate']:.2f} over {ap['runs']} runs",
        })

    # Unused nselib → synthesis opportunity
    for method in obs.get("unused_nselib", [])[:20]:  # top 20 unused
        gaps.append({
            "type":      "unused_capability",
            "ticker":    None,
            "node_type": None,
            "method":    method,
            "priority":  3,
            "reason":    f"nselib.{method} never called",
        })

    gaps.sort(key=lambda g: g["priority"])
    return gaps


# ── Gap enqueue ───────────────────────────────────────────────────────────────

def enqueue_gap_work(gaps: list[dict]) -> int:
    """Push coverage/staleness gaps onto work_queue for re-ingestion."""
    from memory.work_queue import enqueue
    enqueued = 0
    seen = set()
    for g in gaps:
        if g["type"] not in ("coverage", "stale"):
            continue
        ticker = g.get("ticker")
        if not ticker:
            continue
        key = (ticker, g.get("node_type") or "coverage")
        if key in seen:
            continue
        seen.add(key)
        queue = "context_queue" if g["type"] == "coverage" else _staleness_queue(g["node_type"])
        try:
            enqueue(queue=queue, item_key=ticker, payload={"gap": g}, priority=g["priority"])
            enqueued += 1
        except Exception:
            pass
    return enqueued


def _staleness_queue(node_type: str) -> str:
    mapping = {
        "PriceSnapshot":    "context_queue",
        "TrendSummary":     "trend_queue",
        "ConfluenceSignal": "trend_queue",
        "CorporateEvent":   "context_queue",
        "DeliveryMetric":   "context_queue",
        "InsiderTrade":     "context_queue",
        "ManagementStatement": "document_queue",
    }
    return mapping.get(node_type, "context_queue")


# ── Snapshot ──────────────────────────────────────────────────────────────────

def write_snapshot(obs: dict, gaps: list[dict], enqueued: int) -> None:
    """Write a MetaCogSnapshot episode to graph.db."""
    try:
        gs = _gs()
        summary = (
            f"graph={obs['graph']['total_nodes']}nodes "
            f"gaps={len(obs.get('coverage_gaps',[]))} "
            f"stale_types={len(obs.get('stale',{}))} "
            f"enqueued={enqueued} "
            f"agent_issues={len(obs.get('agent_problems',[]))}"
        )
        gs.add_episode(
            episode_type="meta_cog_snapshot",
            ticker=None,
            text=summary,
            source="memory/meta_cognition.py",
            metadata={
                "observation": {k: v for k, v in obs.items() if k not in ("agent_perf",)},
                "top_gaps":    gaps[:20],
                "enqueued":    enqueued,
            },
        )
    except Exception:
        pass


# ── Full cycle ────────────────────────────────────────────────────────────────

def run_meta_cycle(enqueue_work: bool = True, verbose: bool = False) -> dict:
    """
    Full meta-cognitive cycle:
      1. Observe system state
      2. Identify gaps
      3. Enqueue re-ingestion work (if enqueue_work=True)
      4. Write snapshot to graph
    Returns summary dict.
    """
    if verbose:
        print("  [meta] observing system...")
    obs = observe()

    if verbose:
        g = obs["graph"]
        print(f"  [meta] graph: {g['total_nodes']} nodes, {g['total_episodes']} episodes")
        print(f"  [meta] coverage_gaps: {len(obs.get('coverage_gaps', []))}")
        print(f"  [meta] stale types: {list(obs.get('stale', {}).keys())}")

    gaps = identify_gaps(obs)
    if verbose:
        print(f"  [meta] identified {len(gaps)} gaps (priority 1: {sum(1 for g in gaps if g['priority']==1)})")

    enqueued = 0
    if enqueue_work:
        enqueued = enqueue_gap_work(gaps)
        if verbose:
            print(f"  [meta] enqueued {enqueued} gap-fill tasks")

    write_snapshot(obs, gaps, enqueued)

    return {
        "ts":               obs["ts"],
        "graph_nodes":      obs["graph"]["total_nodes"],
        "coverage_gaps":    len(obs.get("coverage_gaps", [])),
        "stale_types":      len(obs.get("stale", {})),
        "total_gaps":       len(gaps),
        "enqueued":         enqueued,
        "agent_problems":   len(obs.get("agent_problems", [])),
        "unused_nselib":    len(obs.get("unused_nselib", [])),
        "errors":           obs.get("errors", []),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))
    p = argparse.ArgumentParser(description="Meta-cognitive engine")
    p.add_argument("--report", action="store_true", help="Print observation report")
    p.add_argument("--cycle",  action="store_true", help="Run full meta cycle")
    p.add_argument("--no-enqueue", action="store_true")
    args = p.parse_args()

    if args.report:
        obs = observe()
        print(json.dumps({k: v for k, v in obs.items() if k != "agent_perf"}, indent=2, default=str))
    elif args.cycle:
        result = run_meta_cycle(enqueue_work=not args.no_enqueue, verbose=True)
        print(json.dumps(result, indent=2, default=str))
    else:
        p.print_help()
