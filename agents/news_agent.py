"""
NewsAgent — keyword-based sentiment scoring from ManagementStatement nodes.

Scans ManagementStatement and CorporateEvent nodes in graph.db for bull/bear
signals. Writes SentimentScore nodes per ticker with rolling 30-day sentiment.

No external API calls. Pure graph analysis.

Called by:
  jobs/run_macro_news.py
  jobs/orchestrator.py Phase 4
"""
import re
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path

REPO = Path(__file__).parent.parent
import sys
sys.path.insert(0, str(REPO))

DB_PATH = REPO / "memory_store" / "graph.db"


BEAR_KEYWORDS = [
    "resignation", "fraud", "investigation", "penalty", "default",
    "downgrade", "loss", "decline", "miss", "shortfall", "warning",
    "recall", "lawsuit", "litigation", "breach", "violation", "suspension",
    "regulatory action", "sebi", "enforcement", "audit qualification",
    "going concern", "impairment", "write-off", "write-down", "restatement",
    "delay", "deferral", "cancellation", "termination", "layoff", "restructuring",
    "debt", "npa", "stressed", "rating downgrade", "credit watch",
    "promoter pledge", "margin call", "block deal sell", "insider sell",
    "profit warning", "guidance cut", "revenue miss", "ebitda miss",
]

BULL_KEYWORDS = [
    "dividend", "buyback", "bonus", "split", "rights",
    "acquisition", "merger", "joint venture", "mou", "contract", "order",
    "expansion", "capacity", "capex", "new plant", "new product",
    "rating upgrade", "credit rating", "increase", "beat", "outperform",
    "record revenue", "record profit", "highest ever", "strong growth",
    "guidance raise", "positive outlook", "new client", "new order",
    "fii buying", "dii buying", "bulk deal buy", "insider buy",
    "promoter increase", "promoter buy", "stake increase",
    "demerger", "listing", "ipo", "ncd", "qip",
    "export order", "government contract", "approval", "licence",
    "patent", "innovation", "technology", "digital", "ai", "automation",
]


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _score_text(text: str) -> tuple[int, int, list, list]:
    """Return (bull_count, bear_count, bull_hits, bear_hits) for a text string."""
    if not text:
        return 0, 0, [], []
    low = text.lower()
    bull_hits = [kw for kw in BULL_KEYWORDS if kw in low]
    bear_hits = [kw for kw in BEAR_KEYWORDS if kw in low]
    return len(bull_hits), len(bear_hits), bull_hits, bear_hits


def _get_statements_for_ticker(ticker: str, days_back: int = 90) -> list[dict]:
    """Fetch ManagementStatement and CorporateEvent nodes for a ticker."""
    cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()
    rows = []
    with _conn() as c:
        # ManagementStatement nodes
        for row in c.execute(
            """SELECT name, properties, created_at FROM nodes
               WHERE node_type IN ('ManagementStatement','CorporateEvent','NewsEvent')
               AND ticker = ? AND created_at >= ?
               ORDER BY created_at DESC LIMIT 200""",
            (ticker, cutoff),
        ).fetchall():
            props = {}
            try:
                import json
                props = json.loads(row["properties"] or "{}")
            except Exception:
                pass
            rows.append({
                "name":       row["name"],
                "created_at": row["created_at"],
                "text":       " ".join([
                    str(props.get("statement", "")),
                    str(props.get("headline", "")),
                    str(props.get("description", "")),
                    str(props.get("body", "")),
                    str(props.get("content", "")),
                    str(props.get("subject", "")),
                    str(props.get("announcement", "")),
                ]),
            })
    return rows


def analyze_ticker(ticker: str, days_back: int = 90) -> dict:
    """
    Score sentiment for a ticker from recent management/corporate statements.
    Returns {ticker, sentiment_score, bull_count, bear_count, bull_hits, bear_hits,
             statement_count, signal, verdict, days_analyzed}.
    """
    statements = _get_statements_for_ticker(ticker, days_back)

    total_bull, total_bear = 0, 0
    all_bull_hits: list = []
    all_bear_hits: list = []

    for stmt in statements:
        b, br, bh, brh = _score_text(stmt["text"])
        total_bull += b
        total_bear += br
        all_bull_hits.extend(bh)
        all_bear_hits.extend(brh)

    statement_count = len(statements)
    if statement_count == 0 or (total_bull + total_bear) == 0:
        return {
            "ticker":          ticker,
            "sentiment_score": 0.5,
            "bull_count":      0,
            "bear_count":      0,
            "bull_hits":       [],
            "bear_hits":       [],
            "statement_count": 0,
            "signal":          "neutral",
            "verdict":         "no_data",
            "days_analyzed":   days_back,
        }

    # Sentiment score: bull share, anchored to 0.5
    total_hits = total_bull + total_bear
    raw_score = total_bull / total_hits  # 0–1
    # Scale: 0.3–0.7 range (avoid extremes from sparse data)
    sentiment_score = round(0.30 + raw_score * 0.40, 3)

    # Adjust for statement volume (more statements = higher confidence)
    if statement_count >= 5:
        pass  # full weight
    elif statement_count >= 2:
        sentiment_score = round(0.5 + (sentiment_score - 0.5) * 0.6, 3)
    else:
        sentiment_score = round(0.5 + (sentiment_score - 0.5) * 0.3, 3)

    if sentiment_score >= 0.58:
        signal  = "bullish"
        verdict = "positive_news_flow"
    elif sentiment_score <= 0.42:
        signal  = "bearish"
        verdict = "negative_news_flow"
    else:
        signal  = "neutral"
        verdict = "mixed_news_flow"

    # Deduplicate hit lists
    unique_bull = list(dict.fromkeys(all_bull_hits))[:10]
    unique_bear = list(dict.fromkeys(all_bear_hits))[:10]

    return {
        "ticker":          ticker,
        "sentiment_score": sentiment_score,
        "bull_count":      total_bull,
        "bear_count":      total_bear,
        "bull_hits":       unique_bull,
        "bear_hits":       unique_bear,
        "statement_count": statement_count,
        "signal":          signal,
        "verdict":         verdict,
        "days_analyzed":   days_back,
    }


