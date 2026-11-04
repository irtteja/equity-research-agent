"""
ConvictionTracker — daily conviction snapshots and trajectory analysis.

Stores daily conviction_history rows combining:
  - Bayesian confidence from belief_engine
  - Sentiment score from news_agent
  - Macro score from global_macro
  - Technical signal from technical_engine (if available)

Provides: rising/falling conviction tickers, trajectory plots data,
and cross-signal convergence detection.

No external API calls. Pure SQLite analysis.

Called by:
  jobs/orchestrator.py Phase 4
  python -m memory.conviction_tracker [--record] [--rising] [--report TICKER]
"""
import json
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path

REPO = Path(__file__).parent.parent
import sys
sys.path.insert(0, str(REPO))

DB_PATH = REPO / "memory_store" / "graph.db"


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _ensure_table():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS conviction_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT NOT NULL,
                record_date     TEXT NOT NULL,
                bayesian_conf   REAL,
                sentiment_score REAL,
                macro_score     REAL,
                technical_score REAL,
                conviction_score REAL,
                signal          TEXT,
                node_count      INTEGER,
                belief_state    TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(ticker, record_date)
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ch_ticker ON conviction_history(ticker)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ch_date ON conviction_history(record_date)")


_ensure_table()


def _get_bayesian_conf(ticker: str) -> tuple[float, str, int]:
    """Return (avg_confidence, dominant_belief_state, node_count) for ticker."""
    with _conn() as c:
        rows = c.execute(
            """SELECT confidence, belief_state FROM nodes
               WHERE ticker = ? AND confidence IS NOT NULL
               AND node_type IN ('ConfluenceSignal','TechnicalSignal','ValuationSignal',
                                 'PriceSnapshot','FundamentalData')
               LIMIT 500""",
            (ticker,),
        ).fetchall()

    if not rows:
        # Fallback: any node
        with _conn() as c:
            rows = c.execute(
                "SELECT confidence, belief_state FROM nodes WHERE ticker=? AND confidence IS NOT NULL LIMIT 500",
                (ticker,),
            ).fetchall()

    if not rows:
        return 0.5, "forming", 0

    confs = [r["confidence"] for r in rows if r["confidence"]]
    states = [r["belief_state"] for r in rows if r["belief_state"]]

    avg_conf = round(sum(confs) / len(confs), 4) if confs else 0.5
    dominant = max(set(states), key=states.count) if states else "forming"
    return avg_conf, dominant, len(rows)


def _get_latest_sentiment(ticker: str) -> float:
    """Return most recent SentimentScore for ticker."""
    with _conn() as c:
        row = c.execute(
            """SELECT properties FROM nodes
               WHERE node_type='SentimentScore' AND ticker=?
               ORDER BY created_at DESC LIMIT 1""",
            (ticker,),
        ).fetchone()
    if not row:
        return 0.5
    try:
        props = json.loads(row["properties"] or "{}")
        return float(props.get("sentiment_score", 0.5))
    except Exception:
        return 0.5


