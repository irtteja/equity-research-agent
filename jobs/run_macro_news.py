"""
Macro + News + Conviction daily cycle.

Runs three sequential phases:
  1. Global macro snapshot (yfinance — no auth)
  2. News sentiment scoring (ManagementStatement graph nodes)
  3. Daily conviction snapshot (blends bayesian + sentiment + macro + technical)

Writes to graph.db:
  - GlobalMacro node
  - SentimentScore nodes (per ticker)
  - conviction_history rows (per ticker)

No external API calls beyond yfinance public data.

Called by:
  jobs/orchestrator.py Phase 4
  CCR trigger (daily after market close)
  python jobs/run_macro_news.py [--macro-only] [--news-only] [--conviction-only]
"""
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))


def run_macro(verbose: bool = True) -> dict:
    """Phase 1: global macro."""
    from tools.global_macro import run as macro_run
    result = macro_run(verbose=verbose)
    return result


def run_news(tickers: list = None, days_back: int = 90,
             verbose: bool = True) -> list[dict]:
    """Phase 2: news sentiment for all tickers."""
    from agents.news_agent import run_all
    results = run_all(tickers=tickers, days_back=days_back, verbose=verbose)
    scored = [r for r in results if "sentiment_score" in r]
    if verbose:
        print(f"  [news] scored {len(scored)} tickers  "
              f"bullish={sum(1 for r in scored if r.get('signal')=='bullish')}  "
              f"bearish={sum(1 for r in scored if r.get('signal')=='bearish')}")
    return results


def run_conviction(tickers: list = None, verbose: bool = True) -> list[dict]:
    """Phase 3: daily conviction snapshot."""
    from memory.conviction_tracker import record_daily_conviction
    results = record_daily_conviction(tickers=tickers, verbose=verbose)
    ok = [r for r in results if "conviction_score" in r]
    if verbose:
        strong_buy = sum(1 for r in ok if r.get("signal") == "strong_buy")
        buy        = sum(1 for r in ok if r.get("signal") == "buy")
        sell       = sum(1 for r in ok if r.get("signal") in ("sell", "strong_sell"))
        print(f"  [conv] recorded {len(ok)} tickers  "
              f"strong_buy={strong_buy}  buy={buy}  sell={sell}")
    return results


def run(macro: bool = True, news: bool = True, conviction: bool = True,
        verbose: bool = True) -> dict:
    """Full macro/news/conviction cycle."""
    start = datetime.now()
    if verbose:
        print(f"\n[macro_news] {start.strftime('%Y-%m-%d %H:%M:%S')} — starting")

    macro_result = None
    news_results = None
    conviction_results = None

    if macro:
        if verbose:
            print("  Phase 1: global macro")
        macro_result = run_macro(verbose=verbose)

    if news:
        if verbose:
            print("  Phase 2: news sentiment")
        news_results = run_news(verbose=verbose)

    if conviction:
        if verbose:
            print("  Phase 3: conviction snapshot")
        conviction_results = run_conviction(verbose=verbose)

    elapsed = (datetime.now() - start).total_seconds()
    if verbose:
        print(f"[macro_news] done in {elapsed:.1f}s")

    return {
        "macro":      macro_result,
        "news_count": len(news_results) if news_results else 0,
        "conv_count": len(conviction_results) if conviction_results else 0,
        "elapsed_s":  round(elapsed, 1),
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Macro + News + Conviction cycle")
    p.add_argument("--macro-only",      action="store_true")
    p.add_argument("--news-only",       action="store_true")
    p.add_argument("--conviction-only", action="store_true")
    p.add_argument("--quiet",           action="store_true")
    args = p.parse_args()

    verbose = not args.quiet

    if args.macro_only:
        run(macro=True, news=False, conviction=False, verbose=verbose)
    elif args.news_only:
        run(macro=False, news=True, conviction=False, verbose=verbose)
    elif args.conviction_only:
        run(macro=False, news=False, conviction=True, verbose=verbose)
    else:
        result = run(verbose=verbose)
        import json
        print(json.dumps({k: v for k, v in result.items() if k != "macro"}, indent=2))
