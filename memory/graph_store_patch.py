"""
graph_store_patch.py — monkey-patch GraphStore with belief-aware methods.

Adds to GraphStore instances:
  upsert_node_with_confidence(node_type, name, ticker, properties, confidence, corroborate)
  corroborate_node(node_id, weight, source, reason) → dict
  contradict_node(node_id, weight, source, reason) → dict
  get_confident_nodes(ticker, min_confidence, belief_state, node_type, limit) → list[dict]

Call patch_graph_store() once at startup (idempotent via _belief_patched flag).
Imported by jobs/orchestrator.py and jobs/dynamics_runner.py.
"""
from __future__ import annotations
from pathlib import Path

REPO = Path(__file__).parent.parent


def patch_graph_store():
    """Monkey-patch GraphStore with belief-aware helpers. Idempotent."""
    try:
        from memory.graph_store import GraphStore
    except ImportError:
        return

    if getattr(GraphStore, "_belief_patched", False):
        return

    from memory import belief_engine as be

    def upsert_node_with_confidence(
        self,
        node_type: str,
        name: str,
        ticker: str = None,
        properties: dict = None,
        confidence: float = None,
        corroborate: bool = False,
    ) -> int:
        """
        Upsert a node, then optionally update its belief.
        confidence overrides the initial value if the node is new.
        corroborate=True applies a Bayesian update to an existing node.
        Returns node_id.
        """
        node_id = self.upsert_node(node_type=node_type, name=name, ticker=ticker, properties=properties)
        if node_id is None:
            return node_id

        import sqlite3
        db_path = REPO / "memory_store" / "graph.db"
        with sqlite3.connect(str(db_path), timeout=30) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT confidence, evidence_count FROM nodes WHERE id=?", (node_id,)
            ).fetchone()
            if row is None:
                return node_id
            is_new = (row["evidence_count"] is None or row["evidence_count"] <= 1)

        if is_new and confidence is not None:
            import sqlite3
            db_path = REPO / "memory_store" / "graph.db"
            state = be._state_for(confidence)
            with sqlite3.connect(str(db_path), timeout=30) as c:
                c.execute(
                    "UPDATE nodes SET confidence=?, belief_state=? WHERE id=?",
                    (confidence, state, node_id),
                )
        elif corroborate:
            be.corroborate(node_id, weight=0.6, reason="re_ingestion")

        return node_id

    def corroborate_node(
        self, node_id: int, weight: float = 1.0, source: str = "", reason: str = ""
    ) -> dict:
        """Apply corroborating evidence to a node."""
        return be.corroborate(node_id, weight=weight, source=source, reason=reason)

    def contradict_node(
        self, node_id: int, weight: float = 1.0, source: str = "", reason: str = ""
    ) -> dict:
        """Apply contradicting evidence to a node."""
        return be.contradict(node_id, weight=weight, source=source, reason=reason)

    def get_confident_nodes(
        self,
        ticker: str = None,
        min_confidence: float = 0.60,
        belief_state: str = None,
        node_type: str = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Return nodes filtered by confidence/belief_state.
        Useful for downstream analysis that should only act on well-supported beliefs.
        """
        import sqlite3
        db_path = REPO / "memory_store" / "graph.db"
        clauses = ["confidence >= ?"]
        params: list = [min_confidence]
        if ticker:
            clauses.append("ticker=?")
            params.append(ticker.upper())
        if belief_state:
            clauses.append("belief_state=?")
            params.append(belief_state)
        if node_type:
            clauses.append("node_type=?")
            params.append(node_type)
        where = " AND ".join(clauses)
        params.append(limit)
        with sqlite3.connect(str(db_path), timeout=30) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                f"SELECT * FROM nodes WHERE {where} ORDER BY confidence DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    GraphStore.upsert_node_with_confidence = upsert_node_with_confidence
    GraphStore.corroborate_node = corroborate_node
    GraphStore.contradict_node = contradict_node
    GraphStore.get_confident_nodes = get_confident_nodes
    GraphStore._belief_patched = True


# Auto-patch on import
patch_graph_store()
