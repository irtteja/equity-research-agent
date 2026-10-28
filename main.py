#!/usr/bin/env python3
"""
Equity Research Agent — self-improving deep research on stocks.

Usage:
    python main.py "Research TCS.NS for long-term investment"
    python main.py "Compare RELIANCE.NS vs INFY.NS on growth metrics"
    python main.py --ticker HDFCBANK.NS "Analyze HDFC Bank's asset quality and NPA trends"
    python main.py --no-improve "Quick technical analysis of NIFTY50"
"""
import sys
import argparse
from agent.orchestrator import ResearchOrchestrator


def main():
    parser = argparse.ArgumentParser(description="Equity Research Agent")
    parser.add_argument("query", nargs="+", help="Research query")
    parser.add_argument("--ticker", "-t", default=None, help="Primary ticker symbol (e.g. RELIANCE.NS, AAPL)")
    parser.add_argument("--no-improve", action="store_true", help="Skip self-improvement step")
    args = parser.parse_args()

    query = " ".join(args.query)
    ticker = args.ticker

    # Try to auto-detect ticker from query if not given
    if not ticker:
        words = query.upper().split()
        for w in words:
            if ("." in w and len(w) < 15) or (w.isalpha() and 2 <= len(w) <= 6):
                ticker = w
                break

    agent = ResearchOrchestrator()
    agent.research(query, ticker=ticker, improve=not args.no_improve)


if __name__ == "__main__":
    main()
