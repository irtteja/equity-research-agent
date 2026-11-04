"""
Belief engine — Bayesian belief revision for the knowledge graph.

Each node has:
  confidence    REAL 0-1       — how well-supported this belief is
  belief_state  TEXT           — forming | confirmed | contested | retracted
  evidence_count INTEGER       — number of independent corroborating sources

Belief transitions (confidence thresholds):
  < 0.20  → retracted    (no support — node may be pruned)
  < 0.40  → contested    (contradicted — trigger revision cycle)
  < 0.65  → forming      (some support — needs more evidence)
  >= 0.65 → confirmed    (well-supported)

Bayesian update (simplified):
  Corroboration:  new = old + (1 - old) * weight * 0.30
  Contradiction:  new = old * (1 - weight * 0.55)
  Decay (1 day):  new = old - (1 - old) * 0.03

All changes written to belief_log table.
Called by graph_dynamics.py after each ingestion / analysis cycle.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"

# Confidence thresholds for state transitions
_T_RETRACTED  = 0.20
_T_CONTESTED  = 0.40
_T_FORMING    = 0.65   # below → forming, above → confirmed


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _ensure_schema():
    """Add confidence columns to nodes and create belief_log if missing."""
    with _conn() as c:
        # Add columns (safe no-op if they exist)
        for col, defn in [
            ("confidence",     "REAL    DEFAULT 0.50"),
            ("belief_state",   "TEXT    DEFAULT 'forming'"),
            ("evidence_count", "INTEGER DEFAULT 1"),
        ]:
            try:
                c.execute(f"ALTER TABLE nodes ADD COLUMN {col} {defn}")
            except Exception:
                pass   # column already exists

        # Ensure edges has weight column (may already exist)
        try:
            c.execute("ALTER TABLE edges ADD COLUMN weight REAL DEFAULT 1.0")
        except Exception:
            pass

        c.execute("""
            CREATE TABLE IF NOT EXISTS belief_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id      INTEGER NOT NULL,
                ticker       TEXT,
                node_type    TEXT,
                old_conf     REAL,
                new_conf     REAL,
                old_state    TEXT,
                new_state    TEXT,
                reason       TEXT,
                source       TEXT DEFAULT '',
                created_at   TEXT DEFAULT (datetime('now'))
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bl_node ON belief_log(node_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bl_ticker ON belief_log(ticker)")


_ensure_schema()


# ── State logic ───────────────────────────────────────────────────────────────

def _state_for(confidence: float) -> str:
    if confidence < _T_RETRACTED:
        return "retracted"
    if confidence < _T_CONTESTED:
        return "contested"
    if confidence < _T_FORMING:
        return "forming"
    return "confirmed"


# ── Core update ───────────────────────────────────────────────────────────────

def update_belief(node_id: int, corroborate: bool, weight: float = 1.0,
                  source: str = "", reason: str = "") -> dict:
    """
    Apply one Bayesian update step to a node.
    corroborate=True  → evidence supports the belief (confidence rises)
    corroborate=False → evidence contradicts the belief (confidence drops)
    weight ∈ [0,1]: strength of evidence (1=strong, 0.3=weak).
    Returns {node_id, old_conf, new_conf, old_state, new_state}.
    """
    w = max(0.0, min(1.0, weight))
    with _conn() as c:
        row = c.execute(
            "SELECT id, ticker, node_type, confidence, belief_state, evidence_count FROM nodes WHERE id=?",
            (node_id,),
        ).fetchone()
        if not row:
            return {}

        old_conf  = row["confidence"] if row["confidence"] is not None else 0.5
        old_state = row["belief_state"] or "forming"
        ev_count  = (row["evidence_count"] or 1)

        if corroborate:
            new_conf = old_conf + (1.0 - old_conf) * w * 0.30
            ev_count += 1
        else:
            new_conf = old_conf * (1.0 - w * 0.55)

        new_conf  = round(max(0.05, min(0.95, new_conf)), 4)
        new_state = _state_for(new_conf)

        c.execute(
            """UPDATE nodes SET confidence=?, belief_state=?, evidence_count=?
               WHERE id=?""",
            (new_conf, new_state, ev_count, node_id),
        )
        if old_conf != new_conf:
            c.execute(
                """INSERT INTO belief_log
                   (node_id, ticker, node_type, old_conf, new_conf,
                    old_state, new_state, reason, source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (node_id, row["ticker"], row["node_type"],
                 old_conf, new_conf, old_state, new_state,
                 reason or ("corroborated" if corroborate else "contradicted"),
                 source),
            )

    return {
        "node_id":   node_id,
        "old_conf":  old_conf,
        "new_conf":  new_conf,
        "old_state": old_state,
        "new_state": new_state,
    }


def corroborate(node_id: int, weight: float = 1.0, source: str = "", reason: str = "") -> dict:
    """Convenience: corroborating evidence for a node."""
    return update_belief(node_id, corroborate=True, weight=weight, source=source, reason=reason)


def contradict(node_id: int, weight: float = 1.0, source: str = "", reason: str = "") -> dict:
    """Convenience: contradicting evidence for a node."""
    return update_belief(node_id, corroborate=False, weight=weight, source=source, reason=reason)


def decay_belief(node_id: int, days: float = 1.0) -> dict:
    """
    Apply temporal decay: older evidence loses weight.
    Decay rate: ~3% confidence loss per day toward the midpoint (0.5).
    """
    with _conn() as c:
        row = c.execute(
            "SELECT confidence, belief_state, ticker, node_type FROM nodes WHERE id=?",
            (node_id,),
        ).fetchone()
        if not row:
            return {}
        old_conf  = row["confidence"] or 0.5
        old_state = row["belief_state"] or "forming"
        # Pull toward 0.5 at 3% per day
        delta     = (old_conf - 0.5) * (1.0 - (0.97 ** days))
        new_conf  = round(max(0.05, min(0.95, old_conf - delta)), 4)
        new_state = _state_for(new_conf)
        if abs(new_conf - old_conf) > 0.001:
            c.execute("UPDATE nodes SET confidence=?, belief_state=? WHERE id=?",
                      (new_conf, new_state, node_id))
            c.execute(
                """INSERT INTO belief_log
                   (node_id, ticker, node_type, old_conf, new_conf,
                    old_state, new_state, reason)
                   VALUES (?,?,?,?,?,?,?,'temporal_decay')""",
                (node_id, row["ticker"], row["node_type"],
                 old_conf, new_conf, old_state, new_state),
            )
    return {"node_id": node_id, "old_conf": old_conf, "new_conf": new_conf, "new_state": new_state}


def retract_belief(node_id: int, reason: str = "explicit_retraction") -> dict:
    """Force-retract a belief (set confidence to 0.10, state to retracted)."""
    with _conn() as c:
        row = c.execute(
            "SELECT confidence, belief_state, ticker, node_type FROM nodes WHERE id=?",
            (node_id,),
        ).fetchone()
        if not row:
            return {}
        c.execute("UPDATE nodes SET confidence=0.10, belief_state='retracted' WHERE id=?", (node_id,))
        c.execute(
            """INSERT INTO belief_log
               (node_id, ticker, node_type, old_conf, new_conf,
                old_state, new_state, reason)
               VALUES (?,?,?,?,0.10,?,'retracted',?)""",
            (node_id, row["ticker"], row["node_type"],
             row["confidence"], row["belief_state"], reason),
        )
    return {"node_id": node_id, "new_state": "retracted"}


# ── Contradiction detection ───────────────────────────────────────────────────

def detect_contradictions(ticker: str) -> list[dict]:
    """
    Find contradictions in the knowledge graph for one ticker.
    Returns list of {node_id_a, node_id_b, contradiction_type, description}.

    Contradiction types:
      trend_flip    — TrendSummary improving then deteriorating for same metric
      signal_miss   — ConfluenceSignal BUY but analyst says AVOID
      mgmt_vs_result — positive management tone but results missed
    """
    contradictions = []
    with _conn() as c:
        # Trend flip: same metric, conflicting direction
        rows = c.execute(
            """SELECT id, json_extract(properties,'$.metric') as metric,
                      json_extract(properties,'$.direction') as direction,
                      created_at
               FROM nodes
               WHERE node_type='TrendSummary' AND ticker=?
                 AND belief_state != 'retracted'
               ORDER BY metric, created_at DESC""",
            (ticker,),
        ).fetchall()
        # Group by metric, check if first (latest) contradicts second (prior)
        seen = {}
        for r in rows:
            m = r["metric"]
            if m not in seen:
                seen[m] = r
            else:
                prior = seen[m]
                if r["direction"] and prior["direction"] and r["direction"] != prior["direction"]:
                    contradictions.append({
                        "node_id_a":          prior["id"],
                        "node_id_b":          r["id"],
                        "contradiction_type": "trend_flip",
                        "description":        f"{ticker} {m}: was {r['direction']}, now {prior['direction']}",
                    })

        # Signal vs analyst: ConfluenceSignal vs analyst_synthesis
        signal_row = c.execute(
            """SELECT id FROM nodes WHERE node_type='ConfluenceSignal'
               AND ticker=? AND belief_state != 'retracted'
               ORDER BY created_at DESC LIMIT 1""", (ticker,),
        ).fetchone()
        analyst_ep = c.execute(
            """SELECT id, json_extract(metadata,'$.recommendation') as rec
               FROM episodes WHERE episode_type='analyst_synthesis' AND ticker=?
               ORDER BY created_at DESC LIMIT 1""", (ticker,),
        ).fetchone()
        if signal_row and analyst_ep:
            rec = analyst_ep["rec"]
            if rec == "AVOID":
                contradictions.append({
                    "node_id_a":          signal_row["id"],
                    "node_id_b":          None,
                    "contradiction_type": "signal_vs_analyst",
                    "description":        f"{ticker}: ConfluenceSignal BUY but analyst says AVOID",
                })

    return contradictions


def resolve_contradictions(ticker: str) -> int:
    """
    Detect contradictions for a ticker and apply revision:
    - trend_flip: contradict older TrendSummary (reduce confidence)
    - signal_vs_analyst: contest ConfluenceSignal
    Returns count of revisions applied.
    """
    revisions = 0
    for c in detect_contradictions(ticker):
        if c["contradiction_type"] == "trend_flip":
            # Older node (node_id_b) gets contradicted — it's the prior belief
            if c["node_id_b"]:
                contradict(c["node_id_b"], weight=0.6, reason="trend_flip_detected")
                revisions += 1
        elif c["contradiction_type"] == "signal_vs_analyst":
            if c["node_id_a"]:
                contradict(c["node_id_a"], weight=0.4, reason="analyst_override_AVOID")
                revisions += 1
    return revisions


# ── Bulk operations ───────────────────────────────────────────────────────────

def decay_all_stale(days_threshold: float = 7.0) -> int:
    """Apply temporal decay to all nodes not updated in N days."""
    cutoff = (datetime.now() - timedelta(days=days_threshold)).isoformat()
    with _conn() as c:
        old_nodes = c.execute(
            """SELECT id, (julianday('now') - julianday(created_at)) as age_days
               FROM nodes WHERE created_at < ? AND belief_state != 'retracted'""",
            (cutoff,),
        ).fetchall()
    decayed = 0
    for row in old_nodes:
        result = decay_belief(row["id"], days=min(row["age_days"] or 1, 30))
        if result:
            decayed += 1
    return decayed


def belief_summary(ticker: str) -> dict:
    """Return per-state breakdown of belief nodes for a ticker."""
    with _conn() as c:
        rows = c.execute(
            """SELECT belief_state, COUNT(*) as n, AVG(confidence) as avg_conf
               FROM nodes WHERE ticker=? GROUP BY belief_state""",
            (ticker,),
        ).fetchall()
    return {r["belief_state"] or "unknown": {"count": r["n"], "avg_conf": round(r["avg_conf"] or 0.5, 3)} for r in rows}


def recent_revisions(ticker: str = None, limit: int = 20) -> list[dict]:
    """Return recent belief log entries."""
    with _conn() as c:
        if ticker:
            rows = c.execute(
                "SELECT * FROM belief_log WHERE ticker=? ORDER BY created_at DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM belief_log ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]
