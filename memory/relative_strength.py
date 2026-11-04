"""
Relative strength engine — computes Nifty-relative performance for each ticker.

For each ticker in the BUY universe:
  - Loads PriceSnapshot history from graph.db
  - Computes 30d/60d/90d returns vs Nifty50 returns over the same window
  - Writes RelativeStrength node with rs_30/rs_60/rs_90 and label

No external API calls. Reads from graph.db.

CLI:
  python -m memory.relative_strength --ticker RELIANCE
  python -m memory.relative_strength --all
"""
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _get_price_history(ticker: str, days_back: int = 100) -> list[dict]:
    """Return price snapshots sorted oldest-first."""
    cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()
    with _conn() as c:
        rows = c.execute(
            """SELECT created_at, properties FROM nodes
               WHERE ticker=? AND node_type='PriceSnapshot'
               AND created_at >= ?
               ORDER BY created_at ASC""",
            (ticker, cutoff),
        ).fetchall()
    out = []
    for row in rows:
        try:
            p = json.loads(row["properties"] or "{}")
            close = p.get("close") or p.get("ltp") or p.get("lastPrice")
            if close:
                out.append({"ts": row["created_at"], "close": float(close)})
        except Exception:
            pass
    return out


def _pct_change(history: list[dict], days: int) -> float | None:
    """Compute percentage return over `days` back from most recent observation."""
    if not history:
        return None
    cutoff = datetime.now() - timedelta(days=days)
    # find the most recent price
    latest = history[-1]["close"]
    # find price around `days` ago
    past = None
    for h in history:
        try:
            ts = datetime.fromisoformat(h["ts"])
        except Exception:
            continue
        if ts >= cutoff:
            past = h["close"]
            break
    if past is None or past == 0:
        return None
    return (latest - past) / past * 100


def compute_relative_strength(ticker: str) -> dict | None:
    """
    Compute relative strength of `ticker` vs NIFTY 50.
    Returns dict with rs_30, rs_60, rs_90, label, or None if insufficient data.
    """
    ticker_hist = _get_price_history(ticker, days_back=100)
    nifty_hist  = _get_price_history("NIFTY", days_back=100)

    if not ticker_hist:
        return None

    result = {"ticker": ticker}
    for days, key in [(30, "rs_30"), (60, "rs_60"), (90, "rs_90")]:
        t_ret = _pct_change(ticker_hist, days)
        n_ret = _pct_change(nifty_hist, days)
        if t_ret is not None:
            # If no Nifty data, just store raw return
            rs = t_ret - (n_ret or 0.0)
            result[key] = round(rs, 2)
        else:
            result[key] = None

    # Label: strong/neutral/weak based on rs_30
    rs30 = result.get("rs_30")
    if rs30 is None:
        result["label"] = "insufficient_data"
    elif rs30 >= 5:
        result["label"] = "strong_outperformer"
    elif rs30 >= 1:
        result["label"] = "outperformer"
    elif rs30 >= -1:
        result["label"] = "in_line"
    elif rs30 >= -5:
        result["label"] = "underperformer"
    else:
        result["label"] = "strong_underperformer"

    return result


def save_relative_strength(ticker: str) -> dict | None:
    """Compute and save RelativeStrength node for `ticker`."""
    rs = compute_relative_strength(ticker)
    if not rs:
        return None

    from memory.graph_store import get_store
    gs = get_store()

    today = date.today().isoformat()
    gs.upsert_node(
        node_type="RelativeStrength",
        name=f"rs_{ticker}_{today}",
        ticker=ticker,
        properties=rs,
    )
    return rs


def run_all(limit: int = 200) -> list[dict]:
    """Compute relative strength for all BUY signal tickers."""
    with _conn() as c:
        rows = c.execute(
            """SELECT DISTINCT ticker FROM nodes
               WHERE node_type='ConfluenceSignal'
               ORDER BY confidence DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    tickers = [r[0] for r in rows]

    results = []
    for t in tickers:
        try:
            rs = save_relative_strength(t)
            if rs:
                results.append(rs)
        except Exception:
            pass
    return results


def get_top_rs(limit: int = 20, days: int = 30) -> list[dict]:
    """Return top relative strength tickers by rs_30."""
    key = f"rs_{days}"
    with _conn() as c:
        rows = c.execute(
            f"""SELECT ticker, json_extract(properties, '$.{key}') as rs,
                       json_extract(properties, '$.label') as label
                FROM nodes WHERE node_type='RelativeStrength'
                AND json_extract(properties, '$.{key}') IS NOT NULL
                ORDER BY json_extract(properties, '$.{key}') DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    return [{"ticker": r[0], key: r[1], "label": r[2]} for r in rows]


if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))
    p = argparse.ArgumentParser(description="Relative strength vs Nifty")
    p.add_argument("--ticker", help="Single ticker")
    p.add_argument("--all",    action="store_true")
    p.add_argument("--top",    type=int, default=20, metavar="N")
    args = p.parse_args()

    if args.ticker:
        result = compute_relative_strength(args.ticker.upper())
        print(json.dumps(result, indent=2))
    elif args.all:
        results = run_all()
        print(f"Computed relative strength for {len(results)} tickers")
    else:
        top = get_top_rs(limit=args.top)
        print(f"{'Ticker':<12} {'RS-30':>8} {'Label'}")
        for r in top:
            print(f"{r['ticker']:<12} {r.get('rs_30','—'):>8} {r.get('label','')}")
