"""
valuation_engine.py — compute P/E, growth, margins from results JSONL files.

Reads quarterly XBRL data from memory_store/results/<TICKER>.jsonl.
Gets current price from PriceSnapshot node or bhavcopy.
Writes ValuationScore nodes to graph.db.

Called by: agents/valuation_agent.py
"""
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

REPO     = Path(__file__).parent.parent
DB_PATH  = REPO / "memory_store" / "graph.db"
RES_DIR  = REPO / "memory_store" / "results"

MIN_QUARTERS = 2


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _load_quarters(ticker: str) -> list[dict]:
    """Load quarterly results sorted oldest→newest."""
    path = RES_DIR / f"{ticker}.jsonl"
    if not path.exists():
        return []
    quarters = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                quarters.append(json.loads(line))
            except Exception:
                continue
    # sort by period_end ascending
    quarters.sort(key=lambda q: q.get("period_end", ""))
    return quarters


def _get_current_price(ticker: str) -> float | None:
    """Get latest close price from PriceSnapshot node or bhavcopy."""
    with _conn() as c:
        row = c.execute(
            "SELECT properties FROM nodes WHERE node_type='PriceSnapshot' AND ticker=? ORDER BY created_at DESC LIMIT 1",
            (ticker,)
        ).fetchone()
        if row:
            props = json.loads(row[0]) if row[0] else {}
            return props.get("close") or props.get("ltp") or props.get("last_price")
    return None


def _safe(val) -> float | None:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _pct_change(new_val, old_val) -> float | None:
    if new_val is None or old_val is None or old_val == 0:
        return None
    return round((new_val - old_val) / abs(old_val) * 100, 2)


