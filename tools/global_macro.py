"""
Global macro overlay — USD/INR, crude oil, US rates, global indices.
Provides macro context for Indian equity signals.

No API key needed — uses yfinance public data.
Called by jobs/run_macro_news.py and jobs/orchestrator.py Phase 4.
"""
import json
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).parent.parent
import sys
sys.path.insert(0, str(REPO))

MACRO_TICKERS = {
    "USDINR=X": ("USD/INR",       "fx"),
    "CL=F":     ("Crude Oil WTI", "commodity"),
    "GC=F":     ("Gold",          "commodity"),
    "^TNX":     ("US 10Y Yield",  "rates"),
    "^GSPC":    ("S&P 500",       "index"),
    "^N225":    ("Nikkei 225",    "index"),
    "^HSI":     ("Hang Seng",     "index"),
    "^DJI":     ("Dow Jones",     "index"),
}

# India-specific macro thresholds
_THRESHOLDS = {
    "USD/INR":       {"bearish_above": 84.5, "bullish_below": 83.0},
    "Crude Oil WTI": {"bearish_above": 90.0, "bullish_below": 75.0},
    "US 10Y Yield":  {"bearish_above": 4.5,  "bullish_below": 4.0},
    "S&P 500":       {"bullish_pct_above": 0.5, "bearish_pct_below": -0.5},
}


def fetch_macro_snapshot() -> dict:
    """
    Fetch current price + 1d change % for all macro tickers via yfinance.
    Returns: {name: {price, change_pct, type, symbol}}
    """
    try:
        import yfinance as yf
    except ImportError:
        return {}

    snapshot = {}
    symbols = list(MACRO_TICKERS.keys())

    try:
        # Batch download last 2 days
        data = yf.download(symbols, period="2d", auto_adjust=True,
                           progress=False, threads=True)
        close = data.get("Close", data) if hasattr(data, "get") else data

        for sym, (name, mtype) in MACRO_TICKERS.items():
            try:
                if sym in close.columns:
                    prices = close[sym].dropna()
                    if len(prices) >= 2:
                        prev  = float(prices.iloc[-2])
                        curr  = float(prices.iloc[-1])
                        chg   = ((curr - prev) / prev * 100) if prev else 0.0
                    elif len(prices) == 1:
                        curr  = float(prices.iloc[-1])
                        chg   = 0.0
                    else:
                        continue
                    snapshot[name] = {
                        "price":      round(curr, 4),
                        "change_pct": round(chg, 3),
                        "type":       mtype,
                        "symbol":     sym,
                    }
            except Exception:
                pass
    except Exception:
        # Fallback: individual tickers
        for sym, (name, mtype) in MACRO_TICKERS.items():
            try:
                t = yf.Ticker(sym)
                hist = t.history(period="2d")
                if len(hist) >= 2:
                    prev = float(hist["Close"].iloc[-2])
                    curr = float(hist["Close"].iloc[-1])
                    chg  = ((curr - prev) / prev * 100) if prev else 0.0
                elif len(hist) == 1:
                    curr = float(hist["Close"].iloc[-1])
                    chg  = 0.0
                else:
                    continue
                snapshot[name] = {"price": round(curr, 4), "change_pct": round(chg, 3),
                                  "type": mtype, "symbol": sym}
            except Exception:
                pass

    return snapshot