def _get_latest_macro_score() -> float:
    """Return most recent global macro score."""
    with _conn() as c:
        row = c.execute(
            """SELECT properties FROM nodes
               WHERE node_type='GlobalMacro'
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return 0.5
    try:
        props = json.loads(row["properties"] or "{}")
        return float(props.get("macro_score", 0.5))
    except Exception:
        return 0.5


def _get_latest_technical_score(ticker: str) -> float:
    """Return most recent TechnicalSignal score for ticker."""
    with _conn() as c:
        row = c.execute(
            """SELECT properties FROM nodes
               WHERE node_type='TechnicalSignal' AND ticker=?
               ORDER BY created_at DESC LIMIT 1""",
            (ticker,),
        ).fetchone()
    if not row:
        return 0.5
    try:
        props = json.loads(row["properties"] or "{}")
        # Try composite_score, then signal_score, then derive from signal
        score = props.get("composite_score") or props.get("signal_score")
        if score is not None:
            return float(score)
        sig = str(props.get("signal", "neutral")).lower()
        return 0.65 if "bull" in sig else 0.35 if "bear" in sig else 0.5
    except Exception:
        return 0.5


def compute_conviction_score(bayesian: float, sentiment: float,
                              macro: float, technical: float) -> float:
    """
    Weighted blend:
      Bayesian confidence: 40%  (what graph evidence says)
      Technical signal:    25%  (price action)
      Sentiment score:     20%  (news/mgmt tone)
      Macro score:         15%  (global context)
    """
    score = (
        bayesian  * 0.40 +
        technical * 0.25 +
        sentiment * 0.20 +
        macro     * 0.15
    )
    return round(min(1.0, max(0.0, score)), 4)


def record_daily_conviction(tickers: list = None,
                             record_date: str = None,
                             verbose: bool = False) -> list[dict]:
    """
    Record today's conviction snapshot for all given tickers.
    Returns list of recorded rows.
    """
    if record_date is None:
        record_date = str(date.today())

    if tickers is None:
        with _conn() as c:
            rows = c.execute(
                "SELECT DISTINCT ticker FROM nodes WHERE node_type='ConfluenceSignal' "
                "AND ticker IS NOT NULL LIMIT 500"
            ).fetchall()
        tickers = [r[0] for r in rows]

    if not tickers:
        with _conn() as c:
            rows = c.execute(
                "SELECT DISTINCT ticker FROM nodes WHERE ticker IS NOT NULL LIMIT 500"
            ).fetchall()
        tickers = [r[0] for r in rows]

    macro_score = _get_latest_macro_score()
    results = []

    for ticker in tickers:
        try:
            bayesian, belief_state, node_count = _get_bayesian_conf(ticker)
            sentiment = _get_latest_sentiment(ticker)
            technical = _get_latest_technical_score(ticker)
            conviction = compute_conviction_score(bayesian, sentiment, macro_score, technical)

            if conviction >= 0.65:
                signal = "strong_buy"
            elif conviction >= 0.57:
                signal = "buy"
            elif conviction <= 0.35:
                signal = "strong_sell"
            elif conviction <= 0.43:
                signal = "sell"
            else:
                signal = "hold"

            with _conn() as c:
                c.execute(
                    """INSERT OR REPLACE INTO conviction_history
                       (ticker, record_date, bayesian_conf, sentiment_score, macro_score,
                        technical_score, conviction_score, signal, node_count, belief_state)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (ticker, record_date, bayesian, sentiment, macro_score,
                     technical, conviction, signal, node_count, belief_state),
                )

            row = {
                "ticker": ticker, "record_date": record_date,
                "conviction_score": conviction, "signal": signal,
                "bayesian_conf": bayesian, "sentiment_score": sentiment,
                "macro_score": macro_score, "technical_score": technical,
                "node_count": node_count, "belief_state": belief_state,
            }
            results.append(row)
            if verbose:
                print(f"  [conv] {ticker:12s}  conv={conviction:.3f}  signal={signal:12s}  "
                      f"bayes={bayesian:.3f}  sent={sentiment:.3f}  tech={technical:.3f}")
        except Exception as e:
            results.append({"ticker": ticker, "status": "error", "error": str(e)})

    return results


