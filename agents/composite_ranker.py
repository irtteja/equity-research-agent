"""
composite_ranker.py — Composite screener ranker combining all analysis nodes.

Combines 4 signals into a single composite_score per ticker:
  - Confluence confidence  (40%): ConfluenceSignal.score
  - Business quality       (25%): BusinessQuality.quality_score
  - Peer standing          (20%): PeerComparison.peer_verdict → 0/0.4/0.5/1.0
  - Technical position     (15%): 1 - TechnicalSignal.pos_52w/100

Writes CompositeRank nodes to memory_store/graph.db.
Saves sorted watchlist to memory_store/ranked_watchlist.json.

No external API calls. Pure graph queries + math.

CLI:
  python agents/composite_ranker.py              # rank all 159 tickers
  python agents/composite_ranker.py --limit 50   # top 50 by confluence
  python agents/composite_ranker.py --ticker RELIANCE
"""

import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

DB_PATH       = REPO / "memory_store" / "graph.db"
WATCHLIST_OUT = REPO / "memory_store" / "ranked_watchlist.json"

# ── Scoring weights ───────────────────────────────────────────────────────────

W_CONFLUENCE = 0.40
W_QUALITY    = 0.25
W_PEER       = 0.20
W_TECHNICAL  = 0.15

PEER_VERDICT_SCORES = {
    "top_quartile":       1.0,
    "mid_quartile":       0.5,
    "bottom_quartile":    0.0,
    "insufficient_data":  0.4,
}

DEFAULT_QUALITY  = 0.4
DEFAULT_PEER     = 0.4
DEFAULT_TECH     = 0.5


# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _p(s) -> dict:
    """Safely parse JSON properties string."""
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


# ── Node loaders ──────────────────────────────────────────────────────────────

