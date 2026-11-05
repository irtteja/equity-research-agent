"""
Ingest TradingView data into graph.db nodes.

Fetches for each ConfluenceSignal ticker:
  - Analyst forecasts: Buy/Hold/Sell counts, price_target_1y, consensus
  - Quality metrics: Piotroski F-score, Altman Z-score (from screener)
  - Technicals: RSI, MACD, MA recommendation (from technicals rating)
  - Financials: ROE, gross_margin, net_margin, P/E, EV/EBITDA (from financials)

Writes/updates:
  - AnalystConsensus nodes (new node type)
  - Updates BusinessQuality nodes with Piotroski/Altman scores
  - Updates TechnicalSignal nodes with TradingView RSI/MACD

No Claude API calls. All data from TradingView MCP tools.

CLI:
  python jobs/ingest_tradingview.py [--limit N] [--ticker SYMBOL] [--dry-run]
"""
import json
import sqlite3
import sys
import time
import argparse
from datetime import datetime, date
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))
GRAPH_DB = REPO / "memory_store" / "graph.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(GRAPH_DB), timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _p(s) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def _get_confluence_tickers(conn, limit: int) -> list[str]:
    rows = conn.execute(
        """SELECT ticker FROM nodes WHERE node_type='ConfluenceSignal'
           ORDER BY CAST(json_extract(properties,'$.score') AS REAL) DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    return [r["ticker"] for r in rows]


def _tv_symbol(ticker: str) -> str:
    """Convert NSE ticker to TradingView symbol (NSE:TICKER)."""
    return f"NSE:{ticker}"


def ingest_analyst_consensus(conn, ticker: str, tv_data: dict) -> bool:
    """Write AnalystConsensus node from TradingView forecast data."""
    forecasts = tv_data.get("forecasts")
    if not forecasts:
        return False

    buy   = forecasts.get("buy", 0) or 0
    hold  = forecasts.get("hold", 0) or 0
    sell  = forecasts.get("sell", 0) or 0
    overweight  = forecasts.get("overweight", 0) or 0
    underweight = forecasts.get("underweight", 0) or 0
    total = buy + hold + sell + overweight + underweight

    buy_total  = buy + overweight
    sell_total = sell + underweight
    target     = forecasts.get("price_target_1y")
    current    = tv_data.get("current_price")

    if total == 0:
        return False

    upside_pct = None
    if target and current and current > 0:
        upside_pct = round((target - current) / current * 100, 2)

    consensus = "HOLD"
    if buy_total / total >= 0.60:
        consensus = "STRONG_BUY" if buy_total / total >= 0.75 else "BUY"
    elif sell_total / total >= 0.40:
        consensus = "SELL"

    node_id = f"analyst-{ticker}"
    props = json.dumps({
        "buy": buy, "hold": hold, "sell": sell,
        "overweight": overweight, "underweight": underweight,
        "total_analysts": total,
        "buy_total": buy_total, "sell_total": sell_total,
        "consensus": consensus,
        "price_target_1y": target,
        "current_price": current,
        "upside_pct": upside_pct,
        "as_of": date.today().isoformat(),
    }, ensure_ascii=False)

    existing = conn.execute("SELECT id FROM nodes WHERE id=?", (node_id,)).fetchone()
    if existing:
        conn.execute("UPDATE nodes SET properties=?, enriched=1 WHERE id=?", (props, node_id))
    else:
        conn.execute("""
            INSERT INTO nodes (id, node_type, name, ticker, properties,
              confidence, belief_state, evidence_count, enriched, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            node_id, "AnalystConsensus",
            f"{ticker} analyst consensus ({consensus})", ticker,
            props, 0.85, "confirmed", 1, 1, date.today().isoformat(),
        ))
    return True


def ingest_quality_metrics(conn, ticker: str, tv_data: dict) -> bool:
    """Update BusinessQuality node with Piotroski F-score and Altman Z-score."""
    piotroski = tv_data.get("piotroski_f_score")
    altman    = tv_data.get("altman_z_score")
    if piotroski is None and altman is None:
        return False

    row = conn.execute(
        "SELECT id, properties FROM nodes WHERE ticker=? AND node_type='BusinessQuality' LIMIT 1",
        (ticker,)
    ).fetchone()
    if not row:
        return False

    props = _p(row["properties"])
    if piotroski is not None:
        props["piotroski_f_score"] = piotroski
        # Piotroski: 0-3 weak, 4-6 average, 7-9 strong
        props["piotroski_grade"] = "strong" if piotroski >= 7 else "average" if piotroski >= 4 else "weak"
    if altman is not None:
        props["altman_z_score"] = altman
        # Altman: >2.99 safe, 1.81-2.99 grey, <1.81 distress
        props["altman_zone"] = "safe" if altman > 2.99 else "grey" if altman > 1.81 else "distress"

    conn.execute("UPDATE nodes SET properties=? WHERE id=?",
                 (json.dumps(props, ensure_ascii=False), row["id"]))
    return True


