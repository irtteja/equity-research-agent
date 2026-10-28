import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta


def get_price_history(ticker: str, period: str = "1y") -> dict:
    """Fetch OHLCV price history for a ticker. period: 1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max"""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)
        if hist.empty:
            return {"error": f"No price data found for {ticker}"}
        hist = hist.round(2)
        recent = hist.tail(30)
        return {
            "ticker": ticker,
            "period": period,
            "start": str(hist.index[0].date()),
            "end": str(hist.index[-1].date()),
            "current_price": float(hist["Close"].iloc[-1]),
            "price_change_pct": round(
                ((hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0]) * 100, 2
            ),
            "high_52w": float(hist["High"].max()),
            "low_52w": float(hist["Low"].min()),
            "avg_volume": int(hist["Volume"].mean()),
            "recent_30d": recent[["Close", "Volume"]].to_dict(),
        }
    except Exception as e:
        return {"error": str(e)}


def get_current_quote(ticker: str) -> dict:
    """Get real-time quote and basic info for a ticker."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return {
            "ticker": ticker,
            "name": info.get("longName", ""),
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "country": info.get("country", ""),
            "exchange": info.get("exchange", ""),
            "currency": info.get("currency", ""),
            "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "pb_ratio": info.get("priceToBook"),
            "dividend_yield": info.get("dividendYield"),
            "beta": info.get("beta"),
            "description": (info.get("longBusinessSummary", "") or "")[:500],
        }
    except Exception as e:
        return {"error": str(e)}