def compute_valuation(ticker: str) -> dict | None:
    """
    Compute valuation metrics for ticker.
    Returns dict or None if insufficient data.
    """
    quarters = _load_quarters(ticker)
    if len(quarters) < MIN_QUARTERS:
        return None

    latest = quarters[-1]
    price  = _get_current_price(ticker)

    # TTM metrics: sum last 4 quarters (or available)
    window = quarters[-4:]
    ttm_revenue = sum(_safe(q.get("revenue_from_operations")) or 0 for q in window) or None
    ttm_pat     = sum(_safe(q.get("pat")) or 0 for q in window) or None
    ttm_ebit    = sum(_safe(q.get("ebit")) or 0 for q in window) or None
    ttm_eps     = sum(_safe(q.get("eps_basic")) or 0 for q in window) or None
    ttm_depr    = sum(_safe(q.get("depreciation")) or 0 for q in window) or None

    # P/E
    pe = None
    if price and ttm_eps and ttm_eps > 0:
        pe = round(price / ttm_eps, 2)

    # Earnings growth YoY (compare same quarter last year)
    pat_growth = None
    rev_growth = None
    if len(quarters) >= 5:
        q_now  = quarters[-1]
        q_1y   = quarters[-5]
        pat_growth = _pct_change(_safe(q_now.get("pat")), _safe(q_1y.get("pat")))
        rev_growth = _pct_change(
            _safe(q_now.get("revenue_from_operations")),
            _safe(q_1y.get("revenue_from_operations"))
        )
    elif len(quarters) >= 2:
        # QoQ as fallback
        q_now = quarters[-1]
        q_prev = quarters[-2]
        pat_growth = _pct_change(_safe(q_now.get("pat")), _safe(q_prev.get("pat")))
        rev_growth = _pct_change(
            _safe(q_now.get("revenue_from_operations")),
            _safe(q_prev.get("revenue_from_operations"))
        )

    # EBIT margin
    ebit_margin = None
    q_rev = _safe(latest.get("revenue_from_operations"))
    q_ebit = _safe(latest.get("ebit"))
    if q_rev and q_ebit and q_rev > 0:
        ebit_margin = round(q_ebit / q_rev * 100, 2)

    # FCF proxy: PAT + Depreciation (approximation; capex usually None in XBRL)
    fcf_proxy = None
    if ttm_pat is not None and ttm_depr is not None:
        fcf_proxy = round(ttm_pat + ttm_depr, 2)

    # Debt stress: finance_costs / ebit (interest coverage proxy)
    int_coverage = None
    q_fin_costs = _safe(latest.get("finance_costs"))
    if q_ebit and q_fin_costs and q_fin_costs > 0:
        int_coverage = round(q_ebit / q_fin_costs, 2)

    # Balance sheet — debt/equity (often None in standalone XBRL)
    bs = latest.get("balance_sheet", {}) or {}
    borrowings = (_safe(bs.get("borrowings_current")) or 0) + (_safe(bs.get("borrowings_noncurrent")) or 0)
    equity = _safe(bs.get("equity"))
    debt_equity = round(borrowings / equity, 2) if equity and equity > 0 and borrowings else None

    # Valuation score (0–1, higher = more attractive)
    score_parts = []
    # Growth component (0–0.4): positive growth good
    if pat_growth is not None:
        g_score = min(max(pat_growth / 50, 0), 1) * 0.4
        score_parts.append(g_score)
    # PE component (0–0.3): lower PE relative to 25 benchmark = better
    if pe is not None and pe > 0:
        pe_score = max(0, 1 - pe / 50) * 0.3
        score_parts.append(pe_score)
    # Margin component (0–0.3): higher EBIT margin better
    if ebit_margin is not None:
        m_score = min(ebit_margin / 30, 1) * 0.3
        score_parts.append(m_score)

    valuation_score = round(sum(score_parts), 3) if score_parts else None

    # Verdict
    verdict = "insufficient_data"
    if pe is not None and pat_growth is not None:
        if pe < 15 and pat_growth > 10:
            verdict = "cheap"
        elif pe > 40 or pat_growth < -10:
            verdict = "expensive"
        else:
            verdict = "fair"

    return {
        "ticker":          ticker,
        "price":           price,
        "pe":              pe,
        "ttm_eps":         round(ttm_eps, 2) if ttm_eps else None,
        "ttm_revenue_cr":  round(ttm_revenue / 100, 2) if ttm_revenue else None,
        "ttm_pat_cr":      round(ttm_pat / 100, 2) if ttm_pat else None,
        "pat_growth_pct":  pat_growth,
        "rev_growth_pct":  rev_growth,
        "ebit_margin_pct": ebit_margin,
        "fcf_proxy_cr":    round(fcf_proxy / 100, 2) if fcf_proxy else None,
        "int_coverage":    int_coverage,
        "debt_equity":     debt_equity,
        "valuation_score": valuation_score,
        "verdict":         verdict,
        "quarters_used":   len(quarters),
        "latest_period":   latest.get("period_end"),
        "computed_at":     datetime.now().isoformat(),
    }


def write_valuation_score(ticker: str, metrics: dict) -> bool:
    """Upsert ValuationScore node for ticker."""
    try:
        from memory.graph_store import get_store
        gs = get_store()
        gs.upsert_node(
            node_type="ValuationScore",
            name=f"ValuationScore_{ticker}_{date.today()}",
            ticker=ticker,
            properties=metrics,
        )
        return True
    except Exception:
        return False


def run_ticker(ticker: str) -> dict:
    """Compute and store valuation score for one ticker. Returns status dict."""
    try:
        metrics = compute_valuation(ticker)
        if metrics is None:
            return {"ticker": ticker, "status": "skipped", "reason": "insufficient_data"}
        write_valuation_score(ticker, metrics)
        return {
            "ticker":  ticker,
            "status":  "ok",
            "verdict": metrics["verdict"],
            "pe":      metrics["pe"],
            "pat_growth": metrics["pat_growth_pct"],
        }
    except Exception as e:
        return {"ticker": ticker, "status": "error", "error": str(e)}