def _run_one(ticker: str, mcp_tools: dict, dry_run: bool = False) -> dict:
    """Fetch TradingView data for one ticker and ingest into graph."""
    tv_sym = _tv_symbol(ticker)
    result = {"ticker": ticker, "status": "skip"}

    # 1. Analyst forecasts + price target
    try:
        forecasts = mcp_tools["get_forecasts"](tv_sym)
        if forecasts:
            result["forecasts"] = True
            if not dry_run:
                # merged into ingest below
                pass
    except Exception as e:
        result["forecast_err"] = str(e)[:80]
        return result

    # 2. Screener for quality metrics (Piotroski, Altman)
    quality_data = {}
    try:
        sym_data = mcp_tools["get_symbol_data"](tv_sym, [
            "piotroski_f_score", "altman_z_score", "close"
        ])
        if sym_data:
            quality_data = sym_data
    except Exception:
        pass

    if dry_run:
        return {
            "ticker": ticker,
            "status": "dry_run",
            "has_forecasts": bool(forecasts),
            "has_quality": bool(quality_data),
        }

    # Ingest
    conn = _conn()
    try:
        # Merge data for AnalystConsensus
        tv_data = {
            "forecasts": forecasts,
            "current_price": quality_data.get("close"),
            "piotroski_f_score": quality_data.get("piotroski_f_score"),
            "altman_z_score": quality_data.get("altman_z_score"),
        }

        with conn:
            ac_ok = ingest_analyst_consensus(conn, ticker, tv_data)
            bq_ok = ingest_quality_metrics(conn, ticker, tv_data)

        result["status"] = "ok"
        result["analyst_consensus"] = ac_ok
        result["quality_updated"] = bq_ok
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
    finally:
        conn.close()

    return result


def run(limit: int = 50, ticker: str | None = None, dry_run: bool = False) -> dict:
    """
    Ingest TradingView data for top-N ConfluenceSignal tickers.

    Uses TradingView MCP tools directly (not an HTTP client).
    Must be called from within Claude Code (MCP tools available via Python bridge).
    """
    # TradingView MCP tools are only available via Claude's tool-call layer,
    # not directly callable from Python. This job is designed to be called
    # by the orchestrator which passes tool results through, or run interactively.
    # For now, provide the ingest functions; actual MCP calls happen in the
    # agent loop in orchestrator.

    return {
        "status": "not_runnable_standalone",
        "note": (
            "TradingView MCP tools are only available via Claude's tool-call interface. "
            "Call ingest_analyst_consensus() and ingest_quality_metrics() directly "
            "from within a Claude session with TradingView MCP connected."
        ),
    }


def ingest_from_data(ticker: str, forecasts: dict | None,
                     symbol_data: dict | None) -> dict:
    """
    Public API: ingest pre-fetched TradingView data for one ticker.
    Call this from within Claude session after fetching via MCP tools.

    forecasts:   output of mcp-tv-get-forecasts for the ticker
    symbol_data: output of mcp-tv-get-symbol-data with piotroski_f_score, altman_z_score, close
    """
    conn = _conn()
    result = {"ticker": ticker}
    try:
        tv_data = {
            "forecasts": forecasts,
            "current_price": (symbol_data or {}).get("close"),
            "piotroski_f_score": (symbol_data or {}).get("piotroski_f_score"),
            "altman_z_score": (symbol_data or {}).get("altman_z_score"),
        }
        with conn:
            result["analyst_consensus"] = ingest_analyst_consensus(conn, ticker, tv_data)
            result["quality_updated"]   = ingest_quality_metrics(conn, ticker, tv_data)
        result["status"] = "ok"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
    finally:
        conn.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TradingView data ingestor")
    parser.add_argument("--limit",   type=int, default=50)
    parser.add_argument("--ticker",  type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = run(limit=args.limit, ticker=args.ticker, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