def run_all(tickers: list = None, days_back: int = 90,
            limit: int = 200, verbose: bool = False) -> list[dict]:
    """
    Score sentiment for all given tickers (or all ConfluenceSignal tickers).
    Writes SentimentScore nodes to graph.db.
    Returns list of result dicts.
    """
    if tickers is None:
        # Pull from ConfluenceSignal nodes
        with _conn() as c:
            rows = c.execute(
                "SELECT DISTINCT ticker FROM nodes WHERE node_type='ConfluenceSignal' "
                "AND ticker IS NOT NULL LIMIT ?", (limit,)
            ).fetchall()
        tickers = [r[0] for r in rows]

    if not tickers:
        # Fallback: all tickers with any node
        with _conn() as c:
            rows = c.execute(
                "SELECT DISTINCT ticker FROM nodes WHERE ticker IS NOT NULL LIMIT ?",
                (limit,)
            ).fetchall()
        tickers = [r[0] for r in rows]

    from memory.graph_store import get_store
    gs = get_store()

    results = []
    for ticker in tickers[:limit]:
        try:
            result = analyze_ticker(ticker, days_back)
            # Write SentimentScore node
            gs.upsert_node(
                node_type="SentimentScore",
                name=f"SentimentScore_{ticker}_{date.today()}",
                ticker=ticker,
                properties={
                    "sentiment_score":  result["sentiment_score"],
                    "bull_count":       result["bull_count"],
                    "bear_count":       result["bear_count"],
                    "bull_hits":        result["bull_hits"],
                    "bear_hits":        result["bear_hits"],
                    "statement_count":  result["statement_count"],
                    "signal":           result["signal"],
                    "verdict":          result["verdict"],
                    "days_analyzed":    result["days_analyzed"],
                    "scored_at":        datetime.now().isoformat(),
                },
            )
            results.append(result)
            if verbose:
                print(f"  [news] {ticker:12s}  score={result['sentiment_score']:.3f}  "
                      f"signal={result['signal']:8s}  stmts={result['statement_count']}")
        except Exception as e:
            results.append({"ticker": ticker, "status": "error", "error": str(e)})

    return results


def get_top_sentiment(n: int = 20, signal: str = None) -> list[dict]:
    """
    Return top N tickers by sentiment score from most-recent SentimentScore nodes.
    signal: 'bullish', 'bearish', or None for all.
    """
    import json
    rows = []
    with _conn() as c:
        for row in c.execute(
            """SELECT ticker, properties, created_at FROM nodes
               WHERE node_type='SentimentScore' AND ticker IS NOT NULL
               ORDER BY created_at DESC LIMIT 2000"""
        ).fetchall():
            try:
                props = json.loads(row["properties"] or "{}")
                ticker = row["ticker"]
                sig = props.get("signal", "neutral")
                score = props.get("sentiment_score", 0.5)
                rows.append({"ticker": ticker, "signal": sig, "sentiment_score": score,
                             "bull_hits": props.get("bull_hits", []),
                             "bear_hits": props.get("bear_hits", [])})
            except Exception:
                pass

    # Keep latest per ticker
    seen: dict = {}
    for r in rows:
        t = r["ticker"]
        if t not in seen:
            seen[t] = r

    filtered = list(seen.values())
    if signal:
        filtered = [r for r in filtered if r["signal"] == signal]

    filtered.sort(key=lambda x: x["sentiment_score"],
                  reverse=(signal != "bearish"))
    return filtered[:n]


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="News sentiment agent")
    p.add_argument("--ticker", help="Single ticker to analyze")
    p.add_argument("--run-all", action="store_true", help="Score all signal tickers")
    p.add_argument("--top-bullish", action="store_true")
    p.add_argument("--top-bearish", action="store_true")
    p.add_argument("--days", type=int, default=90)
    args = p.parse_args()

    if args.ticker:
        import json
        result = analyze_ticker(args.ticker, days_back=args.days)
        print(json.dumps(result, indent=2, default=str))
    elif args.run_all:
        results = run_all(days_back=args.days, verbose=True)
        scored = [r for r in results if "sentiment_score" in r]
        print(f"\nScored {len(scored)} tickers")
    elif args.top_bullish:
        import json
        print(json.dumps(get_top_sentiment(20, "bullish"), indent=2, default=str))
    elif args.top_bearish:
        import json
        print(json.dumps(get_top_sentiment(20, "bearish"), indent=2, default=str))
    else:
        p.print_help()
