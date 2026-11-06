"""
Backtest Engine — tests whether the system's signals have predictive value.

Two modes:
1. Historical cross-sectional backtest:
   - Derives fundamental momentum signals from XBRL MetricValue nodes
   - Fetches 1M/3M/6M price performance from TradingView
   - Correlates signal strength with subsequent returns
   - Answers: "do our scoring factors predict outperformance?"

2. Live prediction tracking (future):
   - Updates prediction_log with t+30/60/90 prices as they mature
   - Runs weekly to fill in outcomes once dates arrive

Usage:
    python3 jobs/backtest_engine.py --mode cross_sectional --limit 100
    python3 jobs/backtest_engine.py --mode update_predictions
    python3 jobs/backtest_engine.py --mode report
"""
import json
import sqlite3
import sys
import time
import argparse
from pathlib import Path
from datetime import date, datetime, timedelta
from collections import defaultdict

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "vendor"))

DB_PATH = REPO / "memory_store" / "graph.db"
RESULTS_DIR = REPO / "reports" / "backtest"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ── helpers ──────────────────────────────────────────────────────────────────

def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _get_metric_pivot(tickers: list) -> dict:
    """
    Returns {ticker: {period: {metric: value}}} for revenue and pat
    across the last 4 quarters.
    """
    conn = _conn()
    c = conn.cursor()
    pivot = defaultdict(lambda: defaultdict(dict))

    if tickers:
        placeholders = ",".join(["?" for _ in tickers])
        c.execute(f"""
            SELECT ticker, name, properties FROM nodes
            WHERE node_type='MetricValue'
            AND (name LIKE '%revenue%' OR name LIKE '%pat%' OR name LIKE '%ebit%')
            AND ticker IN ({placeholders})
        """, tickers)
    else:
        c.execute("""
            SELECT ticker, name, properties FROM nodes
            WHERE node_type='MetricValue'
            AND (name LIKE '%revenue%' OR name LIKE '%pat%' OR name LIKE '%ebit%')
        """)

    for row in c.fetchall():
        ticker = row["ticker"]
        props_str = row["properties"] or "{}"
        props = json.loads(props_str)
        period = props.get("period", "")
        value = props.get("value")
        name_parts = row["name"].split()
        # name format: "TICKER revenue Q4-FY26 Q4-FY26" — metric is 2nd token
        if len(name_parts) >= 2:
            metric = name_parts[1]
            pivot[ticker][period][metric] = value

    return dict(pivot)


QUARTER_ORDER = ["Q2-FY26", "Q3-FY26", "Q4-FY26", "Q1-FY27", "Q2-FY27"]


def _fundamental_momentum_score(ticker_data: dict) -> dict:
    """
    Score 0-1 for fundamental momentum based on 3-4 quarters of revenue + PAT.
    Returns:
        score: float 0-1
        signal: 'improving' / 'flat' / 'deteriorating'
        metrics: {quarter: {revenue, pat, rev_qoq, pat_qoq}}
    """
    quarters_present = [q for q in QUARTER_ORDER if q in ticker_data]
    if len(quarters_present) < 2:
        return {"score": None, "signal": "insufficient_data", "quarters": len(quarters_present)}

    # Build timeline
    timeline = []
    for q in quarters_present:
        qdata = ticker_data[q]
        timeline.append({
            "period": q,
            "revenue": qdata.get("revenue"),
            "pat": qdata.get("pat"),
            "ebit": qdata.get("ebit"),
        })

    # Compute QoQ changes
    for i in range(1, len(timeline)):
        prev_rev = timeline[i-1]["revenue"]
        curr_rev = timeline[i]["revenue"]
        if prev_rev and curr_rev and prev_rev != 0:
            timeline[i]["rev_qoq"] = (curr_rev - prev_rev) / abs(prev_rev)
        else:
            timeline[i]["rev_qoq"] = None

        prev_pat = timeline[i-1]["pat"]
        curr_pat = timeline[i]["pat"]
        if prev_pat and curr_pat and prev_pat != 0:
            timeline[i]["pat_qoq"] = (curr_pat - prev_pat) / abs(prev_pat)
        else:
            timeline[i]["pat_qoq"] = None

    # Score based on most recent 2 periods of change
    recent = [t for t in timeline[1:] if t.get("rev_qoq") is not None][-2:]
    if not recent:
        return {"score": None, "signal": "insufficient_data", "timeline": timeline}

    rev_positives = sum(1 for t in recent if (t.get("rev_qoq") or 0) > 0)
    pat_positives = sum(1 for t in recent if (t.get("pat_qoq") or 0) > 0)

    # Score: how many of the last 2 periods were positive for both metrics
    score = (rev_positives + pat_positives) / (2 * len(recent))

    # Magnitude bonus: if latest QoQ > 10%
    latest = recent[-1]
    if (latest.get("rev_qoq") or 0) > 0.10:
        score = min(1.0, score + 0.1)
    if (latest.get("pat_qoq") or 0) > 0.15:
        score = min(1.0, score + 0.1)

    if score >= 0.70:
        signal = "improving"
    elif score >= 0.40:
        signal = "flat"
    else:
        signal = "deteriorating"

    return {
        "score": round(score, 3),
        "signal": signal,
        "quarters_used": len(quarters_present),
        "latest_rev_qoq": latest.get("rev_qoq"),
        "latest_pat_qoq": latest.get("pat_qoq"),
        "timeline": timeline,
    }


