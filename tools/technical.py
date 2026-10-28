import yfinance as yf
import pandas as pd
import ta


def run_technical_analysis(ticker: str, period: str = "6mo") -> dict:
    """Run technical analysis: moving averages, RSI, MACD, Bollinger Bands, support/resistance."""
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period=period)
        if df.empty or len(df) < 30:
            return {"error": f"Insufficient price data for {ticker}"}

        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        volume = df["Volume"]

        # Moving averages
        sma20 = ta.trend.sma_indicator(close, window=20)
        sma50 = ta.trend.sma_indicator(close, window=50)
        ema20 = ta.trend.ema_indicator(close, window=20)

        # RSI
        rsi = ta.momentum.rsi(close, window=14)

        # MACD
        macd = ta.trend.macd(close)
        macd_signal = ta.trend.macd_signal(close)
        macd_hist = ta.trend.macd_diff(close)

        # Bollinger Bands
        bb = ta.volatility.BollingerBands(close, window=20)

        # ATR (volatility)
        atr = ta.volatility.average_true_range(high, low, close)

        current = float(close.iloc[-1])

        def _f(series):
            v = series.iloc[-1]
            return round(float(v), 2) if pd.notna(v) else None

        # Trend signal
        trend = "neutral"
        if current > _f(sma50) and _f(sma20) > _f(sma50):
            trend = "bullish"
        elif current < _f(sma50) and _f(sma20) < _f(sma50):
            trend = "bearish"

        return {
            "ticker": ticker,
            "current_price": current,
            "trend": trend,
            "moving_averages": {
                "sma_20": _f(sma20),
                "sma_50": _f(sma50),
                "ema_20": _f(ema20),
                "price_vs_sma20_pct": round((current / _f(sma20) - 1) * 100, 2) if _f(sma20) else None,
                "price_vs_sma50_pct": round((current / _f(sma50) - 1) * 100, 2) if _f(sma50) else None,
            },
            "momentum": {
                "rsi_14": _f(rsi),
                "rsi_signal": "overbought" if _f(rsi) and _f(rsi) > 70 else ("oversold" if _f(rsi) and _f(rsi) < 30 else "neutral"),
                "macd": _f(macd),
                "macd_signal": _f(macd_signal),
                "macd_histogram": _f(macd_hist),
                "macd_crossover": "bullish" if _f(macd_hist) and _f(macd_hist) > 0 else "bearish",
            },
            "volatility": {
                "bb_upper": _f(bb.bollinger_hband()),
                "bb_middle": _f(bb.bollinger_mavg()),
                "bb_lower": _f(bb.bollinger_lband()),
                "bb_position": round((current - _f(bb.bollinger_lband())) / (_f(bb.bollinger_hband()) - _f(bb.bollinger_lband())) * 100, 1) if _f(bb.bollinger_hband()) and _f(bb.bollinger_lband()) else None,
                "atr_14": _f(atr),
            },
        }
    except Exception as e:
        return {"error": str(e)}
