"""
Per-ticker node coverage map — what graph node types exist per ticker and their freshness.

Table: ticker_coverage(ticker, node_type, count, last_updated, staleness_days, created_at)
Rebuilt by CoverageAgent after each ingestion cycle.

Used by meta_cognition.py to find coverage gaps and prioritise re-ingestion.
Coverage score 0–1: (unique node types / FULL_TYPES) weighted by freshness.
"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"

# Node types that constitute "full coverage" for a mainboard equity ticker
FULL_TYPES = {
    "PriceSnapshot", "TrendSummary", "ConfluenceSignal",
    "CorporateEvent", "ManagementStatement", "MetricValue",
    "DeliveryMetric", "InsiderTrade", "CorporateAction",
}
FULL_SCORE = len(FULL_TYPES)


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _ensure_table():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS ticker_coverage (
                ticker        TEXT NOT NULL,
                node_type     TEXT NOT NULL,
                count         INTEGER DEFAULT 0,
                last_updated  TEXT,
                staleness_days REAL DEFAULT 999,
                created_at    TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (ticker, node_type)
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tc_ticker ON ticker_coverage(ticker)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tc_stale ON ticker_coverage(staleness_days)")


_ensure_table()


def rebuild_coverage(tickers: list[str] | None = None) -> int:
    """
    Recompute ticker_coverage from graph.db nodes.
    If tickers=None, rebuilds for all tickers that have nodes.
    Returns number of (ticker, node_type) rows written.
    """
    now = datetime.now()
    with _conn() as c:
        if tickers:
            placeholders = ",".join("?" * len(tickers))
            rows = c.execute(
                f"""SELECT ticker, node_type,
                           COUNT(*) as cnt,
                           MAX(created_at) as latest
                    FROM nodes
                    WHERE ticker IN ({placeholders}) AND ticker IS NOT NULL
                    GROUP BY ticker, node_type""",
                tickers,
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT ticker, node_type,
                          COUNT(*) as cnt,
                          MAX(created_at) as latest
                   FROM nodes
                   WHERE ticker IS NOT NULL
                   GROUP BY ticker, node_type"""
            ).fetchall()

        written = 0
        for r in rows:
            try:
                latest_dt = datetime.fromisoformat(r["latest"])
                staleness = (now - latest_dt).total_seconds() / 86400
            except Exception:
                staleness = 999.0
            c.execute(
                """INSERT INTO ticker_coverage (ticker, node_type, count, last_updated, staleness_days)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(ticker, node_type) DO UPDATE SET
                     count=excluded.count,
                     last_updated=excluded.last_updated,
                     staleness_days=excluded.staleness_days,
                     created_at=datetime('now')""",
                (r["ticker"], r["node_type"], r["cnt"], r["latest"], round(staleness, 2)),
            )
            written += 1
        return written


def coverage_score(ticker: str) -> float:
    """Return 0–1 coverage score for one ticker (more types + fresher = higher)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT node_type, staleness_days FROM ticker_coverage WHERE ticker=?",
            (ticker,),
        ).fetchall()
    if not rows:
        return 0.0
    type_set = {r["node_type"] for r in rows}
    present  = type_set & FULL_TYPES
    type_pct = len(present) / FULL_SCORE
    # freshness: 1.0 if all stale<1d, 0.5 if stale=7d, 0 if stale>30d
    avg_stale = sum(r["staleness_days"] for r in rows) / len(rows)
    freshness = max(0.0, 1.0 - avg_stale / 30.0)
    return round(type_pct * 0.6 + freshness * 0.4, 3)


def get_coverage(ticker: str) -> dict:
    """Return per-node-type coverage info for one ticker."""
    with _conn() as c:
        rows = c.execute(
            "SELECT node_type, count, last_updated, staleness_days FROM ticker_coverage WHERE ticker=?",
            (ticker,),
        ).fetchall()
    return {
        r["node_type"]: {
            "count":          r["count"],
            "last_updated":   r["last_updated"],
            "staleness_days": r["staleness_days"],
        }
        for r in rows
    }


def get_gaps(min_coverage: float = 0.4, limit: int = 200) -> list[dict]:
    """Return tickers with coverage_score below threshold, sorted by score asc."""
    with _conn() as c:
        tickers = [r[0] for r in c.execute(
            "SELECT DISTINCT ticker FROM ticker_coverage WHERE ticker IS NOT NULL"
        ).fetchall()]
    scored = [(t, coverage_score(t)) for t in tickers]
    scored.sort(key=lambda x: x[1])
    return [
        {"ticker": t, "score": s}
        for t, s in scored
        if s < min_coverage
    ][:limit]


def get_stale(node_type: str, days_threshold: float = 7.0, limit: int = 200) -> list[str]:
    """Return tickers whose node_type is older than days_threshold."""
    with _conn() as c:
        rows = c.execute(
            """SELECT ticker FROM ticker_coverage
               WHERE node_type=? AND staleness_days > ?
               ORDER BY staleness_days DESC LIMIT ?""",
            (node_type, days_threshold, limit),
        ).fetchall()
    return [r[0] for r in rows]


def missing_types(ticker: str) -> set[str]:
    """Return FULL_TYPES node types not yet present for this ticker."""
    have = set(get_coverage(ticker).keys())
    return FULL_TYPES - have


def top_covered(limit: int = 20) -> list[dict]:
    """Return the most comprehensively covered tickers."""
    with _conn() as c:
        tickers = [r[0] for r in c.execute(
            "SELECT DISTINCT ticker FROM ticker_coverage WHERE ticker IS NOT NULL"
        ).fetchall()]
    scored = [(t, coverage_score(t)) for t in tickers]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [{"ticker": t, "score": s} for t, s in scored[:limit]]