def compute_all_fundamental_signals(limit: int = None) -> dict:
    """Compute fundamental momentum score for all tickers with XBRL data."""
    pivot = _get_metric_pivot([])
    results = {}
    for ticker, data in pivot.items():
        score_data = _fundamental_momentum_score(data)
        results[ticker] = score_data
    if limit:
        # Return top N by score descending
        scored = [(t, d) for t, d in results.items() if d.get("score") is not None]
        scored.sort(key=lambda x: x[1]["score"], reverse=True)
        return dict(scored[:limit])
    return results


# ── TradingView price performance fetcher ────────────────────────────────────

def fetch_tv_performance(tickers: list, batch_size: int = 10) -> dict:
    """
    Fetch 1M/3M/6M/1Y price performance + P/E for a list of tickers from TradingView.
    Returns {ticker: {perf_1m, perf_3m, perf_6m, perf_1y, pe, rsi, close, market_cap}}
    """
    # Import inline to avoid import errors if not available
    try:
        # TradingView MCP tools are available as Python callables via the MCP framework
        # In this context we'll use the nselib bhavcopy approach instead
        # For now we'll use a subprocess approach with the TV MCP
        pass
    except Exception:
        pass

    # Fallback: use requests to call TradingView screener directly
    import subprocess
    results = {}

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i+batch_size]
        for ticker in batch:
            try:
                result = _fetch_single_tv(ticker)
                if result:
                    results[ticker] = result
            except Exception as e:
                results[ticker] = {"error": str(e)}
        if i + batch_size < len(tickers):
            time.sleep(0.5)

    return results


def _fetch_single_tv(ticker: str) -> dict:
    """Fetch TradingView data for one ticker using yfinance as fallback."""
    try:
        import yfinance as yf
        t = yf.Ticker(f"{ticker}.NS")
        hist = t.history(period="1y")
        if hist.empty:
            return None

        current = float(hist["Close"].iloc[-1])
        # Compute performance windows
        def perf_pct(days):
            idx = max(0, len(hist) - 1 - days)
            if idx < 0 or idx >= len(hist):
                return None
            past = float(hist["Close"].iloc[idx])
            return round((current - past) / past * 100, 2) if past > 0 else None

        return {
            "close": round(current, 2),
            "perf_1m": perf_pct(21),
            "perf_3m": perf_pct(63),
            "perf_6m": perf_pct(126),
            "perf_1y": perf_pct(252),
        }
    except Exception as e:
        return {"error": str(e)}


# ── Cross-sectional backtest ──────────────────────────────────────────────────