def get_trajectory(ticker: str, days: int = 30) -> list[dict]:
    """Return conviction_history for ticker over last N days."""
    cutoff = str(date.today() - timedelta(days=days))
    with _conn() as c:
        rows = c.execute(
            """SELECT record_date, conviction_score, signal, bayesian_conf,
                      sentiment_score, technical_score, macro_score, belief_state
               FROM conviction_history
               WHERE ticker=? AND record_date >= ?
               ORDER BY record_date ASC""",
            (ticker, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def get_rising_conviction(min_days: int = 3, min_gain: float = 0.05,
                           top_n: int = 20) -> list[dict]:
    """
    Find tickers with monotonically rising conviction over last min_days days.
    Returns list sorted by conviction gain descending.
    """
    cutoff = str(date.today() - timedelta(days=min_days + 2))
    with _conn() as c:
        rows = c.execute(
            """SELECT ticker, record_date, conviction_score
               FROM conviction_history
               WHERE record_date >= ?
               ORDER BY ticker, record_date""",
            (cutoff,),
        ).fetchall()

    # Group by ticker
    by_ticker: dict = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(
            (r["record_date"], r["conviction_score"])
        )

    rising = []
    for ticker, history in by_ticker.items():
        if len(history) < min_days:
            continue
        scores = [h[1] for h in history[-min_days:]]
        gain = scores[-1] - scores[0]
        if gain < min_gain:
            continue
        # Check monotonic (allow one dip)
        dips = sum(1 for i in range(1, len(scores)) if scores[i] < scores[i-1])
        if dips > 1:
            continue
        rising.append({
            "ticker":           ticker,
            "conviction_gain":  round(gain, 4),
            "latest_score":     scores[-1],
            "earliest_score":   scores[0],
            "days":             len(scores),
            "trajectory":       scores,
        })

    rising.sort(key=lambda x: x["conviction_gain"], reverse=True)
    return rising[:top_n]


def get_falling_conviction(min_days: int = 3, min_drop: float = 0.05,
                            top_n: int = 20) -> list[dict]:
    """Find tickers with falling conviction — potential exit signals."""
    cutoff = str(date.today() - timedelta(days=min_days + 2))
    with _conn() as c:
        rows = c.execute(
            """SELECT ticker, record_date, conviction_score
               FROM conviction_history
               WHERE record_date >= ?
               ORDER BY ticker, record_date""",
            (cutoff,),
        ).fetchall()

    by_ticker: dict = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(
            (r["record_date"], r["conviction_score"])
        )

    falling = []
    for ticker, history in by_ticker.items():
        if len(history) < min_days:
            continue
        scores = [h[1] for h in history[-min_days:]]
        drop = scores[0] - scores[-1]
        if drop < min_drop:
            continue
        spikes = sum(1 for i in range(1, len(scores)) if scores[i] > scores[i-1])
        if spikes > 1:
            continue
        falling.append({
            "ticker":          ticker,
            "conviction_drop": round(drop, 4),
            "latest_score":    scores[-1],
            "earliest_score":  scores[0],
            "days":            len(scores),
            "trajectory":      scores,
        })

    falling.sort(key=lambda x: x["conviction_drop"], reverse=True)
    return falling[:top_n]


def get_top_conviction(n: int = 20, signal_filter: str = None) -> list[dict]:
    """Return top N tickers by latest conviction score."""
    with _conn() as c:
        rows = c.execute(
            """SELECT ticker, conviction_score, signal, belief_state, record_date
               FROM conviction_history
               WHERE record_date = (
                   SELECT MAX(record_date) FROM conviction_history
               )
               ORDER BY conviction_score DESC""",
        ).fetchall()

    result = [dict(r) for r in rows]
    if signal_filter:
        result = [r for r in result if r["signal"] == signal_filter]
    return result[:n]


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Conviction tracker")
    p.add_argument("--record", action="store_true", help="Record today's conviction")
    p.add_argument("--rising", action="store_true", help="Show rising conviction tickers")
    p.add_argument("--falling", action="store_true", help="Show falling conviction tickers")
    p.add_argument("--top", action="store_true", help="Show top conviction tickers")
    p.add_argument("--report", metavar="TICKER", help="Conviction trajectory for ticker")
    p.add_argument("--days", type=int, default=30)
    args = p.parse_args()

    if args.record:
        results = record_daily_conviction(verbose=True)
        ok = [r for r in results if "conviction_score" in r]
        print(f"\nRecorded {len(ok)} tickers")
    elif args.rising:
        print(json.dumps(get_rising_conviction(), indent=2, default=str))
    elif args.falling:
        print(json.dumps(get_falling_conviction(), indent=2, default=str))
    elif args.top:
        print(json.dumps(get_top_conviction(20), indent=2, default=str))
    elif args.report:
        print(json.dumps(get_trajectory(args.report, days=args.days), indent=2, default=str))
    else:
        p.print_help()