def compute_macro_signal(snapshot: dict) -> dict:
    """
    Derive an India-macro signal from global context.
    Returns: {macro_score, signal, factors, verdict}
    """
    if not snapshot:
        return {"macro_score": 0.5, "signal": "neutral", "factors": [], "verdict": "no_data"}

    factors = []
    score   = 0.5  # neutral baseline

    usdinr = snapshot.get("USD/INR", {}).get("price", 0)
    crude  = snapshot.get("Crude Oil WTI", {}).get("price", 0)
    t10y   = snapshot.get("US 10Y Yield", {}).get("price", 0)
    sp500c = snapshot.get("S&P 500", {}).get("change_pct", 0)

    # USD/INR — rupee weakness hurts FII flows
    if usdinr > 84.5:
        score -= 0.10
        factors.append({"factor": "USD/INR", "value": usdinr, "impact": "bearish",
                         "reason": f"Rupee weak at {usdinr:.2f} (>84.5) — FII outflows likely"})
    elif usdinr < 83.0:
        score += 0.08
        factors.append({"factor": "USD/INR", "value": usdinr, "impact": "bullish",
                         "reason": f"Rupee strong at {usdinr:.2f} (<83.0) — supportive for FIIs"})

    # Crude — India is net importer; high crude = inflation + CAD pressure
    if crude > 90:
        score -= 0.12
        factors.append({"factor": "Crude", "value": crude, "impact": "bearish",
                         "reason": f"Crude at ${crude:.1f} (>$90) — inflationary, CAD pressure"})
    elif crude < 75:
        score += 0.08
        factors.append({"factor": "Crude", "value": crude, "impact": "bullish",
                         "reason": f"Crude at ${crude:.1f} (<$75) — positive for India economy"})

    # US 10Y yield — high yield pulls FII from EM to US bonds
    if t10y > 4.5:
        score -= 0.10
        factors.append({"factor": "US 10Y", "value": t10y, "impact": "bearish",
                         "reason": f"US 10Y at {t10y:.2f}% (>4.5%) — EM outflow risk"})
    elif t10y < 4.0:
        score += 0.07
        factors.append({"factor": "US 10Y", "value": t10y, "impact": "bullish",
                         "reason": f"US 10Y at {t10y:.2f}% (<4.0%) — EM inflow supportive"})

    # S&P 500 — risk-on/off global signal
    if sp500c > 0.5:
        score += 0.08
        factors.append({"factor": "S&P 500", "value": sp500c, "impact": "bullish",
                         "reason": f"S&P 500 up {sp500c:.2f}% — risk-on, FII inflows likely"})
    elif sp500c < -0.5:
        score -= 0.08
        factors.append({"factor": "S&P 500", "value": sp500c, "impact": "bearish",
                         "reason": f"S&P 500 down {sp500c:.2f}% — risk-off, FII outflows"})

    score = round(max(0.0, min(1.0, score)), 3)

    if score >= 0.60:
        signal  = "risk_on"
        verdict = "macro_positive"
    elif score <= 0.35:
        signal  = "risk_off"
        verdict = "macro_negative"
    else:
        signal  = "neutral"
        verdict = "macro_mixed"

    return {
        "macro_score":  score,
        "signal":       signal,
        "verdict":      verdict,
        "factors":      factors,
        "usdinr":       usdinr,
        "crude":        crude,
        "us10y":        t10y,
        "sp500_chg":    sp500c,
    }


def save_macro_to_graph(snapshot: dict, signal: dict) -> None:
    """Write GlobalMacro node to graph.db."""
    try:
        from memory.graph_store import get_store
        gs = get_store()
        props = {}
        props.update(snapshot)
        props.update(signal)
        props["fetched_at"] = datetime.now().isoformat()
        gs.upsert_node(
            node_type="GlobalMacro",
            name=f"GlobalMacro_{date.today()}",
            ticker=None,
            properties=props,
        )
    except Exception as e:
        print(f"  [macro] save_to_graph failed: {e}")


def run(verbose: bool = True) -> dict:
    """Full macro cycle: fetch → signal → save."""
    snapshot = fetch_macro_snapshot()
    signal   = compute_macro_signal(snapshot)
    save_macro_to_graph(snapshot, signal)
    if verbose:
        print(f"  [macro] signal={signal['signal']}  score={signal['macro_score']:.2f}  "
              f"usdinr={signal.get('usdinr',0):.2f}  crude={signal.get('crude',0):.1f}  "
              f"us10y={signal.get('us10y',0):.2f}%")
    return {"snapshot": snapshot, "signal": signal}


if __name__ == "__main__":
    result = run(verbose=True)
    import json
    print(json.dumps(result["signal"], indent=2, default=str))