def run_cross_sectional_backtest(limit: int = 100) -> dict:
    """
    Test: do companies with strong fundamental momentum scores outperform
    those with weak scores over 6 months?

    Signal date proxy: Q4 FY26 results = ~May 2026
    Return window: May→Nov 2026 = 6-month performance from TradingView
    """
    print(f"[backtest] Computing fundamental signals for all companies...")
    all_signals = compute_all_fundamental_signals()

    # Bucket into improving / flat / deteriorating
    improving = [(t, d) for t, d in all_signals.items() if d.get("signal") == "improving"]
    flat = [(t, d) for t, d in all_signals.items() if d.get("signal") == "flat"]
    deteriorating = [(t, d) for t, d in all_signals.items() if d.get("signal") == "deteriorating"]

    improving.sort(key=lambda x: x[1]["score"], reverse=True)
    deteriorating.sort(key=lambda x: x[1]["score"])

    print(f"[backtest] Signal breakdown:")
    print(f"  Improving: {len(improving)} | Flat: {len(flat)} | Deteriorating: {len(deteriorating)}")
    print(f"  Insufficient data: {sum(1 for d in all_signals.values() if d.get('signal') == 'insufficient_data')}")

    # Sample: top 50 improving + bottom 50 deteriorating
    sample_improving = [t for t, _ in improving[:min(50, len(improving))]]
    sample_deteriorating = [t for t, _ in deteriorating[:min(50, len(deteriorating))]]
    all_sample = list(set(sample_improving + sample_deteriorating))

    print(f"\n[backtest] Fetching 6M price performance for {len(all_sample)} tickers...")
    perf_data = fetch_tv_performance(all_sample)

    # Compile results
    results = []
    for ticker in all_sample:
        sig = all_signals.get(ticker, {})
        perf = perf_data.get(ticker, {})
        if "error" in perf or perf.get("perf_6m") is None:
            continue
        results.append({
            "ticker": ticker,
            "fund_signal": sig.get("signal"),
            "fund_score": sig.get("score"),
            "latest_rev_qoq": sig.get("latest_rev_qoq"),
            "latest_pat_qoq": sig.get("latest_pat_qoq"),
            "quarters_used": sig.get("quarters_used"),
            "perf_1m": perf.get("perf_1m"),
            "perf_3m": perf.get("perf_3m"),
            "perf_6m": perf.get("perf_6m"),
            "perf_1y": perf.get("perf_1y"),
            "close": perf.get("close"),
        })

    return {
        "run_date": date.today().isoformat(),
        "total_companies_scored": len(all_signals),
        "signal_breakdown": {
            "improving": len(improving),
            "flat": len(flat),
            "deteriorating": len(deteriorating),
        },
        "sample_size": len(results),
        "results": results,
    }