def _load_all_confluence_tickers(conn, limit: int) -> list[dict]:
    """Return all ConfluenceSignal tickers ordered by score desc."""
    rows = conn.execute(
        """SELECT ticker, properties FROM nodes
           WHERE node_type='ConfluenceSignal'
           ORDER BY CAST(json_extract(properties,'$.score') AS REAL) DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        p = _p(r["properties"])
        out.append({
            "ticker": r["ticker"],
            "conf":   float(p.get("score", 0.0)),
        })
    return out


def _load_business_quality(conn, ticker: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT properties FROM nodes WHERE ticker=? AND node_type='BusinessQuality' LIMIT 1",
        (ticker,),
    ).fetchone()
    return _p(row["properties"]) if row else None


def _load_peer_comparison(conn, ticker: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT properties FROM nodes WHERE ticker=? AND node_type='PeerComparison' LIMIT 1",
        (ticker,),
    ).fetchone()
    return _p(row["properties"]) if row else None


def _load_technical(conn, ticker: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT properties FROM nodes WHERE ticker=? AND node_type='TechnicalSignal' LIMIT 1",
        (ticker,),
    ).fetchone()
    return _p(row["properties"]) if row else None


# ── Score computation ─────────────────────────────────────────────────────────

def _compute_score(ticker: str, conf: float, conn) -> dict:
    """
    Compute composite_score for one ticker.

    Returns a dict with:
      composite_score, quality_tier,
      conf, quality_score, peer_verdict, pos_52w,
      quality_component, peer_component, tech_component
    """
    # 1. Confluence confidence (40%) — already provided
    conf_score = max(0.0, min(1.0, conf))

    # 2. Business quality (25%)
    bq   = _load_business_quality(conn, ticker)
    if bq and "quality_score" in bq:
        q_score = float(bq["quality_score"])
    else:
        q_score = DEFAULT_QUALITY
    q_score = max(0.0, min(1.0, q_score))

    # 3. Peer standing (20%)
    pc = _load_peer_comparison(conn, ticker)
    if pc:
        verdict   = pc.get("peer_verdict", "insufficient_data")
        peer_score = PEER_VERDICT_SCORES.get(verdict, DEFAULT_PEER)
    else:
        verdict   = "absent"
        peer_score = DEFAULT_PEER

    # 4. Technical position (15%) — lower pos_52w = closer to low = better entry
    ts = _load_technical(conn, ticker)
    if ts and "pos_52w" in ts and ts["pos_52w"] is not None:
        pos = float(ts["pos_52w"])
        tech_score = 1.0 - (pos / 100.0)
    else:
        pos        = None
        tech_score = DEFAULT_TECH
    tech_score = max(0.0, min(1.0, tech_score))

    # Weighted composite
    composite = (
        W_CONFLUENCE * conf_score +
        W_QUALITY    * q_score +
        W_PEER       * peer_score +
        W_TECHNICAL  * tech_score
    )
    composite = round(max(0.0, min(1.0, composite)), 4)

    # Quality tier
    if composite >= 0.70:
        tier = "A"
    elif composite >= 0.55:
        tier = "B"
    elif composite >= 0.40:
        tier = "C"
    else:
        tier = "D"

    return {
        "ticker":               ticker,
        "composite_score":      composite,
        "quality_tier":         tier,
        "conf":                 round(conf_score, 4),
        "quality_score":        round(q_score, 4),
        "peer_verdict":         verdict,
        "pos_52w":              pos,
        # Component breakdown (for debugging)
        "conf_component":       round(W_CONFLUENCE * conf_score, 4),
        "quality_component":    round(W_QUALITY    * q_score,    4),
        "peer_component":       round(W_PEER       * peer_score, 4),
        "tech_component":       round(W_TECHNICAL  * tech_score, 4),
    }


# ── Graph writer ──────────────────────────────────────────────────────────────

def _write_composite_rank(conn, result: dict, rank: int):
    """Upsert a CompositeRank node for ticker."""
    import hashlib
    node_id = hashlib.md5(f"CompositeRank:{result['ticker']}".encode()).hexdigest()[:16]
    props = {**result, "rank": rank, "computed_at": datetime.now().isoformat()}

    existing = conn.execute("SELECT id FROM nodes WHERE id=?", (node_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE nodes SET properties=? WHERE id=?",
            (json.dumps(props, default=str), node_id),
        )
    else:
        conn.execute(
            """INSERT INTO nodes (id, node_type, name, ticker, properties, created_at)
               VALUES (?, 'CompositeRank', ?, ?, ?, ?)""",
            (node_id,
             f"CompositeRank:{result['ticker']}",
             result["ticker"],
             json.dumps(props, default=str),
             datetime.now().isoformat()),
        )


# ── Main class ────────────────────────────────────────────────────────────────

class CompositeRanker:
    """
    Ranks all ConfluenceSignal tickers by composite_score and writes
    CompositeRank nodes + ranked_watchlist.json.
    """

    def rank_ticker(self, ticker: str) -> dict:
        """
        Score and return composite rank for a single ticker.
        Does not assign rank (use run_all for ranked output).
        """
        conn = _conn()
        ticker = ticker.upper()
        # Pull confluence score for this ticker
        row = conn.execute(
            "SELECT properties FROM nodes WHERE ticker=? AND node_type='ConfluenceSignal' LIMIT 1",
            (ticker,),
        ).fetchone()
        if not row:
            return {"error": f"No ConfluenceSignal found for {ticker}"}
        p    = _p(row["properties"])
        conf = float(p.get("score", 0.0))
        result = _compute_score(ticker, conf, conn)
        result["rank"] = None
        return result

    def run_all(self, limit: int = 200) -> dict:
        """
        Score all ConfluenceSignal tickers, rank them, write nodes + JSON.

        Returns:
          {ok, errors, tickers, duration_s}
        """
        t0     = time.time()
        conn   = _conn()
        tickers_data = _load_all_confluence_tickers(conn, limit)

        if not tickers_data:
            return {"ok": 0, "errors": 0, "tickers": [], "duration_s": 0.0,
                    "error": "No ConfluenceSignal nodes found in graph.db"}

        # Score all tickers
        scored  = []
        errors  = 0
        for td in tickers_data:
            try:
                result = _compute_score(td["ticker"], td["conf"], conn)
                scored.append(result)
            except Exception as e:
                errors += 1
                print(f"[ranker] ERROR scoring {td['ticker']}: {e}", file=sys.stderr)

        # Sort by composite_score desc, assign ranks
        scored.sort(key=lambda x: x["composite_score"], reverse=True)
        for i, r in enumerate(scored):
            r["rank"] = i + 1

        # Write CompositeRank nodes to graph.db
        try:
            with conn:
                for r in scored:
                    _write_composite_rank(conn, r, r["rank"])
        except Exception as e:
            print(f"[ranker] ERROR writing nodes: {e}", file=sys.stderr)
            errors += 1

        # Save ranked watchlist JSON
        watchlist = []
        for r in scored:
            watchlist.append({
                "ticker":          r["ticker"],
                "composite_score": r["composite_score"],
                "quality_tier":    r["quality_tier"],
                "rank":            r["rank"],
                "conf":            r["conf"],
                "quality_score":   r["quality_score"],
                "peer_verdict":    r["peer_verdict"],
                "pos_52w":         r["pos_52w"],
            })

        try:
            WATCHLIST_OUT.write_text(json.dumps(watchlist, indent=2, default=str))
        except Exception as e:
            print(f"[ranker] ERROR writing watchlist JSON: {e}", file=sys.stderr)
            errors += 1

        dur = round(time.time() - t0, 2)
        tickers_out = [r["ticker"] for r in scored]

        return {
            "ok":         len(scored),
            "errors":     errors,
            "tickers":    tickers_out,
            "duration_s": dur,
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Composite screener ranker")
    parser.add_argument("--limit",  type=int, default=200,
                        help="Max number of tickers to rank (default 200)")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Score a single ticker and print result")
    args = parser.parse_args()

    ranker = CompositeRanker()

    if args.ticker:
        result = ranker.rank_ticker(args.ticker)
        print(json.dumps(result, indent=2))
    else:
        print(f"[ranker] Scoring all ConfluenceSignal tickers (limit={args.limit})...")
        out = ranker.run_all(limit=args.limit)
        print(f"[ranker] Done: {out['ok']} ranked, {out['errors']} errors, "
              f"took {out['duration_s']}s")

        # Print top 10
        watchlist_path = REPO / "memory_store" / "ranked_watchlist.json"
        if watchlist_path.exists():
            watchlist = json.loads(watchlist_path.read_text())
            print("\nTop 10 tickers by composite score:")
            print(f"{'Rank':<5} {'Ticker':<15} {'Score':<8} {'Tier':<5} "
                  f"{'Conf':<8} {'Quality':<8} {'Peer':<20} {'52W pos':<8}")
            print("-" * 80)
            for r in watchlist[:10]:
                pos_str = f"{r['pos_52w']:.1f}%" if r["pos_52w"] is not None else "n/a"
                print(f"{r['rank']:<5} {r['ticker']:<15} {r['composite_score']:<8.4f} "
                      f"{r['quality_tier']:<5} {r['conf']:<8.4f} "
                      f"{r['quality_score']:<8.4f} {r['peer_verdict']:<20} {pos_str:<8}")
