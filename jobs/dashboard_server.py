"""
NSE-style equity research dashboard — FastAPI backend.

Serves live data from memory_store/ and graph.db.

Run:
    python jobs/dashboard_server.py
    python jobs/dashboard_server.py --port 8080

Endpoints:
    GET /api/market        — today's snapshot + breadth + movers
    GET /api/signals       — ConfluenceSignal nodes (latest 50)
    GET /api/trends        — TrendSummary nodes (latest 50)
    GET /api/announcements — recent corporate announcements
    GET /api/agents        — task log (swarm activity)
    GET /api/graph/stats   — knowledge graph statistics
    GET /api/watchlist/{ticker} — full company detail
    GET /api/results/{ticker}  — quarterly results
    GET /api/sectors       — sector breakdown from sector_map
"""
import sys
import json
import sqlite3
import argparse
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import HTMLResponse, FileResponse
    import uvicorn
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install fastapi uvicorn")
    sys.exit(1)

REPO = Path(__file__).parent.parent
STORE = REPO / "memory_store"

app = FastAPI(title="Equity Research Dashboard", version="1.0")

# ── helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default if default is not None else {}


def _graph_conn() -> sqlite3.Connection:
    db = STORE / "graph.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _latest_price_file() -> Path | None:
    files = sorted((STORE / "prices").glob("*.json")) if (STORE / "prices").exists() else []
    return files[-1] if files else None


def _sector_map() -> dict:
    return _load_json(STORE / "sector_map.json", {})


def _company_meta() -> dict:
    return _load_json(STORE / "company_meta.json", {})


# ── market ────────────────────────────────────────────────────────────────────

