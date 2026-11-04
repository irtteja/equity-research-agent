"""
technical_engine.py — compute RSI, EMA, MACD, 52W position from bhavcopy CSVs.

Reads daily OHLCV from memory_store/bhavcopy/*.csv (249 days available).
Writes TechnicalSignal nodes to graph.db.

Called by: agents/technical_agent.py
"""
import csv
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"
BHAV_DIR = REPO / "memory_store" / "bhavcopy"

MIN_PERIODS = 14   # minimum days needed
SME_SERIES  = {"SM", "ST", "SE"}  # NSE SME series to skip


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _load_price_series(ticker: str) -> list[dict]:
    """Return sorted list of {date, open, high, low, close, volume} from bhavcopy CSVs."""
    rows = []
    for f in sorted(BHAV_DIR.glob("*.csv")):
        try:
            with open(f, newline="") as fh:
                for row in csv.DictReader(fh):
                    if row.get("TckrSymb") != ticker:
                        continue
                    if row.get("SctySrs", "EQ") in SME_SERIES:
                        continue
                    rows.append({
                        "date":   row["TradDt"],
                        "open":   float(row["OpnPric"] or 0),
                        "high":   float(row["HghPric"] or 0),
                        "low":    float(row["LwPric"] or 0),
                        "close":  float(row["ClsPric"] or row["LastPric"] or 0),
                        "volume": float(row.get("TtlTradgVol") or 0),
                    })
        except Exception:
            continue
    rows.sort(key=lambda r: r["date"])
    return rows


def _ema(values: list[float], period: int) -> list[float]:
    """Compute EMA for a list of closing prices."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 2)


def compute_technicals(ticker: str) -> dict | None:
    """
    Compute technical indicators for ticker.
    Returns dict of indicators or None if insufficient data.
    """
    series = _load_price_series(ticker)
    if len(series) < MIN_PERIODS:
        return None

    closes  = [r["close"] for r in series]
    volumes = [r["volume"] for r in series]
    highs   = [r["high"] for r in series]
    lows    = [r["low"] for r in series]

    # RSI(14)
    rsi = _rsi(closes, 14)

    # EMAs
    ema20_series = _ema(closes, 20)
    ema50_series = _ema(closes, 50)
    ema20 = round(ema20_series[-1], 2) if ema20_series else None
    ema50 = round(ema50_series[-1], 2) if ema50_series else None

    # MACD(12, 26, 9)
    ema12_series = _ema(closes, 12)
    ema26_series = _ema(closes, 26)
    macd_line, macd_signal_val, macd_hist = None, None, None
    if ema12_series and ema26_series:
        # Align: ema26 starts later; compute MACD from the overlap
        offset = 26 - 12
        macd_values = [ema12_series[offset + i] - ema26_series[i] for i in range(len(ema26_series))]
        signal_series = _ema(macd_values, 9)
        if macd_values and signal_series:
            macd_line       = round(macd_values[-1], 4)
            macd_signal_val = round(signal_series[-1], 4)
            macd_hist       = round(macd_line - macd_signal_val, 4)

    # 52-week position (using all data up to 252 trading days)
    window_52w = series[-252:] if len(series) >= 252 else series
    high52w = max(r["high"] for r in window_52w)
    low52w  = min(r["low"]  for r in window_52w)
    close   = closes[-1]
    pos_52w = round((close - low52w) / (high52w - low52w), 4) if high52w != low52w else 0.5

    # Volume trend: 5d avg / 20d avg
    vol_ratio = None
    if len(volumes) >= 20:
        avg5  = sum(volumes[-5:]) / 5
        avg20 = sum(volumes[-20:]) / 20
        vol_ratio = round(avg5 / avg20, 2) if avg20 else None

    # Price vs EMA20
    above_ema20    = (close > ema20) if ema20 else None
    pct_from_ema20 = round((close - ema20) / ema20 * 100, 2) if ema20 else None

    # Signal classification
    signal = "neutral"
    if rsi is not None and pos_52w is not None:
        if rsi < 40 and pos_52w < 0.30:
            signal = "oversold_bounce"
        elif rsi > 70 and vol_ratio and vol_ratio > 1.5 and pct_from_ema20 and pct_from_ema20 > 5:
            signal = "distribution"
        elif rsi > 55 and macd_line and macd_signal_val and macd_line > macd_signal_val and above_ema20:
            signal = "momentum"

    return {
        "ticker":        ticker,
        "close":         round(close, 2),
        "high52w":       round(high52w, 2),
        "low52w":        round(low52w, 2),
        "pos_52w":       pos_52w,
        "rsi":           rsi,
        "ema20":         ema20,
        "ema50":         ema50,
        "macd":          macd_line,
        "macd_signal":   macd_signal_val,
        "macd_hist":     macd_hist,
        "vol_ratio":     vol_ratio,
        "above_ema20":   above_ema20,
        "pct_from_ema20": pct_from_ema20,
        "signal":        signal,
        "data_points":   len(series),
        "computed_at":   datetime.now().isoformat(),
    }


def write_technical_signal(ticker: str, indicators: dict) -> bool:
    """Upsert TechnicalSignal node for ticker."""
    try:
        from memory.graph_store import get_store
        gs = get_store()
        gs.upsert_node(
            node_type="TechnicalSignal",
            name=f"TechnicalSignal_{ticker}_{date.today()}",
            ticker=ticker,
            properties=indicators,
        )
        return True
    except Exception:
        return False


def run_ticker(ticker: str) -> dict:
    """Compute and store technical signal for one ticker. Returns status dict."""
    try:
        indicators = compute_technicals(ticker)
        if indicators is None:
            return {"ticker": ticker, "status": "skipped", "reason": "insufficient_data"}
        write_technical_signal(ticker, indicators)
        return {"ticker": ticker, "status": "ok", "signal": indicators["signal"], "rsi": indicators["rsi"]}
    except Exception as e:
        return {"ticker": ticker, "status": "error", "error": str(e)}
