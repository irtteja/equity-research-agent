"""
Knowledge graph store — SQLite-backed nodes, edges, and episodes.

The graph is the shared memory all agents read from and write to.
Every significant event in the system becomes an episode; episodes
feed nodes and edges; agents traverse edges to find context.

Schema:
  nodes    — entities: Company, MetricValue, Filing, ManagementStatement, Gap, Patch
  edges    — relationships: PRECEDED_BY, CORROBORATES, CONTRADICTS, TRIGGERED, FIXED_BY
  episodes — raw event log (audit trail + text search source)

Agents interact through the graph, not through direct calls:
  IngestionAgent writes MetricValue nodes → graph
  ContextAgent   reads unenriched nodes  → adds PRECEDED_BY edges → marks enriched
  TrendAgent     reads edge chains       → writes trend summaries  → graph
  AlertAgent     reads trend nodes       → decides what to surface
"""
import json
import sqlite3
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Any

REPO  = Path(__file__).parent.parent
DB    = REPO / "memory_store" / "graph.db"


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    node_type   TEXT NOT NULL,
    name        TEXT NOT NULL,
    ticker      TEXT,
    properties  TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL,
    enriched    INTEGER DEFAULT 0,
    enriched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_ticker    ON nodes(ticker);
CREATE INDEX IF NOT EXISTS idx_nodes_type      ON nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_nodes_enriched  ON nodes(enriched);

