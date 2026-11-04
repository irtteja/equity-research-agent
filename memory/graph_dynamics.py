"""
Graph dynamics — the living knowledge graph runtime.

Implements the learn / relearn / unlearn / prune cycle.
Called after every ingestion and analysis cycle to keep the graph healthy.

Operations:
  learn_from_new_nodes(ticker)   — assign initial confidence; corroborate related nodes
  relearn_ticker(ticker)         — full belief revision pass: check contradictions, re-score
  unlearn_stale(days)            — decay + retract beliefs with no recent support
  prune_graph(ticker)            — merge duplicates, delete retracted low-conf nodes
  compress_episodes(ticker)      — summarise old episodes into SummaryNode
  form_edges(ticker)             — induce edges from co-occurring patterns
  run_dynamics_cycle(tickers)    — full cycle for a list of tickers

No external API calls. Pure graph operations.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"

# How many episodes of same type before compression
COMPRESS_THRESHOLD = 30
# Confidence below which a retracted node is physically deleted
DELETE_THRESHOLD   = 0.12
# Age in days before an un-evidenced node starts decaying
DECAY_START_DAYS   = 3


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory  = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _gs():
    from memory.graph_store import get_store
    return get_store()


# ── Learning: initial confidence assignment ────────────────────────────────────

# Initial confidence by node type (how trustworthy is first observation?)
_INITIAL_CONF = {
    "PriceSnapshot":     0.80,   # price data is objective
    "TrendSummary":      0.55,   # computed metric — plausible
    "ConfluenceSignal":  0.60,   # inferred — needs corroboration
    "CorporateEvent":    0.85,   # official NSE filing — high trust
    "ManagementStatement": 0.50, # forward-looking — uncertain
    "MetricValue":       0.70,   # financial data — relatively objective
    "DeliveryMetric":    0.70,
    "InsiderTrade":      0.80,   # regulatory filing
    "CorporateAction":   0.85,
    "PledgeData":        0.75,
    "SASTFiling":        0.80,
    "CoverageScore":     0.60,
    "AnalystSynthesis":  0.55,
}
_DEFAULT_INITIAL = 0.50


def learn_from_new_nodes(ticker: str, hours_back: float = 2.0) -> int:
    """
    Assign initial confidence to nodes written in the last N hours.
    Corroborate related nodes if the new node provides supporting evidence.
    Returns count of nodes initialised.
    """
    from memory.belief_engine import corroborate, _state_for
    cutoff = (datetime.now() - timedelta(hours=hours_back)).isoformat()

    with _conn() as c:
        new_nodes = c.execute(
            """SELECT id, node_type, ticker, properties, confidence
               FROM nodes
               WHERE ticker=? AND created_at >= ? AND (confidence IS NULL OR confidence = 0.5)""",
            (ticker, cutoff),
        ).fetchall()

    initialised = 0
    for n in new_nodes:
        init_conf = _INITIAL_CONF.get(n["node_type"], _DEFAULT_INITIAL)
        with _conn() as c:
            c.execute(
                "UPDATE nodes SET confidence=?, belief_state=? WHERE id=?",
                (init_conf, _state_for(init_conf), n["id"]),
            )
        initialised += 1

        # Cross-corroboration: new price data supports existing confluence signals
        if n["node_type"] == "PriceSnapshot":
            _corroborate_related(ticker, "ConfluenceSignal", weight=0.2,
                                 reason="new_price_data_supports_signal")

        # New positive corp event corroborates analyst synthesis
        elif n["node_type"] == "CorporateEvent":
            try:
                props = json.loads(n["properties"] or "{}")
                if props.get("category") in ("Results", "Dividend"):
                    _corroborate_related(ticker, "ConfluenceSignal", weight=0.25,
                                         reason="positive_corp_event")
            except Exception:
                pass

        # New trend summary: if improving, corroborate confluence; if deteriorating, contradict
        elif n["node_type"] == "TrendSummary":
            try:
                props = json.loads(n["properties"] or "{}")
                direction = props.get("direction", "")
                if direction == "improving":
                    _corroborate_related(ticker, "ConfluenceSignal", weight=0.30,
                                         reason="metric_improving")
                elif direction == "deteriorating":
                    _contradict_related(ticker, "ConfluenceSignal", weight=0.30,
                                        reason="metric_deteriorating")
            except Exception:
                pass

    return initialised


def _corroborate_related(ticker: str, node_type: str, weight: float, reason: str):
    from memory.belief_engine import corroborate
    with _conn() as c:
        rows = c.execute(
            "SELECT id FROM nodes WHERE ticker=? AND node_type=? AND belief_state != 'retracted'",
            (ticker, node_type),
        ).fetchall()
    for r in rows:
        corroborate(r["id"], weight=weight, source="graph_dynamics", reason=reason)


def _contradict_related(ticker: str, node_type: str, weight: float, reason: str):
    from memory.belief_engine import contradict
    with _conn() as c:
        rows = c.execute(
            "SELECT id FROM nodes WHERE ticker=? AND node_type=? AND belief_state != 'retracted'",
            (ticker, node_type),
        ).fetchall()
    for r in rows:
        contradict(r["id"], weight=weight, source="graph_dynamics", reason=reason)


# ── Relearning: belief revision pass ─────────────────────────────────────────

def relearn_ticker(ticker: str) -> dict:
    """
    Full belief revision for one ticker:
      1. Detect and resolve contradictions
      2. Apply temporal decay to old nodes
      3. Corroborate nodes that have multiple supporting nodes
    Returns revision summary.
    """
    from memory.belief_engine import (
        resolve_contradictions, decay_belief, corroborate
    )

    # Step 1: contradiction resolution
    revisions = resolve_contradictions(ticker)

    # Step 2: temporal decay for nodes older than DECAY_START_DAYS
    cutoff = (datetime.now() - timedelta(days=DECAY_START_DAYS)).isoformat()
    with _conn() as c:
        old_nodes = c.execute(
            """SELECT id, (julianday('now') - julianday(created_at)) as age_days
               FROM nodes WHERE ticker=? AND created_at < ?
               AND belief_state NOT IN ('retracted')""",
            (ticker, cutoff),
        ).fetchall()
    decayed = 0
    for n in old_nodes:
        result = decay_belief(n["id"], days=min(float(n["age_days"] or 1), 14))
        if result.get("old_conf") != result.get("new_conf"):
            decayed += 1

    # Step 3: multi-evidence corroboration
    # Count how many distinct node types support a ConfluenceSignal
    with _conn() as c:
        signal_nodes = c.execute(
            "SELECT id FROM nodes WHERE ticker=? AND node_type='ConfluenceSignal' AND belief_state != 'retracted'",
            (ticker,),
        ).fetchall()
        supporting_types = c.execute(
            """SELECT COUNT(DISTINCT node_type) as n FROM nodes
               WHERE ticker=? AND node_type IN
               ('TrendSummary','MetricValue','CorporateEvent','ManagementStatement')
               AND belief_state != 'retracted'""",
            (ticker,),
        ).fetchone()
    supporting = supporting_types["n"] if supporting_types else 0
    for s in signal_nodes:
        if supporting >= 3:
            corroborate(s["id"], weight=float(supporting) * 0.1, reason="multi_type_support")

    return {
        "ticker":         ticker,
        "revisions":      revisions,
        "decayed":        decayed,
        "signal_support": supporting,
    }


# ── Unlearning: retract unsupported beliefs ────────────────────────────────────

def unlearn_stale(days_threshold: float = 14.0) -> dict:
    """
    Find nodes with no corroboration in the last N days and retract them
    if their confidence has fallen below the contested threshold.
    Returns retraction summary.
    """
    from memory.belief_engine import retract_belief, _T_CONTESTED
    cutoff = (datetime.now() - timedelta(days=days_threshold)).isoformat()

    with _conn() as c:
        candidates = c.execute(
            """SELECT id, ticker, node_type, confidence, belief_state
               FROM nodes
               WHERE created_at < ?
                 AND belief_state IN ('forming', 'contested')
                 AND confidence < ?
                 AND id NOT IN (
                     SELECT DISTINCT node_id FROM belief_log
                     WHERE created_at >= ? AND (reason LIKE '%corroborate%' OR reason LIKE '%support%')
                 )""",
            (cutoff, _T_CONTESTED, cutoff),
        ).fetchall()

    retracted = 0
    for n in candidates:
        retract_belief(n["id"], reason=f"unlearn_stale: no corroboration in {days_threshold}d")
        retracted += 1

    return {"retracted": retracted, "threshold_days": days_threshold}


# ── Pruning: deduplicate, delete, compress ─────────────────────────────────────

def prune_graph(ticker: str = None) -> dict:
    """
    Prune the graph for a ticker (or all tickers if None):
      1. Merge duplicate nodes (same type + content within 24h)
      2. Delete retracted low-confidence nodes (keep tombstone episode)
      3. Isolate cleanup (nodes with no edges and very low confidence)
    Returns prune statistics.
    """
    merged = deleted = isolated = 0

    # 1. Merge duplicates: same (ticker, node_type, day) — keep highest confidence
    with _conn() as c:
        if ticker:
            dupes = c.execute(
                """SELECT ticker, node_type, date(created_at) as day,
                          COUNT(*) as cnt, MIN(id) as keep_id
                   FROM nodes WHERE ticker=?
                   GROUP BY ticker, node_type, date(created_at)
                   HAVING cnt > 1""",
                (ticker,),
            ).fetchall()
        else:
            dupes = c.execute(
                """SELECT ticker, node_type, date(created_at) as day,
                          COUNT(*) as cnt, MIN(id) as keep_id
                   FROM nodes
                   GROUP BY ticker, node_type, date(created_at)
                   HAVING cnt > 3""",   # only merge if 3+ dupes (avoid false merges)
            ).fetchall()

        for d in dupes:
            # Identify the best node (highest confidence) to keep
            best = c.execute(
                """SELECT id, confidence FROM nodes
                   WHERE ticker=? AND node_type=? AND date(created_at)=?
                   ORDER BY confidence DESC LIMIT 1""",
                (d["ticker"], d["node_type"], d["day"]),
            ).fetchone()
            if not best:
                continue
            # Delete others from that group
            deleted_ids = c.execute(
                """SELECT id FROM nodes
                   WHERE ticker=? AND node_type=? AND date(created_at)=?
                   AND id != ?""",
                (d["ticker"], d["node_type"], d["day"], best["id"]),
            ).fetchall()
            for did in deleted_ids:
                # Re-parent edges from deleted → kept
                c.execute("UPDATE edges SET source_id=? WHERE source_id=?", (best["id"], did["id"]))
                c.execute("UPDATE edges SET target_id=? WHERE target_id=?", (best["id"], did["id"]))
                c.execute("DELETE FROM nodes WHERE id=?", (did["id"],))
                merged += 1

    # 2. Delete retracted nodes below delete threshold
    with _conn() as c:
        candidates = c.execute(
            """SELECT id, ticker, node_type, confidence FROM nodes
               WHERE belief_state='retracted' AND confidence < ?
               AND created_at < datetime('now', '-7 days')""",
            (DELETE_THRESHOLD,),
        ).fetchall()

    gs = _gs()
    for n in candidates:
        # Write tombstone episode before deletion
        try:
            gs.add_episode(
                episode_type="node_deleted",
                ticker=n["ticker"],
                text=f"retracted {n['node_type']} (conf={n['confidence']:.2f})",
                source="graph_dynamics.prune",
                metadata={"node_id": n["id"], "reason": "low_confidence_retracted"},
            )
        except Exception:
            pass
        with _conn() as c:
            c.execute("DELETE FROM edges WHERE source_id=? OR target_id=?", (n["id"], n["id"]))
            c.execute("DELETE FROM nodes WHERE id=?", (n["id"],))
        deleted += 1

    # 3. Isolated cleanup — nodes with no edges and evidence_count=1 and very old
    with _conn() as c:
        old_cutoff = (datetime.now() - timedelta(days=60)).isoformat()
        iso = c.execute(
            """SELECT id FROM nodes
               WHERE confidence < 0.25
                 AND evidence_count <= 1
                 AND created_at < ?
                 AND id NOT IN (SELECT source_id FROM edges UNION SELECT target_id FROM edges)""",
            (old_cutoff,),
        ).fetchall()
        for n in iso:
            c.execute("DELETE FROM nodes WHERE id=?", (n["id"],))
            isolated += 1

    return {"merged": merged, "deleted": deleted, "isolated": isolated}


# ── Episode compression ───────────────────────────────────────────────────────

def compress_episodes(ticker: str) -> dict:
    """
    If a ticker has more than COMPRESS_THRESHOLD episodes of the same type,
    summarise the oldest ones into a SummaryNode and delete the originals.
    Returns compression summary.
    """
    gs = _gs()
    compressed = 0

    with _conn() as c:
        type_counts = c.execute(
            """SELECT episode_type, COUNT(*) as n FROM episodes
               WHERE ticker=? GROUP BY episode_type HAVING n > ?""",
            (ticker, COMPRESS_THRESHOLD),
        ).fetchall()

    for tc in type_counts:
        ep_type  = tc["episode_type"]
        keep     = 10  # keep the 10 most recent

        with _conn() as c:
            old_eps = c.execute(
                """SELECT id, text, metadata FROM episodes
                   WHERE ticker=? AND episode_type=?
                   ORDER BY created_at DESC
                   LIMIT -1 OFFSET ?""",
                (ticker, ep_type, keep),
            ).fetchall()

        if len(old_eps) < 5:
            continue

        summary_texts = [r["text"] for r in old_eps[:20] if r["text"]]
        summary_meta  = {
            "compressed_from":  ep_type,
            "count_compressed": len(old_eps),
            "ticker":           ticker,
            "sample_texts":     summary_texts[:5],
        }
        try:
            gs.add_episode(
                episode_type=f"{ep_type}_summary",
                ticker=ticker,
                text=f"Compressed {len(old_eps)} {ep_type} episodes for {ticker}",
                source="graph_dynamics.compress",
                metadata=summary_meta,
            )
        except Exception:
            pass

        ids_to_delete = [r["id"] for r in old_eps]
        with _conn() as c:
            placeholders = ",".join("?" * len(ids_to_delete))
            c.execute(f"DELETE FROM episodes WHERE id IN ({placeholders})", ids_to_delete)
        compressed += len(ids_to_delete)

    return {"ticker": ticker, "compressed": compressed}


# ── Dynamic edge formation ────────────────────────────────────────────────────

def form_edges(ticker: str) -> int:
    """
    Induce new edges from co-occurring patterns in the graph.
    - analyst_synthesis BUY/HOLD → ANALYST_BUY/HOLD edge on ConfluenceSignal
    - Two improving TrendSummary for same metric → TREND_CHAIN edge
    Returns count of edges formed.
    """
    formed = 0
    with _conn() as c:
        # analyst_synthesis episode → ConfluenceSignal
        analyst_eps = c.execute(
            """SELECT id, json_extract(metadata,'$.recommendation') as rec,
                      json_extract(metadata,'$.conviction_score') as conv
               FROM episodes WHERE episode_type='analyst_synthesis' AND ticker=?
               ORDER BY created_at DESC LIMIT 1""",
            (ticker,),
        ).fetchone()
        signal_node = c.execute(
            "SELECT id FROM nodes WHERE ticker=? AND node_type='ConfluenceSignal' AND belief_state != 'retracted' LIMIT 1",
            (ticker,),
        ).fetchone()

        if analyst_eps and signal_node and analyst_eps["rec"] in ("BUY", "HOLD"):
            existing = c.execute(
                "SELECT id FROM edges WHERE source_id=? AND target_id=? AND edge_type=?",
                (signal_node["id"], signal_node["id"], f"ANALYST_{analyst_eps['rec']}"),
            ).fetchone()
            if not existing:
                weight = float(analyst_eps["conv"] or 0.5)
                c.execute(
                    """INSERT OR IGNORE INTO edges (source_id, target_id, edge_type, weight, created_at)
                       VALUES (?,?,?,?,datetime('now'))""",
                    (signal_node["id"], signal_node["id"],
                     f"ANALYST_{analyst_eps['rec']}", weight),
                )
                formed += 1

        # TrendSummary chains: improving metric → TREND_CHAIN edges
        trend_rows = c.execute(
            """SELECT id, json_extract(properties,'$.metric') as metric
               FROM nodes WHERE ticker=? AND node_type='TrendSummary'
                 AND json_extract(properties,'$.direction')='improving'
                 AND belief_state != 'retracted'
               ORDER BY created_at DESC LIMIT 10""",
            (ticker,),
        ).fetchall()
        metric_nodes: dict = {}
        for r in trend_rows:
            metric_nodes.setdefault(r["metric"], []).append(r["id"])
        for metric, ids in metric_nodes.items():
            if len(ids) >= 2:
                for i in range(len(ids) - 1):
                    existing = c.execute(
                        "SELECT id FROM edges WHERE source_id=? AND target_id=? AND edge_type='TREND_CHAIN'",
                        (ids[i], ids[i+1]),
                    ).fetchone()
                    if not existing:
                        c.execute(
                            "INSERT OR IGNORE INTO edges (source_id, target_id, edge_type, weight, created_at) VALUES (?,?,'TREND_CHAIN',0.7,datetime('now'))",
                            (ids[i], ids[i+1]),
                        )
                        formed += 1

    return formed


# ── Full dynamics cycle ───────────────────────────────────────────────────────

def run_dynamics_cycle(
    tickers: list[str] | None = None,
    limit: int = 100,
    unlearn_days: float = 14.0,
    prune_all: bool = False,
    verbose: bool = False,
) -> dict:
    """
    Full learn/relearn/unlearn/prune cycle.
      1. Learn from recent nodes (last 24h)
      2. Relearn (contradiction resolution + decay) for each ticker
      3. Unlearn stale beliefs
      4. Prune: merge dupes, delete retracted
      5. Compress old episodes
      6. Form new edges from patterns
    Returns cycle summary.
    """
    from memory.belief_engine import _ensure_schema
    _ensure_schema()   # idempotent

    if tickers is None:
        with _conn() as c:
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            rows = c.execute(
                "SELECT DISTINCT ticker FROM nodes WHERE ticker IS NOT NULL AND created_at >= ? LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        tickers = [r[0] for r in rows]

    if verbose:
        print(f"  [dynamics] processing {len(tickers)} tickers")

    learned = relearned_total = edges_formed = compressed = 0

    for ticker in tickers:
        learned       += learn_from_new_nodes(ticker, hours_back=24)
        r              = relearn_ticker(ticker)
        relearned_total += r.get("revisions", 0)
        edges_formed   += form_edges(ticker)
        compressed     += compress_episodes(ticker).get("compressed", 0)

    unlearn_result = unlearn_stale(days_threshold=unlearn_days)
    prune_result   = prune_graph(ticker=None if prune_all else (tickers[0] if len(tickers) == 1 else None))

    summary = {
        "tickers":        len(tickers),
        "learned":        learned,
        "revisions":      relearned_total,
        "edges_formed":   edges_formed,
        "compressed":     compressed,
        "retracted":      unlearn_result.get("retracted", 0),
        "pruned_merged":  prune_result.get("merged", 0),
        "pruned_deleted": prune_result.get("deleted", 0),
    }
    try:
        _gs().add_episode(
            episode_type="graph_dynamics_snapshot",
            ticker=None,
            text=(
                f"dynamics cycle: {len(tickers)} tickers, "
                f"{learned} learned, {relearned_total} revisions, "
                f"{edges_formed} edges formed, {unlearn_result.get('retracted',0)} retracted"
            ),
            source="memory/graph_dynamics.py",
            metadata=summary,
        )
    except Exception:
        pass

    if verbose:
        for k, v in summary.items():
            print(f"  [dynamics] {k}: {v}")

    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))
    p = argparse.ArgumentParser(description="Graph dynamics engine")
    p.add_argument("--ticker",  help="Run for one ticker")
    p.add_argument("--cycle",   action="store_true", help="Run full dynamics cycle")
    p.add_argument("--prune",   action="store_true", help="Prune graph (all tickers)")
    p.add_argument("--unlearn", type=float, default=14.0, metavar="DAYS")
    p.add_argument("--limit",   type=int, default=100)
    p.add_argument("--belief",  help="Print belief summary for ticker")
    args = p.parse_args()

    import json
    if args.belief:
        from memory.belief_engine import belief_summary, recent_revisions
        print(json.dumps(belief_summary(args.belief.upper()), indent=2))
        print("\nRecent revisions:")
        for r in recent_revisions(args.belief.upper(), limit=10):
            print(f"  {r['node_type']:25s} {r['old_conf']:.2f}→{r['new_conf']:.2f} {r['reason']}")
    elif args.ticker:
        result = relearn_ticker(args.ticker.upper())
        print(json.dumps(result, indent=2))
    elif args.cycle:
        result = run_dynamics_cycle(limit=args.limit, unlearn_days=args.unlearn, verbose=True)
        print(json.dumps(result, indent=2))
    elif args.prune:
        result = prune_graph()
        print(json.dumps(result, indent=2))
    else:
        p.print_help()
