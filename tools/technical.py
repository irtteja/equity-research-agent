import yfinance as yf
import pandas as pd
import pandas_ta as ta


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

        def _f(series):
            v = series.iloc[-1]
            return round(float(v), 2) if pd.notna(v) else None

        current = float(close.iloc[-1])

        # Moving averages
        sma20 = close.rolling(20).mean()
        sma50 = close.rolling(50).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()

        # RSI
        rsi = ta.rsi(close, length=14)

        # MACD
        macd_df = ta.macd(close)
        macd_val = _f(macd_df["MACD_12_26_9"]) if macd_df is not None else None
        macd_sig = _f(macd_df["MACDs_12_26_9"]) if macd_df is not None else None
        macd_hist = _f(macd_df["MACDh_12_26_9"]) if macd_df is not None else None

        # Bollinger Bands
        bb_df = ta.bbands(close, length=20)
        bb_upper = _f(bb_df["BBU_20_2.0"]) if bb_df is not None else None
        bb_mid = _f(bb_df["BBM_20_2.0"]) if bb_df is not None else None
        bb_lower = _f(bb_df["BBL_20_2.0"]) if bb_df is not None else None

        # ATR
        atr = ta.atr(high, low, close, length=14)

        # Trend signal
        s20, s50 = _f(sma20), _f(sma50)
        trend = "neutral"
        if s20 and s50:
            if current > s50 and s20 > s50:
                trend = "bullish"
            elif current < s50 and s20 < s50:
                trend = "bearish"

        bb_pos = None
        if bb_upper and bb_lower and bb_upper != bb_lower:
            bb_pos = round((current - bb_lower) / (bb_upper - bb_lower) * 100, 1)

        return {
            "ticker": ticker,
            "current_price": current,
            "trend": trend,
            "moving_averages": {
                "sma_20": s20,
                "sma_50": s50,
                "ema_20": _f(ema20),
                "price_vs_sma20_pct": round((current / s20 - 1) * 100, 2) if s20 else None,
                "price_vs_sma50_pct": round((current / s50 - 1) * 100, 2) if s50 else None,
            },
            "momentum": {
                "rsi_14": _f(rsi),
                "rsi_signal": "overbought" if rsi is not None and _f(rsi) and _f(rsi) > 70 else ("oversold" if rsi is not None and _f(rsi) and _f(rsi) < 30 else "neutral"),
                "macd": macd_val,
                "macd_signal": macd_sig,
                "macd_histogram": macd_hist,
                "macd_crossover": "bullish" if macd_hist and macd_hist > 0 else "bearish",
            },
            "volatility": {
                "bb_upper": bb_upper,
                "bb_middle": bb_mid,
                "bb_lower": bb_lower,
                "bb_position": bb_pos,
                "atr_14": _f(atr) if atr is not None else None,
            },
        }
    except Exception as e:
        return {"error": str(e)}