@app.get("/api/market")
def get_market():
    pf = _latest_price_file()
    if not pf:
        return {"error": "No price data available", "date": None}

    data = _load_json(pf, {})
    stats = data.get("stats", {})

    # breadth from graph PriceSnapshot nodes
    advances = declines = unchanged = 0
    conn = _graph_conn()
    if conn:
        try:
            rows = conn.execute(
                "SELECT properties FROM nodes WHERE node_type='PriceSnapshot'"
            ).fetchall()
            for r in rows:
                props = json.loads(r["properties"])
                pct = props.get("pct_change", 0) or 0
                if pct > 0.1:
                    advances += 1
                elif pct < -0.1:
                    declines += 1
                else:
                    unchanged += 1

            # Index data
            idx_rows = conn.execute(
                "SELECT name, properties FROM nodes WHERE node_type='Index' "
                "ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
            indices = []
            for r in idx_rows:
                props = json.loads(r["properties"])
                indices.append({
                    "name": r["name"],
                    "value": props.get("close") or props.get("last_price"),
                    "change": props.get("pct_change"),
                })
        finally:
            conn.close()
    else:
        advances = declines = unchanged = 0
        indices = []

    return {
        "date": data.get("date", pf.stem),
        "generated_at": data.get("generated_at"),
        "stats": stats,
        "breadth": {
            "advances": advances or stats.get("advancing", 0),
            "declines": declines or stats.get("declining", 0),
            "unchanged": unchanged or stats.get("unchanged", 0),
        },
        "movers_up": data.get("movers_up", [])[:20],
        "movers_down": data.get("movers_down", [])[:20],
        "indices": indices,
    }


# ── signals ───────────────────────────────────────────────────────────────────

@app.get("/api/signals")
def get_signals(limit: int = 50):
    conn = _graph_conn()
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT id, name, ticker, properties, created_at FROM nodes "
            "WHERE node_type='ConfluenceSignal' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            props = json.loads(r["properties"])
            sig_type = props.get("type", props.get("signal_type", ""))
        direction = "bullish" if "BUY" in sig_type.upper() else "bearish" if "SELL" in sig_type.upper() else ""
        out.append({
                "id": r["id"],
                "name": r["name"],
                "ticker": r["ticker"],
                "signal_type": sig_type,
                "direction": direction,
                "strength": props.get("score", props.get("strength", "")),
                "summary": props.get("reason", props.get("summary", "")),
                "metrics": [props["metric"]] if props.get("metric") else props.get("metrics", []),
                "score": props.get("score"),
                "near_low_pct": props.get("near_low_pct"),
                "streak": props.get("streak"),
                "close": props.get("close"),
                "year_low": props.get("year_low"),
                "year_high": props.get("year_high"),
                "created_at": r["created_at"],
            })
        return out
    finally:
        conn.close()


# ── trends ────────────────────────────────────────────────────────────────────

@app.get("/api/trends")
def get_trends(limit: int = 50, ticker: str = None):
    conn = _graph_conn()
    if not conn:
        return []
    try:
        if ticker:
            rows = conn.execute(
                "SELECT id, name, ticker, properties, created_at FROM nodes "
                "WHERE node_type='TrendSummary' AND ticker=? "
                "ORDER BY created_at DESC LIMIT ?",
                (ticker.upper(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, ticker, properties, created_at FROM nodes "
                "WHERE node_type='TrendSummary' "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            props = json.loads(r["properties"])
            out.append({
                "id": r["id"],
                "name": r["name"],
                "ticker": r["ticker"],
                "metric": props.get("metric", ""),
                "direction": props.get("direction", ""),
                "periods": props.get("periods", 0),
                "summary": props.get("summary", ""),
                "created_at": r["created_at"],
            })
        return out
    finally:
        conn.close()


# ── announcements ─────────────────────────────────────────────────────────────

@app.get("/api/announcements")
def get_announcements(ticker: str = None, limit: int = 30):
    try:
        from memory.task_log import get_recent_announcements
        return get_recent_announcements(ticker=ticker, limit=limit)
    except Exception as e:
        # fallback: read rolling_announcements.json if it exists
        f = STORE / "rolling_announcements.json"
        if f.exists():
            ann = _load_json(f, [])
            if ticker:
                ann = [a for a in ann if a.get("ticker") == ticker.upper()]
            return ann[:limit]
        return {"error": str(e)}


# ── agents / swarm ────────────────────────────────────────────────────────────

@app.get("/api/agents")
def get_agents():
    try:
        from memory.task_log import get_incomplete_tasks
        incomplete = get_incomplete_tasks()

        # Also get recent completed from task log file
        tlog = REPO / "logs" / "tasks.log"
        recent = []
        if tlog.exists():
            lines = tlog.read_text().strip().splitlines()
            for line in reversed(lines[-200:]):
                try:
                    d = json.loads(line)
                    recent.append({
                        "task_id": d.get("task_id"),
                        "status": d.get("status"),
                        "description": d.get("description", "")[:80],
                        "timestamp": d.get("timestamp", ""),
                    })
                except Exception:
                    pass
            recent = recent[:30]

        return {
            "incomplete": incomplete,
            "recent": recent,
            "swarm_size": 8,  # ThreadPoolExecutor target
        }
    except Exception as e:
        return {"error": str(e), "incomplete": [], "recent": []}


# ── graph stats ───────────────────────────────────────────────────────────────

@app.get("/api/graph/stats")
def get_graph_stats():
    conn = _graph_conn()
    if not conn:
        return {"error": "Graph database not found"}
    try:
        nodes    = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        edges    = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        pending  = conn.execute("SELECT COUNT(*) FROM nodes WHERE enriched=0").fetchone()[0]
        by_type  = conn.execute(
            "SELECT node_type, COUNT(*) as n FROM nodes GROUP BY node_type ORDER BY n DESC"
        ).fetchall()

        # Recent activity
        recent_nodes = conn.execute(
            "SELECT node_type, ticker, name, created_at FROM nodes "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()

        return {
            "nodes": nodes,
            "edges": edges,
            "episodes": episodes,
            "pending_enrichment": pending,
            "by_type": {r["node_type"]: r["n"] for r in by_type},
            "recent_nodes": [dict(r) for r in recent_nodes],
        }
    finally:
        conn.close()


# ── sectors ───────────────────────────────────────────────────────────────────

@app.get("/api/sectors")
def get_sectors():
    sm = _sector_map()
    sectors: dict[str, dict] = {}
    for ticker, info in sm.items():
        sec = info.get("sector") or info.get("macro_sector") or "Unknown"
        if sec not in sectors:
            sectors[sec] = {"name": sec, "count": 0, "tickers": []}
        sectors[sec]["count"] += 1
        if len(sectors[sec]["tickers"]) < 5:
            sectors[sec]["tickers"].append(ticker)
    return sorted(sectors.values(), key=lambda x: -x["count"])


# ── watchlist / company detail ────────────────────────────────────────────────

@app.get("/api/watchlist/{ticker}")
def get_watchlist(ticker: str):
    ticker = ticker.upper()
    sm = _sector_map()
    meta = _company_meta()

    info = sm.get(ticker, {})
    m = meta.get(ticker, {})

    conn = _graph_conn()
    company_node = {}
    latest_metrics = []
    latest_trends = []
    management = []

    if conn:
        try:
            # Company node
            cn = conn.execute(
                "SELECT * FROM nodes WHERE node_type='Company' AND ticker=? LIMIT 1",
                (ticker,),
            ).fetchone()
            if cn:
                company_node = dict(cn)
                company_node["properties"] = json.loads(company_node["properties"])

            # Latest 8 MetricValue nodes
            mv = conn.execute(
                "SELECT name, properties, created_at FROM nodes "
                "WHERE node_type='MetricValue' AND ticker=? "
                "ORDER BY json_extract(properties,'$.period_date') DESC LIMIT 16",
                (ticker,),
            ).fetchall()
            latest_metrics = []
            for r in mv:
                p = json.loads(r["properties"])
                latest_metrics.append({
                    "metric": p.get("metric"),
                    "value": p.get("value"),
                    "unit": p.get("unit"),
                    "period": p.get("period"),
                    "direction": p.get("direction"),
                })

            # TrendSummary
            ts = conn.execute(
                "SELECT name, properties FROM nodes "
                "WHERE node_type='TrendSummary' AND ticker=? "
                "ORDER BY created_at DESC LIMIT 10",
                (ticker,),
            ).fetchall()
            latest_trends = []
            for r in ts:
                p = json.loads(r["properties"])
                latest_trends.append({
                    "metric": p.get("metric"),
                    "direction": p.get("direction"),
                    "periods": p.get("periods"),
                    "summary": p.get("summary", ""),
                })

            # ManagementStatement
            ms = conn.execute(
                "SELECT name, properties, created_at FROM nodes "
                "WHERE node_type='ManagementStatement' AND ticker=? "
                "ORDER BY created_at DESC LIMIT 5",
                (ticker,),
            ).fetchall()
            management = []
            for r in ms:
                p = json.loads(r["properties"])
                management.append({
                    "text": p.get("text", r["name"])[:200],
                    "source": p.get("source", ""),
                    "created_at": r["created_at"],
                })
        finally:
            conn.close()

    # Quarterly results
    results = []
    rpath = STORE / "results" / f"{ticker}.jsonl"
    if rpath.exists():
        for line in rpath.read_text().strip().splitlines()[-8:]:
            try:
                results.append(json.loads(line))
            except Exception:
                pass

    return {
        "ticker": ticker,
        "company": company_node.get("name", ticker),
        "sector": info.get("sector") or info.get("macro_sector"),
        "industry": info.get("industry"),
        "isin": m.get("isin"),
        "fo_stock": m.get("fo_stock", False),
        "metrics": latest_metrics,
        "trends": latest_trends,
        "management": management,
        "results": results[-4:],  # last 4 quarters
    }


# ── results ───────────────────────────────────────────────────────────────────

@app.get("/api/results/{ticker}")
def get_results(ticker: str, limit: int = 8):
    ticker = ticker.upper()
    rpath = STORE / "results" / f"{ticker}.jsonl"
    if not rpath.exists():
        raise HTTPException(404, f"No results for {ticker}")
    results = []
    for line in rpath.read_text().strip().splitlines():
        try:
            results.append(json.loads(line))
        except Exception:
            pass
    results.sort(key=lambda x: x.get("period_end", ""))
    return results[-limit:]


# ── dashboard HTML ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    html_path = REPO / "static" / "dashboard" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Dashboard not found — run build_dashboard.py</h1>")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Equity Research Dashboard Server")
    parser.add_argument("--port", type=int, default=7860, help="Port (default 7860)")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default 0.0.0.0)")
    args = parser.parse_args()

    print(f"Starting dashboard at http://localhost:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
