"""
jobs/bulk_report_index.py
--------------------------
Generate/regenerate research reports for all BUY-signal tickers and write a
consolidated index so users have one place to discover all reports.

Data sources (in priority order)
---------------------------------
1. memory_store/ranked_watchlist.json  — created by composite_ranker when present
2. ConfluenceSignal nodes in memory_store/graph.db  — always available

Enrichment per ticker
---------------------
- Sector         → memory_store/company_meta.json
- Quality Tier   → BusinessQuality node  (high→A, medium→B, low→C, else→D)
- Composite Score→ ConfluenceSignal.score (0–1)
- Signal         → "BUY" (all rows in this index are BUY signals)
- Confidence     → same as score
- 52W Position   → ConfluenceSignal.near_low_pct (% above 52W low); lower = closer to low

Outputs
-------
reports/index.md   — Markdown table (Rank / Ticker / Sector / Quality Tier /
                     Composite Score / Signal / Confidence / 52W Position / Report Link)
reports/index.json — same data as a JSON array for dashboard consumption

CLI
---
    python jobs/bulk_report_index.py [--limit N] [--tier A]

    --limit N   : process at most N tickers (default 50)
    --tier A    : filter to a single quality tier (A / B / C / D)
    --no-report : build the index from cached data only, skip report generation
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DB_PATH = REPO / "memory_store" / "graph.db"
META_PATH = REPO / "memory_store" / "company_meta.json"
WATCHLIST_PATH = REPO / "memory_store" / "ranked_watchlist.json"
REPORTS_DIR = REPO / "reports"
INDEX_MD = REPORTS_DIR / "index.md"
INDEX_JSON = REPORTS_DIR / "index.json"

# Quality tier mapping (BusinessQuality overall_quality → letter grade)
_TIER_MAP = {
    "high": "A",
    "medium": "B",
    "low": "C",
}
_TIER_FILTER_REVERSE = {"A": "high", "B": "medium", "C": "low", "D": None}


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_company_meta() -> dict:
    if META_PATH.exists():
        with open(META_PATH) as f:
            return json.load(f)
    return {}


def _get_conn():
    return sqlite3.connect(str(DB_PATH), timeout=30)


def _load_watchlist(limit: int, tier_filter: str | None) -> list[dict]:
    """
    Load tickers from ranked_watchlist.json if present, else fall back to
    ConfluenceSignal nodes from graph.db.

    Returns a list of dicts with keys:
        ticker, score, type, near_low_pct
    sorted by score descending, up to `limit` rows.
    """
    if WATCHLIST_PATH.exists():
        with open(WATCHLIST_PATH) as f:
            data = json.load(f)
        # Expect a list of dicts with at least {ticker, composite_score or score}.
        # ranked_watchlist.json from composite_ranker has no "signal" field — all
        # entries are BUY signals by definition (it only tracks long candidates).
        # If a "signal" field is present, honour it; otherwise treat all as BUY.
        rows = []
        for item in data:
            signal = item.get("signal", item.get("type", "BUY"))
            if signal and "BUY" not in str(signal).upper():
                continue  # skip explicit non-BUY entries
            # near_low_pct may be stored as pos_52w (fractional) or near_low_pct
            nlp = item.get("near_low_pct", item.get("pos_52w", None))
            if nlp is not None:
                # pos_52w is a fraction (0.0168 = 1.68 %); near_low_pct may be %
                nlp = float(nlp)
                if nlp < 2:  # likely a fraction, convert to %
                    nlp *= 100
            rows.append({
                "ticker": item.get("ticker", ""),
                "score": float(item.get("composite_score", item.get("conf", item.get("score", 0.5)))),
                "type": signal or "BUY",
                "near_low_pct": nlp,
                # carry quality_tier if present (saves a DB round-trip)
                "_quality_tier": item.get("quality_tier"),
            })
        rows.sort(key=lambda r: -r["score"])
        return rows[:limit] if tier_filter is None else rows

    # Fallback: graph.db ConfluenceSignal nodes
    conn = _get_conn()
    # When tier_filter is set we need the full universe; otherwise limit to avoid huge scans
    db_limit = 1000 if tier_filter else limit * 4
    rows_db = conn.execute(
        """SELECT ticker,
                  json_extract(properties,'$.score')     AS score,
                  json_extract(properties,'$.type')      AS sig_type,
                  json_extract(properties,'$.near_low_pct') AS near_low
           FROM nodes
           WHERE node_type='ConfluenceSignal'
             AND (json_extract(properties,'$.signal')='BUY'
                  OR json_extract(properties,'$.type') LIKE 'BUY%')
           ORDER BY score DESC
           LIMIT ?""",
        (db_limit,),
    ).fetchall()
    conn.close()

    rows = []
    for ticker, score, sig_type, near_low in rows_db:
        rows.append({
            "ticker": ticker,
            "score": float(score) if score is not None else 0.5,
            "type": sig_type or "BUY_CONFLUENCE",
            "near_low_pct": float(near_low) if near_low is not None else None,
            "_quality_tier": None,  # fetched later via _load_quality_tiers
        })
    return rows  # caller applies limit after optional tier filter


def _load_quality_tiers(tickers: list[str]) -> dict[str, str]:
    """
    Return {ticker: tier_letter} for each ticker that has a BusinessQuality node.
    Missing → "D".
    """
    conn = _get_conn()
    placeholders = ",".join("?" * len(tickers))
    rows = conn.execute(
        f"""SELECT ticker, json_extract(properties,'$.overall_quality')
            FROM nodes
            WHERE node_type='BusinessQuality' AND ticker IN ({placeholders})""",
        tickers,
    ).fetchall()
    conn.close()
    result = {}
    for ticker, quality in rows:
        result[ticker] = _TIER_MAP.get(quality, "D")
    return result


def _52w_label(near_low_pct) -> str:
    """Convert near_low_pct (% above 52W low) to a readable label."""
    if near_low_pct is None:
        return "—"
    v = float(near_low_pct)
    if v <= 5:
        return f"{v:.1f}% (near low)"
    if v <= 20:
        return f"{v:.1f}%"
    if v <= 50:
        return f"{v:.1f}% (mid-range)"
    return f"{v:.1f}% (near high)"


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _generate_reports(tickers: list[str]) -> dict[str, bool]:
    """Generate/regenerate reports. Returns {ticker: success}."""
    from jobs.generate_report import save_report  # lazy import
    results = {}
    for ticker in tickers:
        try:
            save_report(ticker)
            results[ticker] = True
        except Exception as exc:
            print(f"  [report] SKIP {ticker}: {exc}")
            results[ticker] = False
    return results


# ---------------------------------------------------------------------------
# Index writers
# ---------------------------------------------------------------------------

def _write_index_md(rows: list[dict]) -> None:
    today = date.today().isoformat()
    lines = [
        f"# Equity Research — BUY Signal Index",
        f"",
        f"Generated: {today}  |  Total tickers: {len(rows)}",
        f"",
        f"| Rank | Ticker | Sector | Quality Tier | Composite Score | Signal | Confidence | 52W Position | Report |",
        f"|-----:|--------|--------|:------------:|----------------:|--------|:----------:|:-------------|--------|",
    ]
    for row in rows:
        link = f"[{row['ticker']}](tickers/{row['ticker']}/latest.md)"
        lines.append(
            f"| {row['rank']} "
            f"| {row['ticker']} "
            f"| {row['sector']} "
            f"| {row['quality_tier']} "
            f"| {row['composite_score']:.3f} "
            f"| {row['signal']} "
            f"| {row['confidence']:.3f} "
            f"| {row['w52_position']} "
            f"| {link} |"
        )
    INDEX_MD.write_text("\n".join(lines) + "\n")
    print(f"[bulk_report_index] Wrote {INDEX_MD}")


def _write_index_json(rows: list[dict]) -> None:
    INDEX_JSON.write_text(json.dumps(rows, indent=2))
    print(f"[bulk_report_index] Wrote {INDEX_JSON}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate bulk report index for BUY-signal tickers."
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Max tickers to include (default 50)"
    )
    parser.add_argument(
        "--tier", type=str, default=None,
        choices=["A", "B", "C", "D"],
        help="Filter to a specific quality tier (A=high, B=medium, C=low, D=unscored)"
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="Skip report generation; rebuild index from cached data only"
    )
    args = parser.parse_args()

    t0 = time.time()

    # Task logging
    try:
        from memory.task_log import log_task, log_checkpoint, update_task_status
        task_id = "bulk_report_index_001"
        log_task(task_id, f"Bulk report index (limit={args.limit}, tier={args.tier})")
    except Exception:
        task_id = None

    # 1. Load BUY tickers
    print(f"[bulk_report_index] Loading BUY-signal tickers (limit={args.limit}) …")
    all_rows = _load_watchlist(args.limit * 2, args.tier)  # over-fetch for tier filter
    print(f"  fetched {len(all_rows)} candidates")

    # 2. Load enrichment data
    tickers_all = [r["ticker"] for r in all_rows]
    company_meta = _load_company_meta()
    quality_tiers = _load_quality_tiers(tickers_all)

    # 3. Tier filter
    if args.tier:
        all_rows = [r for r in all_rows if quality_tiers.get(r["ticker"], "D") == args.tier]
        print(f"  after tier={args.tier} filter: {len(all_rows)} tickers")

    # 4. Apply final limit
    all_rows = all_rows[: args.limit]
    tickers = [r["ticker"] for r in all_rows]
    print(f"  final list: {len(tickers)} tickers")

    # 5. Generate/regenerate reports
    if not args.no_report:
        print(f"[bulk_report_index] Generating reports for {len(tickers)} tickers …")
        report_results = _generate_reports(tickers)
        ok = sum(1 for v in report_results.values() if v)
        print(f"  reports generated: {ok}/{len(tickers)}")
    else:
        print("[bulk_report_index] --no-report set; skipping report generation")

    # 6. Build index rows
    index_rows = []
    for rank, row in enumerate(all_rows, 1):
        ticker = row["ticker"]
        meta = company_meta.get(ticker, {})
        sector = meta.get("sector") or meta.get("macro") or "—"
        # Use pre-loaded tier from watchlist if present, else graph lookup
        qtier = row.get("_quality_tier") or quality_tiers.get(ticker, "D")
        score = row["score"]
        index_rows.append({
            "rank": rank,
            "ticker": ticker,
            "sector": sector,
            "quality_tier": qtier,
            "composite_score": score,
            "signal": "BUY",
            "confidence": score,
            "w52_position": _52w_label(row.get("near_low_pct")),
            "report_link": f"reports/tickers/{ticker}/latest.md",
        })

    # 7. Write outputs
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    _write_index_md(index_rows)
    _write_index_json(index_rows)

    duration = round(time.time() - t0, 2)
    print(f"[bulk_report_index] Done in {duration}s — {len(index_rows)} tickers indexed")

    if task_id:
        try:
            log_checkpoint(
                task_id,
                status="completed",
                progress=f"Indexed {len(index_rows)} tickers in {duration}s",
                next_steps="None",
                blockers="",
            )
            update_task_status(task_id, "completed")
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
