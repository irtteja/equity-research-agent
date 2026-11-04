"""
Peer Comparison Engine — relative ranking of a ticker vs sector/industry peers.

Pulls from graph.db:
  - ValuationScore  → P/E, revenue growth, PAT growth, EBIT margin
  - ConfluenceSignal → confidence score
  - company_meta.json → sector / industry classification

Writes PeerComparison nodes to graph.db.

No external API calls. Pure graph queries + math.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO = Path(__file__).parent.parent
DB = REPO / "memory_store" / "graph.db"
META_FILE = REPO / "memory_store" / "company_meta.json"


# ── Data loaders ──────────────────────────────────────────────────────────────

def _load_meta() -> dict:
    """Load company_meta.json once; returns {ticker: {...}} or {}."""
    try:
        with open(META_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _get_valuation(conn: sqlite3.Connection, ticker: str) -> Optional[dict]:
    """Return ValuationScore properties for ticker, or None."""
    row = conn.execute(
        "SELECT properties FROM nodes WHERE ticker=? AND node_type='ValuationScore' LIMIT 1",
        (ticker,),
    ).fetchone()
    if not row:
        return None
    return json.loads(row[0])


def _get_confluence(conn: sqlite3.Connection, ticker: str) -> Optional[float]:
    """Return ConfluenceSignal score for ticker, or None."""
    row = conn.execute(
        "SELECT json_extract(properties,'$.score') FROM nodes "
        "WHERE ticker=? AND node_type='ConfluenceSignal' LIMIT 1",
        (ticker,),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def _rank(value: float, all_values: list[float], higher_is_better: bool) -> tuple[int, int]:
    """
    Return (rank, total) where rank=1 is best.
    Skips None values in all_values.
    """
    valid = [v for v in all_values if v is not None and not (isinstance(v, float) and v != v)]
    if value is None or len(valid) == 0:
        return (None, len(valid))
    sorted_vals = sorted(valid, reverse=higher_is_better)
    try:
        rank = sorted_vals.index(value) + 1
    except ValueError:
        # floating point; find nearest
        diffs = [abs(v - value) for v in sorted_vals]
        rank = diffs.index(min(diffs)) + 1
    return (rank, len(valid))


def _quartile_verdict(rank: int, total: int) -> str:
    if rank is None or total == 0:
        return "insufficient_data"
    pct = rank / total
    if pct <= 0.25:
        return "top_quartile"
    elif pct <= 0.75:
        return "mid_quartile"
    else:
        return "bottom_quartile"


# ── Core API ──────────────────────────────────────────────────────────────────

def get_sector_peers(ticker: str, max_peers: int = 10) -> list[str]:
    """
    Return tickers in same industry (or sector if industry match is thin) from
    company_meta.json.  Excludes the query ticker itself.  Max max_peers returned.
    """
    meta = _load_meta()
    if ticker not in meta:
        return []

    info = meta[ticker]
    target_industry = info.get("industry", "")
    target_sector = info.get("sector", "")

    # Collect industry peers first (more precise)
    industry_peers = []
    sector_peers = []
    for sym, m in meta.items():
        if sym == ticker:
            continue
        if target_industry and m.get("industry") == target_industry:
            industry_peers.append(sym)
        elif target_sector and m.get("sector") == target_sector:
            sector_peers.append(sym)

    peers = industry_peers if len(industry_peers) >= 3 else industry_peers + sector_peers
    return peers[:max_peers]


def compute_peer_metrics(ticker: str, peers: list[str]) -> dict:
    """
    For the ticker and each peer, pull ValuationScore + ConfluenceSignal from graph.db.

    Returns:
      {
        ticker, peers: [{ticker, pe, rev_growth, pat_growth, margin, confidence}],
        peer_rank: {pe_rank, growth_rank, pat_growth_rank, margin_rank},
        vs_peer_avg: {pe_premium_pct, growth_vs_avg, pat_growth_vs_avg, margin_vs_avg},
        peer_verdict: "top_quartile|mid_quartile|bottom_quartile|insufficient_data"
      }
    """
    all_tickers = [ticker] + peers
    conn = sqlite3.connect(str(DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")

    rows = []
    for sym in all_tickers:
        v = _get_valuation(conn, sym)
        conf = _get_confluence(conn, sym)
        if v is None:
            rows.append({"ticker": sym, "pe": None, "rev_growth": None,
                         "pat_growth": None, "margin": None, "confidence": conf})
            continue
        rows.append({
            "ticker": sym,
            "pe": v.get("pe"),
            "rev_growth": v.get("rev_growth_pct"),
            "pat_growth": v.get("pat_growth_pct"),
            "margin": v.get("ebit_margin_pct"),
            "confidence": conf,
        })

    conn.close()

    # Subject row
    subj = next((r for r in rows if r["ticker"] == ticker), None)
    peer_rows = [r for r in rows if r["ticker"] != ticker]

    if subj is None:
        return {"ticker": ticker, "error": "no_valuation_data",
                "peers": peer_rows, "peer_rank": {}, "vs_peer_avg": {},
                "peer_verdict": "insufficient_data"}

    # Build value lists (all tickers including subject for ranking)
    all_pe = [r["pe"] for r in rows]
    all_rev = [r["rev_growth"] for r in rows]
    all_pat = [r["pat_growth"] for r in rows]
    all_mar = [r["margin"] for r in rows]

    # Rank (PE: lower is better; growth+margin: higher is better)
    pe_rank, pe_total = _rank(subj["pe"], all_pe, higher_is_better=False)
    rev_rank, rev_total = _rank(subj["rev_growth"], all_rev, higher_is_better=True)
    pat_rank, pat_total = _rank(subj["pat_growth"], all_pat, higher_is_better=True)
    mar_rank, mar_total = _rank(subj["margin"], all_mar, higher_is_better=True)

    def _fmt_rank(r, t):
        return f"{r}/{t}" if r is not None else f"?/{t}"

    peer_rank = {
        "pe_rank": _fmt_rank(pe_rank, pe_total),
        "growth_rank": _fmt_rank(rev_rank, rev_total),
        "pat_growth_rank": _fmt_rank(pat_rank, pat_total),
        "margin_rank": _fmt_rank(mar_rank, mar_total),
    }

    # Peer averages (exclude subject)
    def _avg(lst):
        vals = [v for v in lst if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    peer_pe_vals = [r["pe"] for r in peer_rows]
    peer_rev_vals = [r["rev_growth"] for r in peer_rows]
    peer_pat_vals = [r["pat_growth"] for r in peer_rows]
    peer_mar_vals = [r["margin"] for r in peer_rows]

    avg_pe = _avg(peer_pe_vals)
    avg_rev = _avg(peer_rev_vals)
    avg_pat = _avg(peer_pat_vals)
    avg_mar = _avg(peer_mar_vals)

    def _pct_vs(subj_val, avg_val):
        if subj_val is None or avg_val is None or avg_val == 0:
            return None
        return round((subj_val - avg_val) / abs(avg_val) * 100, 1)

    vs_peer_avg = {
        "pe_premium_pct": _pct_vs(subj["pe"], avg_pe),
        "growth_vs_avg": None if (subj["rev_growth"] is None or avg_rev is None)
                         else round(subj["rev_growth"] - avg_rev, 1),
        "pat_growth_vs_avg": None if (subj["pat_growth"] is None or avg_pat is None)
                              else round(subj["pat_growth"] - avg_pat, 1),
        "margin_vs_avg": None if (subj["margin"] is None or avg_mar is None)
                          else round(subj["margin"] - avg_mar, 1),
        "peer_avg_pe": avg_pe,
        "peer_avg_rev_growth": avg_rev,
        "peer_avg_pat_growth": avg_pat,
        "peer_avg_margin": avg_mar,
    }

    # Composite rank-based verdict (average percentile across available dimensions)
    rank_pcts = []
    for r, t in [(pe_rank, pe_total), (rev_rank, rev_total),
                 (pat_rank, pat_total), (mar_rank, mar_total)]:
        if r is not None and t > 0:
            rank_pcts.append(r / t)
    avg_pct = sum(rank_pcts) / len(rank_pcts) if rank_pcts else None

    if avg_pct is None:
        peer_verdict = "insufficient_data"
    elif avg_pct <= 0.25:
        peer_verdict = "top_quartile"
    elif avg_pct <= 0.75:
        peer_verdict = "mid_quartile"
    else:
        peer_verdict = "bottom_quartile"

    return {
        "ticker": ticker,
        "peers": peer_rows,
        "peer_rank": peer_rank,
        "vs_peer_avg": vs_peer_avg,
        "peer_verdict": peer_verdict,
        "peer_count": len(peer_rows),
        "computed_at": datetime.now().isoformat(),
    }


def save_peer_comparison(ticker: str) -> dict:
    """
    Compute peer comparison for ticker and write PeerComparison node to graph.db.
    Returns the result dict.
    """
    peers = get_sector_peers(ticker)
    result = compute_peer_metrics(ticker, peers)

    try:
        from memory.graph_store import get_store
        store = get_store()
        store.upsert_node(
            node_type="PeerComparison",
            name=f"peer_comparison:{ticker}",
            ticker=ticker,
            properties=result,
        )
    except Exception as e:
        result["save_error"] = str(e)

    return result


def run_all(limit: int = 100) -> list[dict]:
    """
    Run peer comparison for all ConfluenceSignal tickers (up to limit).
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
            r = save_peer_comparison(t)
            results.append(r)
            ok += 1
        except Exception as e:
            results.append({"ticker": t, "error": str(e)})
            fail += 1

    print(f"[peer_comparison] run_all: {ok} ok, {fail} fail, {len(tickers)} total")
    return results