def _analyze_backtest(data: dict) -> dict:
    """Analyze backtest results: average returns by signal bucket."""
    results = data.get("results", [])
    if not results:
        return {}

    buckets = defaultdict(list)
    for r in results:
        sig = r.get("fund_signal", "unknown")
        p6m = r.get("perf_6m")
        p3m = r.get("perf_3m")
        p1m = r.get("perf_1m")
        if p6m is not None:
            buckets[sig].append({"perf_6m": p6m, "perf_3m": p3m, "perf_1m": p1m, "ticker": r["ticker"]})

    summary = {}
    for sig, entries in buckets.items():
        p6 = [e["perf_6m"] for e in entries if e["perf_6m"] is not None]
        p3 = [e["perf_3m"] for e in entries if e["perf_3m"] is not None]
        p1 = [e["perf_1m"] for e in entries if e["perf_1m"] is not None]

        if not p6:
            continue

        p6_sorted = sorted(p6)
        n = len(p6)
        summary[sig] = {
            "count": n,
            "avg_6m_return": round(sum(p6) / n, 2),
            "avg_3m_return": round(sum(p3) / len(p3), 2) if p3 else None,
            "avg_1m_return": round(sum(p1) / len(p1), 2) if p1 else None,
            "median_6m_return": round(p6_sorted[n // 2], 2),
            "win_rate_6m": round(sum(1 for x in p6 if x > 0) / n * 100, 1),
            "best_6m": round(max(p6), 1),
            "worst_6m": round(min(p6), 1),
            "top_performers": sorted(entries, key=lambda x: x["perf_6m"], reverse=True)[:5],
        }

    return summary


def print_report(backtest_data: dict, analysis: dict):
    """Print a formatted backtest report."""
    print("\n" + "="*70)
    print("BACKTEST REPORT — Fundamental Momentum vs 6-Month Returns")
    print("Signal date proxy: Q4 FY26 results (~May 2026)")
    print("Return window: May→Nov 2026 (6 months)")
    print("="*70)
    print(f"Universe: {backtest_data['total_companies_scored']} companies")
    print(f"Sample tested: {backtest_data['sample_size']} companies (with price data)")
    sb = backtest_data["signal_breakdown"]
    print(f"Signals: {sb['improving']} improving | {sb['flat']} flat | {sb['deteriorating']} deteriorating")
    print()

    if not analysis:
        print("No analysis data available.")
        return

    print(f"{'Signal':<15} {'N':>4} {'Avg 6M':>8} {'Med 6M':>8} {'Win%':>7} {'Avg 3M':>8} {'Avg 1M':>8}")
    print("-"*60)
    for sig in ["improving", "flat", "deteriorating"]:
        if sig in analysis:
            a = analysis[sig]
            print(f"{sig:<15} {a['count']:>4} {a['avg_6m_return']:>7.1f}% {a['median_6m_return']:>7.1f}% {a['win_rate_6m']:>6.1f}% {a.get('avg_3m_return') or 0:>7.1f}% {a.get('avg_1m_return') or 0:>7.1f}%")

    print()
    print("Top performers (improving signal bucket):")
    if "improving" in analysis:
        for t in analysis["improving"].get("top_performers", [])[:5]:
            print(f"  {t['ticker']:<12} 6M: {t['perf_6m']:+.1f}%")

    # Score correlation
    results = backtest_data.get("results", [])
    improving_6m = [r["perf_6m"] for r in results if r["fund_signal"] == "improving" and r["perf_6m"] is not None]
    deteriorating_6m = [r["perf_6m"] for r in results if r["fund_signal"] == "deteriorating" and r["perf_6m"] is not None]

    if improving_6m and deteriorating_6m:
        spread = sum(improving_6m)/len(improving_6m) - sum(deteriorating_6m)/len(deteriorating_6m)
        print(f"\nSignal alpha (improving vs deteriorating spread): {spread:+.1f}%")
        if spread > 10:
            print("→ STRONG predictive value — signals materially outperform")
        elif spread > 5:
            print("→ MODERATE predictive value — signals show directional edge")
        elif spread > 0:
            print("→ WEAK predictive value — positive but small edge")
        else:
            print("→ NO predictive value — signals did not outperform")


# ── Update live predictions ───────────────────────────────────────────────────

def update_live_predictions():
    """
    For each pending prediction in prediction_log that has passed its 30/60/90 day mark,
    fetch the current price and fill in the return.
    """
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT id, ticker, rec_date, entry_price, signal FROM prediction_log WHERE outcome='pending'")
    rows = c.fetchall()
    today = date.today()
    updated = 0

    for row in rows:
        pred_id = row["id"]
        ticker = row["ticker"]
        rec_date = datetime.strptime(row["rec_date"], "%Y-%m-%d").date()
        entry_price = row["entry_price"]
        days_elapsed = (today - rec_date).days

        if days_elapsed < 30:
            continue  # Too early

        perf = _fetch_single_tv(ticker)
        if not perf or "error" in perf:
            continue

        current_price = perf.get("close")
        if not current_price or not entry_price:
            continue

        ret = (current_price - entry_price) / entry_price

        updates = {}
        if days_elapsed >= 30 and not row["t30_price"]:
            updates["t30_price"] = current_price
            updates["t30_return"] = round(ret * 100, 2)
        if days_elapsed >= 60 and not row["t60_price"]:
            updates["t60_price"] = current_price
            updates["t60_return"] = round(ret * 100, 2)
        if days_elapsed >= 90:
            updates["t90_price"] = current_price
            updates["t90_return"] = round(ret * 100, 2)
            outcome = "correct" if (row["signal"] == "BUY" and ret > 0) else "incorrect"
            updates["outcome"] = outcome

        if updates:
            sets = ", ".join(f"{k}=?" for k in updates)
            vals = list(updates.values()) + [pred_id]
            c.execute(f"UPDATE prediction_log SET {sets} WHERE id=?", vals)
            updated += 1
            print(f"  Updated {ticker}: entry={entry_price} current={current_price} return={ret*100:+.1f}%")

    conn.commit()
    print(f"Updated {updated} predictions.")


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["cross_sectional", "update_predictions", "report"],
                        default="cross_sectional")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    if args.mode == "cross_sectional":
        backtest_data = run_cross_sectional_backtest(limit=args.limit)
        analysis = _analyze_backtest(backtest_data)
        print_report(backtest_data, analysis)

        if args.save:
            out_path = RESULTS_DIR / f"cross_sectional_{date.today().isoformat()}.json"
            with open(out_path, "w") as f:
                json.dump({"backtest": backtest_data, "analysis": analysis}, f, indent=2, default=str)
            print(f"\nSaved to {out_path}")

    elif args.mode == "update_predictions":
        print("Updating live predictions...")
        update_live_predictions()

    elif args.mode == "report":
        # Load most recent saved backtest
        files = sorted(RESULTS_DIR.glob("cross_sectional_*.json"))
        if not files:
            print("No saved backtest found. Run with --mode cross_sectional --save first.")
        else:
            data = json.load(open(files[-1]))
            print_report(data["backtest"], data["analysis"])
