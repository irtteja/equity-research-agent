"""
Macro monitoring: global indices, commodities, currencies, FII/DII flows, India macro.
"""
import sys, os
_NSELIB_PATH = os.path.expanduser("~/irtteja/nselib")
if _NSELIB_PATH not in sys.path:
    sys.path.insert(0, _NSELIB_PATH)

import yfinance as yf
from datetime import date, datetime


_GLOBAL_INDICES = {
    "S&P 500":      "^GSPC",
    "Nasdaq":       "^IXIC",
    "Dow Jones":    "^DJI",
    "Nikkei 225":   "^N225",
    "Hang Seng":    "^HSI",
    "DAX":          "^GDAXI",
    "FTSE 100":     "^FTSE",
    "Shanghai":     "000001.SS",
    "NIFTY 50":     "^NSEI",
    "SENSEX":       "^BSESN",
    "NIFTY BANK":   "^NSEBANK",
}

_COMMODITIES = {
    "Crude WTI":    "CL=F",
    "Crude Brent":  "BZ=F",
    "Gold":         "GC=F",
    "Silver":       "SI=F",
    "Natural Gas":  "NG=F",
    "Copper":       "HG=F",
}

_CURRENCIES = {
    "USD/INR":      "USDINR=X",
    "EUR/INR":      "EURINR=X",
    "GBP/INR":      "GBPINR=X",
    "JPY/INR":      "JPYINR=X",
    "DXY":          "DX-Y.NYB",
    "EUR/USD":      "EURUSD=X",
}


def _yf_snapshot(tickers: dict) -> dict:
    result = {}
    for name, symbol in tickers.items():
        try:
            t = yf.Ticker(symbol)
            info = t.fast_info
            hist = t.history(period="2d")
            if hist.empty:
                result[name] = {"error": "no data"}
                continue
            prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
            last = float(hist["Close"].iloc[-1])
            chg = round(last - prev_close, 2) if prev_close else None
            pct = round((chg / prev_close) * 100, 2) if prev_close and chg is not None else None
            result[name] = {
                "symbol": symbol,
                "last": round(last, 2),
                "prev_close": round(prev_close, 2) if prev_close else None,
                "change": chg,
                "change_pct": pct,
            }
        except Exception as e:
            result[name] = {"error": str(e)}
    return result


def get_global_indices() -> dict:
    """Live snapshot of major global equity indices: S&P 500, Nasdaq, Nikkei, Hang Seng, DAX, NIFTY, SENSEX etc."""
    return {"timestamp": datetime.now().isoformat(), "indices": _yf_snapshot(_GLOBAL_INDICES)}


def get_commodity_prices() -> dict:
    """Live commodity prices: Crude WTI, Crude Brent, Gold, Silver, Natural Gas, Copper."""
    return {"timestamp": datetime.now().isoformat(), "commodities": _yf_snapshot(_COMMODITIES)}


def get_currency_rates() -> dict:
    """Live currency rates: USD/INR, EUR/INR, GBP/INR, JPY/INR, DXY dollar index."""
    return {"timestamp": datetime.now().isoformat(), "currencies": _yf_snapshot(_CURRENCIES)}


def get_fii_dii_flows() -> dict:
    """NSE FII/DII trade activity — net buy/sell by foreign and domestic institutions."""
    try:
        from nselib import NSE
        with NSE() as nse:
            data = nse.fii_dii_trade_activity()
        return {"timestamp": datetime.now().isoformat(), "fii_dii": data}
    except Exception as e:
        return {"error": str(e)}


def get_block_bulk_deals() -> dict:
    """NSE block deals and bulk deals — large institutional trades reported today."""
    try:
        from nselib import NSE
        with NSE() as nse:
            blocks = nse.block_deals()
            bulks = nse.bulk_deals()
        return {
            "timestamp": datetime.now().isoformat(),
            "block_deals": blocks,
            "bulk_deals": bulks,
        }
    except Exception as e:
        return {"error": str(e)}


def get_market_status() -> dict:
    """NSE market open/closed status for all segments + today's pre-open data."""
    try:
        from nselib import NSE
        with NSE() as nse:
            status = nse.status()
            pre = nse.pre_open("ALL")
        return {"status": status, "pre_open": pre}
    except Exception as e:
        return {"error": str(e)}


def get_full_macro_snapshot() -> dict:
    """Complete macro snapshot: global indices + commodities + currencies + FII/DII. Use for daily monitoring."""
    return {
        "date": date.today().isoformat(),
        "timestamp": datetime.now().isoformat(),
        "global_indices": _yf_snapshot(_GLOBAL_INDICES),
        "commodities": _yf_snapshot(_COMMODITIES),
        "currencies": _yf_snapshot(_CURRENCIES),
    }
