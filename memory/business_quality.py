"""
Business Quality Scorer — earnings quality and capital efficiency assessment.

Pulls from graph.db:
  - ValuationScore  → pe, ttm_pat_cr, ttm_revenue_cr, fcf_proxy_cr, ebit_margin_pct,
                      int_coverage, debt_equity, pat_growth_pct, rev_growth_pct
  - TrendSummary    → direction, momentum_score, acceleration, consistent_periods
  - MetricValue     → per-quarter metric values (sparse — mostly 'revenue')

Writes BusinessQuality nodes to graph.db.

No external API calls. Pure graph queries + math.

Quality signals:
  ROCE proxy   — ebit / (debt+equity): use ebit_margin * revenue as ebit numerator,
                 proxy denominator from int_coverage * debt estimate. Falls back to
                 TrendSummary momentum as ROCE trend direction.
  FCF quality  — fcf_proxy / PAT ratio from ValuationScore.
  WC efficiency— revenue trend direction vs consistent_periods from TrendSummary.
  Capex cycle  — debt_equity trend: stable/low = maintenance; rising = investing.
  Margin trend — ebit_margin + TrendSummary acceleration to classify trend.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO = Path(__file__).parent.parent
DB = REPO / "memory_store" / "graph.db"


# ── Data loaders ──────────────────────────────────────────────────────────────

def _get_valuation(conn: sqlite3.Connection, ticker: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT properties FROM nodes WHERE ticker=? AND node_type='ValuationScore' LIMIT 1",
        (ticker,),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _get_trends(conn: sqlite3.Connection, ticker: str) -> list[dict]:
    """Return all TrendSummary nodes for ticker."""
    rows = conn.execute(
        "SELECT properties FROM nodes WHERE ticker=? AND node_type='TrendSummary'",
        (ticker,),
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _get_metric_history(
    conn: sqlite3.Connection, ticker: str, metric: str, limit: int = 8
) -> list[dict]:
    """Return MetricValue nodes for ticker/metric ordered by created_at desc."""
    rows = conn.execute(
        """SELECT properties FROM nodes
           WHERE ticker=? AND node_type='MetricValue'
             AND json_extract(properties,'$.metric')=?
           ORDER BY created_at DESC LIMIT ?""",
        (ticker, metric, limit),
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _score_fcf_quality(val: dict) -> tuple[str, str]:
    """
    FCF/PAT ratio from ValuationScore.
    Returns (label, reason).
    """
    fcf = val.get("fcf_proxy_cr")
    pat = val.get("ttm_pat_cr")
    if fcf is None or pat is None or pat == 0:
        return "insufficient_data", "fcf or pat missing"
    ratio = fcf / pat
    if ratio >= 0.8:
        return "high", f"FCF/PAT={ratio:.2f} ≥0.8"
    elif ratio >= 0.5:
        return "medium", f"FCF/PAT={ratio:.2f} 0.5–0.8"
    else:
        return "low", f"FCF/PAT={ratio:.2f} <0.5"


def _score_roce_trend(val: dict, trends: list[dict]) -> tuple[str, str]:
    """
    ROCE trend proxy.

    Primary: If we have ebit_margin and can proxy assets via (revenue / int_coverage),
             compute a rough ROCE. But since we lack balance sheet data, use TrendSummary
             direction + ebit_margin_pct as qualitative indicator.

    Revenue trend improving + good margin → improving ROCE proxy.
    Revenue trend deteriorating + margin compression → deteriorating.
    """
    ebit_margin = val.get("ebit_margin_pct")
    rev_growth = val.get("rev_growth_pct")
    int_cov = val.get("int_coverage")  # higher = less debt burden

    if ebit_margin is None:
        return "insufficient_data", "ebit_margin missing"

    # Find revenue TrendSummary
    rev_trend = next((t for t in trends if t.get("metric") == "revenue"), None)
    direction = rev_trend.get("direction", "stable") if rev_trend else "unknown"
    accel = rev_trend.get("acceleration", "steady") if rev_trend else "steady"
    momentum = rev_trend.get("momentum_score", 0.5) if rev_trend else 0.5

    # Score: combine ebit_margin level + revenue trend + interest coverage
    # Good ROCE: healthy margin (>10%), improving revenue, manageable debt
    score = 0.0
    if ebit_margin >= 15:
        score += 0.4
    elif ebit_margin >= 8:
        score += 0.25
    elif ebit_margin >= 3:
        score += 0.1

    if direction == "improving":
        score += 0.3
    elif direction == "stable":
        score += 0.15

    if accel == "accelerating":
        score += 0.15
    elif accel == "steady":
        score += 0.1

    if int_cov is not None and int_cov >= 5:
        score += 0.15
    elif int_cov is not None and int_cov >= 2:
        score += 0.08

    if score >= 0.65:
        return "improving", f"margin={ebit_margin:.1f}%, rev={direction}, intcov={int_cov}"
    elif score >= 0.35:
        return "stable", f"margin={ebit_margin:.1f}%, rev={direction}"
    else:
        return "deteriorating", f"margin={ebit_margin:.1f}%, rev={direction}, low coverage"


def _score_working_capital(val: dict, trends: list[dict]) -> tuple[str, str]:
    """
    Working capital efficiency proxy.

    Without receivables data, use:
    - Revenue growth (rev_growth_pct) vs consistent_periods from TrendSummary
    - If revenue growing consistently → efficient WC management assumed
    - Warning flag if revenue declining + pat declining (cash conversion risk)
    """
    rev_growth = val.get("rev_growth_pct")
    pat_growth = val.get("pat_growth_pct")

    rev_trend = next((t for t in trends if t.get("metric") == "revenue"), None)
    if rev_trend is None and rev_growth is None:
        return "insufficient_data", "no revenue data"

    direction = rev_trend.get("direction", "stable") if rev_trend else (
        "improving" if (rev_growth or 0) > 2 else
        "deteriorating" if (rev_growth or 0) < -2 else "stable"
    )
    consistent = rev_trend.get("consistent_periods", 1) if rev_trend else 1

    # Warning: revenue declining while pat declining faster (margin erosion)
    if direction == "deteriorating" and consistent >= 2:
        if pat_growth is not None and pat_growth < (rev_growth or 0):
            return "warning", f"rev {direction} {consistent}q, PAT growth lag"
        return "warning", f"revenue {direction} for {consistent} periods"

    if direction == "improving" and consistent >= 2:
        return "efficient", f"revenue {direction} {consistent}q consistently"

    return "efficient", f"revenue {direction}, {consistent}q"


def _score_capex_cycle(val: dict) -> tuple[str, str]:
    """
    Capital allocation proxy from ValuationScore.

    Uses debt_equity + interest coverage to infer capex cycle.
    - Low debt + high int_coverage → maintenance cycle (self-funded, returning capital)
    - Moderate/rising debt + low int_coverage → investing cycle (funded capex)
    - Very high debt + poor coverage → stressed
    """
    de = val.get("debt_equity")
    int_cov = val.get("int_coverage")
    rev_growth = val.get("rev_growth_pct", 0) or 0

    if de is None and int_cov is None:
        return "insufficient_data", "no debt data"

    if de is None:
        # Only int_coverage available
        if int_cov and int_cov >= 10:
            return "maintenance", f"int_cov={int_cov:.1f}x (low debt)"
        elif int_cov and int_cov >= 3:
            return "investing", f"int_cov={int_cov:.1f}x (moderate)"
        return "insufficient_data", f"int_cov={int_cov}"

    if de <= 0.1:
        label = "maintenance"
        reason = f"D/E={de:.2f} (nearly debt-free)"
    elif de <= 0.5:
        # Growing revenue + moderate debt → investing cycle
        label = "investing" if rev_growth > 5 else "maintenance"
        reason = f"D/E={de:.2f}, rev_growth={rev_growth:.1f}%"
    elif de <= 1.5:
        label = "investing"
        reason = f"D/E={de:.2f} (moderate leverage)"
    else:
        # High debt
        label = "investing" if (int_cov and int_cov >= 2) else "maintenance"
        reason = f"D/E={de:.2f}, int_cov={int_cov}"

    return label, reason


def _score_margin_trend(val: dict, trends: list[dict]) -> tuple[str, str]:
    """
    Operating margin trend from ebit_margin_pct + TrendSummary acceleration.
    """
    margin = val.get("ebit_margin_pct")
    if margin is None:
        return "insufficient_data", "ebit_margin missing"

    rev_trend = next((t for t in trends if t.get("metric") == "revenue"), None)
    direction = rev_trend.get("direction", "stable") if rev_trend else "stable"
    accel = rev_trend.get("acceleration", "steady") if rev_trend else "steady"

    # We use revenue trend + acceleration as a proxy for margin direction
    # (ebit_margin_pct is a snapshot; without historical margin data, revenue accel is best proxy)
    if direction == "improving" and accel in ("accelerating", "steady"):
        return "expanding", f"margin={margin:.1f}%, revenue improving+{accel}"
    elif direction == "deteriorating" and accel in ("accelerating", "steady"):
        return "contracting", f"margin={margin:.1f}%, revenue deteriorating+{accel}"
    elif direction == "stable":
        return "stable", f"margin={margin:.1f}%, revenue stable"
    else:
        return "stable", f"margin={margin:.1f}%, mixed signals"


def _overall_quality(
    roce: str, fcf: str, wc: str, capex: str, margin: str
) -> tuple[str, float]:
    """
    Composite quality: average signal score → high/medium/low + 0–1 score.

    Signal scoring:
      improving/high/efficient/expanding    → 1.0
      stable/medium                         → 0.5
      deteriorating/low/warning/contracting → 0.0
      investing (neutral)                   → 0.5
      insufficient_data                     → 0.5 (neutral, don't penalise)
    """
    score_map = {
        "improving": 1.0, "high": 1.0, "efficient": 1.0, "expanding": 1.0, "maintenance": 0.75,
        "stable": 0.5, "medium": 0.5, "investing": 0.5,
        "deteriorating": 0.0, "low": 0.0, "warning": 0.0, "contracting": 0.0,
        "insufficient_data": 0.5,
    }
    scores = [score_map.get(x, 0.5) for x in [roce, fcf, wc, capex, margin]]
    avg = sum(scores) / len(scores)

    if avg >= 0.70:
        label = "high"
    elif avg >= 0.45:
        label = "medium"
    else:
        label = "low"

    return label, round(avg, 3)


# ── Core API ──────────────────────────────────────────────────────────────────

def compute_business_quality(ticker: str) -> dict:
    """
    Score the quality of a company's earnings from ValuationScore + TrendSummary.

    Returns:
      {ticker, roce_trend, fcf_quality, working_capital, capex_cycle,
       margin_trend, overall_quality, quality_score, reasons, computed_at}
    """
    conn = sqlite3.connect(str(DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")

    val = _get_valuation(conn, ticker)
    trends = _get_trends(conn, ticker)
    conn.close()

    if val is None:
        return {
            "ticker": ticker,
            "roce_trend": "insufficient_data",
            "fcf_quality": "insufficient_data",
            "working_capital": "insufficient_data",
            "capex_cycle": "insufficient_data",
            "margin_trend": "insufficient_data",
            "overall_quality": "low",
            "quality_score": 0.0,
            "reasons": {"error": "no ValuationScore data"},
            "computed_at": datetime.now().isoformat(),
        }

    roce, roce_r = _score_roce_trend(val, trends)
    fcf, fcf_r = _score_fcf_quality(val)
    wc, wc_r = _score_working_capital(val, trends)
    capex, capex_r = _score_capex_cycle(val)
    margin, margin_r = _score_margin_trend(val, trends)

    overall, score = _overall_quality(roce, fcf, wc, capex, margin)

    return {
        "ticker": ticker,
        "roce_trend": roce,
        "fcf_quality": fcf,
        "working_capital": wc,
        "capex_cycle": capex,
        "margin_trend": margin,
        "overall_quality": overall,
        "quality_score": score,
        "reasons": {
            "roce": roce_r,
            "fcf": fcf_r,
            "working_capital": wc_r,
            "capex": capex_r,
            "margin": margin_r,
        },
        "data_snapshot": {
            "pe": val.get("pe"),
            "ebit_margin": val.get("ebit_margin_pct"),
            "fcf_proxy_cr": val.get("fcf_proxy_cr"),
            "ttm_pat_cr": val.get("ttm_pat_cr"),
            "int_coverage": val.get("int_coverage"),
            "debt_equity": val.get("debt_equity"),
            "pat_growth_pct": val.get("pat_growth_pct"),
            "rev_growth_pct": val.get("rev_growth_pct"),
        },
        "computed_at": datetime.now().isoformat(),
    }


def save_business_quality(ticker: str) -> dict:
    """
    Compute business quality and write BusinessQuality node to graph.db.
    Returns result dict.
    """
    result = compute_business_quality(ticker)
    try:
        from memory.graph_store import get_store
        store = get_store()
        store.upsert_node(
            node_type="BusinessQuality",
            name=f"bq:{ticker}",
            ticker=ticker,
            properties=result,
        )
    except Exception as e:
        result["save_error"] = str(e)
    return result


def run_all(limit: int = 159) -> list[dict]:
    """
    Run business quality scoring for all ConfluenceSignal tickers (up to limit).
    Returns list of result dicts.
    """
    conn = sqlite3.connect(str(DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM nodes WHERE node_type='ConfluenceSignal' LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    tickers = [r[0] for r in rows]
    results = []
    ok = fail = 0

    for t in tickers:
        try:
            r = save_business_quality(t)
            results.append(r)
            ok += 1
        except Exception as e:
            results.append({"ticker": t, "error": str(e)})
            fail += 1

    print(f"[business_quality] run_all: {ok} ok, {fail} fail, {len(tickers)} total")
    return results