CREATE TABLE IF NOT EXISTS edges (
    id          TEXT PRIMARY KEY,
    edge_type   TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    properties  TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES nodes(id),
    FOREIGN KEY (target_id) REFERENCES nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_edges_source    ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target    ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_type      ON edges(edge_type);

CREATE TABLE IF NOT EXISTS episodes (
    id           TEXT PRIMARY KEY,
    episode_type TEXT NOT NULL,
    ticker       TEXT,
    text         TEXT NOT NULL,
    source       TEXT,
    metadata     TEXT DEFAULT '{}',
    created_at   TEXT NOT NULL,
    processed    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_episodes_ticker    ON episodes(ticker);
CREATE INDEX IF NOT EXISTS idx_episodes_processed ON episodes(processed);
CREATE INDEX IF NOT EXISTS idx_episodes_type      ON episodes(episode_type);
"""


# ── GraphStore ────────────────────────────────────────────────────────────────

class GraphStore:
    """
    SQLite knowledge graph. Thread-safe (each call opens its own connection).
    All methods are idempotent — safe to call multiple times with the same data.
    """

    def __init__(self, db_path: Path = DB):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript(_DDL)

    # ── Episodes ──────────────────────────────────────────────────────────────

    def add_episode(
        self,
        episode_type: str,
        text: str,
        source: str = "",
        ticker: str = "",
        metadata: dict = None,
    ) -> str:
        """Append a raw event to the episode log. Returns episode id."""
        eid = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO episodes (id, episode_type, ticker, text, source, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (eid, episode_type, ticker.upper() if ticker else "",
                 text, source, json.dumps(metadata or {}, default=str),
                 datetime.now().isoformat()),
            )
        return eid

    def pending_episodes(self, limit: int = 100) -> list[dict]:
        """Episodes not yet processed into graph nodes/edges."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE processed=0 ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_episode_processed(self, episode_id: str):
        with self._conn() as conn:
            conn.execute("UPDATE episodes SET processed=1 WHERE id=?", (episode_id,))

    # ── Nodes ─────────────────────────────────────────────────────────────────

    def add_node(
        self,
        node_id: str,
        node_type: str,
        name: str,
        ticker: str = "",
        properties: dict = None,
    ) -> str:
        """Upsert a node. Returns node_id."""
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id, properties FROM nodes WHERE id=?", (node_id,)
            ).fetchone()
            if existing:
                # Merge new properties into existing
                merged = json.loads(existing["properties"])
                merged.update(properties or {})
                conn.execute(
                    "UPDATE nodes SET properties=? WHERE id=?",
                    (json.dumps(merged, default=str), node_id),
                )
            else:
                conn.execute(
                    "INSERT INTO nodes (id, node_type, name, ticker, properties, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (node_id, node_type, name,
                     ticker.upper() if ticker else "",
                     json.dumps(properties or {}, default=str),
                     datetime.now().isoformat()),
                )
        return node_id

    def get_node(self, node_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["properties"] = json.loads(d["properties"])
        return d

    def pending_enrichment(self, node_type: str = None, limit: int = 50) -> list[dict]:
        """Nodes not yet processed by the ContextAgent."""
        with self._conn() as conn:
            if node_type:
                rows = conn.execute(
                    "SELECT * FROM nodes WHERE enriched=0 AND node_type=? "
                    "ORDER BY created_at LIMIT ?",
                    (node_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM nodes WHERE enriched=0 ORDER BY created_at LIMIT ?",
                    (limit,),
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["properties"] = json.loads(d["properties"])
            out.append(d)
        return out

    def mark_enriched(self, node_id: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE nodes SET enriched=1, enriched_at=? WHERE id=?",
                (datetime.now().isoformat(), node_id),
            )

    # ── Edges ─────────────────────────────────────────────────────────────────

    def add_edge(
        self,
        edge_type: str,
        source_id: str,
        target_id: str,
        properties: dict = None,
    ) -> str:
        """Add a directed edge. Idempotent on (edge_type, source_id, target_id)."""
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id FROM edges WHERE edge_type=? AND source_id=? AND target_id=?",
                (edge_type, source_id, target_id),
            ).fetchone()
            if existing:
                # Update properties if edge already exists
                if properties:
                    conn.execute(
                        "UPDATE edges SET properties=? WHERE id=?",
                        (json.dumps(properties, default=str), existing["id"]),
                    )
                return existing["id"]
            eid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO edges (id, edge_type, source_id, target_id, properties, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (eid, edge_type, source_id, target_id,
                 json.dumps(properties or {}, default=str),
                 datetime.now().isoformat()),
            )
        return eid

    def get_edges(
        self,
        node_id: str,
        edge_type: str = None,
        direction: str = "out",   # "out" | "in" | "both"
    ) -> list[dict]:
        """Return edges connected to node_id."""
        with self._conn() as conn:
            if direction == "out":
                clause = "source_id=?"
                params: tuple = (node_id,)
            elif direction == "in":
                clause = "target_id=?"
                params = (node_id,)
            else:
                clause = "(source_id=? OR target_id=?)"
                params = (node_id, node_id)

            if edge_type:
                rows = conn.execute(
                    f"SELECT * FROM edges WHERE {clause} AND edge_type=?",
                    params + (edge_type,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM edges WHERE {clause}", params
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["properties"] = json.loads(d["properties"])
            out.append(d)
        return out

    def get_neighbors(
        self,
        node_id: str,
        edge_type: str = None,
        direction: str = "out",
    ) -> list[dict]:
        """Return neighboring nodes (follows edges from node_id)."""
        edges = self.get_edges(node_id, edge_type=edge_type, direction=direction)
        neighbor_ids = []
        for e in edges:
            nid = e["target_id"] if e["source_id"] == node_id else e["source_id"]
            neighbor_ids.append(nid)
        nodes = []
        for nid in neighbor_ids:
            n = self.get_node(nid)
            if n:
                nodes.append(n)
        return nodes

    # ── Queries ───────────────────────────────────────────────────────────────

    def search_nodes(
        self,
        query: str,
        node_type: str = None,
        ticker: str = None,
        limit: int = 10,
    ) -> list[dict]:
        """Text search across node name and properties JSON."""
        with self._conn() as conn:
            base = "SELECT * FROM nodes WHERE (name LIKE ? OR properties LIKE ?)"
            params: list = [f"%{query}%", f"%{query}%"]
            if node_type:
                base += " AND node_type=?"
                params.append(node_type)
            if ticker:
                base += " AND ticker=?"
                params.append(ticker.upper())
            base += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(base, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["properties"] = json.loads(d["properties"])
            out.append(d)
        return out

    def get_metric_history(
        self,
        ticker: str,
        metric: str,
        n: int = 8,
    ) -> list[dict]:
        """
        Return the last N MetricValue nodes for a given ticker + metric,
        ordered by period_date descending (most recent first).
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM nodes WHERE node_type='MetricValue' "
                "AND ticker=? AND properties LIKE ? "
                "ORDER BY json_extract(properties,'$.period_date') DESC LIMIT ?",
                (ticker.upper(), f'%"metric": "{metric}"%', n),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["properties"] = json.loads(d["properties"])
            out.append(d)
        return out

    def stats(self) -> dict:
        with self._conn() as conn:
            nodes    = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edges    = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            pending  = conn.execute("SELECT COUNT(*) FROM nodes WHERE enriched=0").fetchone()[0]
            by_type  = conn.execute(
                "SELECT node_type, COUNT(*) as n FROM nodes GROUP BY node_type"
            ).fetchall()
        return {
            "nodes": nodes, "edges": edges, "episodes": episodes,
            "pending_enrichment": pending,
            "by_type": {r["node_type"]: r["n"] for r in by_type},
        }


# ── Module-level singleton ────────────────────────────────────────────────────

_store: GraphStore | None = None

def get_store() -> GraphStore:
    global _store
    if _store is None:
        _store = GraphStore()
    return _store


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))
    p = argparse.ArgumentParser(description="Query the knowledge graph")
    p.add_argument("--stats",  action="store_true", help="Print graph statistics")
    p.add_argument("--search", help="Search nodes by text")
    p.add_argument("--ticker", help="Filter by ticker")
    p.add_argument("--metric-history", help="Show metric history: TICKER:METRIC")
    p.add_argument("--node",   help="Show a single node by id")
    args = p.parse_args()

    gs = get_store()

    if args.stats:
        s = gs.stats()
        print(f"Nodes: {s['nodes']}  Edges: {s['edges']}  Episodes: {s['episodes']}")
        print(f"Pending enrichment: {s['pending_enrichment']}")
        for t, n in sorted(s["by_type"].items()):
            print(f"  {t}: {n}")

    if args.search:
        results = gs.search_nodes(args.search, ticker=args.ticker)
        for r in results:
            print(f"[{r['node_type']}] {r['id']} — {r['name']}")
            for k, v in r["properties"].items():
                print(f"    {k}: {v}")

    if args.metric_history:
        ticker, metric = args.metric_history.split(":", 1)
        history = gs.get_metric_history(ticker, metric)
        print(f"{ticker} — {metric} ({len(history)} periods):")
        for h in history:
            p = h["properties"]
            delta_str = ""
            edges = gs.get_edges(h["id"], edge_type="PRECEDED_BY")
            if edges:
                ep = edges[0]["properties"]
                delta_str = f"  Δ {ep.get('delta_pct','?')}% ({ep.get('direction','')})"
            print(f"  {p.get('period','?')}: {p.get('value','?')} {p.get('unit','')}{delta_str}")

    if args.node:
        n = gs.get_node(args.node)
        if n:
            print(json.dumps(n, indent=2, default=str))
        else:
            print(f"Node not found: {args.node}")
