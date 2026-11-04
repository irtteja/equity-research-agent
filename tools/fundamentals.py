"""
Fundamental data tools: yfinance (global) + NSE-native (Indian equities).

NSE-native functions avoid the 20-record UI cap by using bulk CSV or per-symbol JSON endpoints:
  - nse_financial_statements()   iXBRL-sourced financials per company via integrated-filing-results
  - nse_quarterly_results_all()  bulk quarterly results (~3800+ records) via corporates-financial-results
  - nse_shareholding()           shareholding pattern via nselib
"""
import sys, os, csv, io
from datetime import date, timedelta

_NSELIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor")
if not os.path.isdir(os.path.join(_NSELIB_PATH, "nselib")):
    _NSELIB_PATH = os.path.expanduser("~/irtteja/nselib")
if _NSELIB_PATH not in sys.path:
    sys.path.insert(0, _NSELIB_PATH)

import yfinance as yf


def get_financial_statements(ticker: str) -> dict:
    """Fetch income statement, balance sheet, and cash flow for a ticker."""
    try:
        stock = yf.Ticker(ticker)
        result = {}

        income = stock.financials
        if not income.empty:
            result["income_statement"] = income.iloc[:, :4].to_dict()

        balance = stock.balance_sheet
        if not balance.empty:
            result["balance_sheet"] = balance.iloc[:, :4].to_dict()

        cashflow = stock.cashflow
        if not cashflow.empty:
            result["cash_flow"] = cashflow.iloc[:, :4].to_dict()

        if not result:
            return {"error": f"No financial data for {ticker}"}
        return {"ticker": ticker, **result}
    except Exception as e:
        return {"error": str(e)}


def get_key_ratios(ticker: str) -> dict:
    """Get key financial ratios and metrics."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return {
            "ticker": ticker,
            "valuation": {
                "pe_trailing": info.get("trailingPE"),
                "pe_forward": info.get("forwardPE"),
                "peg_ratio": info.get("pegRatio"),
                "price_to_book": info.get("priceToBook"),
                "price_to_sales": info.get("priceToSalesTrailing12Months"),
                "ev_to_ebitda": info.get("enterpriseToEbitda"),
                "ev_to_revenue": info.get("enterpriseToRevenue"),
            },
            "profitability": {
                "gross_margin": info.get("grossMargins"),
                "operating_margin": info.get("operatingMargins"),
                "profit_margin": info.get("profitMargins"),
                "roe": info.get("returnOnEquity"),
                "roa": info.get("returnOnAssets"),
            },
            "growth": {
                "revenue_growth": info.get("revenueGrowth"),
                "earnings_growth": info.get("earningsGrowth"),
                "earnings_quarterly_growth": info.get("earningsQuarterlyGrowth"),
            },
            "financial_health": {
                "current_ratio": info.get("currentRatio"),
                "debt_to_equity": info.get("debtToEquity"),
                "quick_ratio": info.get("quickRatio"),
                "total_cash": info.get("totalCash"),
                "total_debt": info.get("totalDebt"),
                "free_cashflow": info.get("freeCashflow"),
            },
        }
    except Exception as e:
        return {"error": str(e), "ticker": ticker}


# ------------------------------------------------------------------ #
#  NSE-native fundamental tools                                       #
# ------------------------------------------------------------------ #

def nse_financial_statements(symbol: str, from_date: str = None, to_date: str = None) -> dict:
    """
    NSE iXBRL-sourced financial statements for a company.
    Uses integrated-filing-results API — returns INTEGRATED_FILING_INDAS filings with
    xbrl_url (interactive viewer) and pdf_url (downloadable PDF from nsearchives).
    from_date / to_date: 'DD-MM-YYYY'. Defaults to past 2 years.
    ticker format: NSE symbol e.g. 'RELIANCE', 'TCS', 'HDFCBANK'.
    """
    try:
        from nselib import NSE
        _fmt = lambda d: d.strftime("%d-%m-%Y")
        fd = from_date or _fmt(date.today() - timedelta(days=730))
        td = to_date or _fmt(date.today())
        with NSE() as nse:
            s = nse._session._s
            r = s.get(
                "https://www.nseindia.com/api/integrated-filing-results",
                params={"symbol": symbol.upper(), "index": "equities", "from_date": fd, "to_date": td},
                timeout=20,
            )
            data = r.json()
        filings = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        financial = [f for f in filings if "INDAS" in f.get("filingType", "").upper()
                     or "FINANCIAL" in f.get("filingType", "").upper()]
        return {
            "symbol": symbol.upper(),
            "from": fd,
            "to": td,
            "total_filings": len(filings),
            "financial_filings": len(financial),
            "filings": [
                {
                    "filing_type": f.get("filingType", ""),
                    "period":      f.get("period", ""),
                    "date":        f.get("sort_date", "")[:10],
                    "xbrl_url":    f.get("xbrl", ""),
                    "pdf_url":     f.get("pdfLink", "") or f.get("attachment", ""),
                }
                for f in financial
            ],
        }
    except Exception as e:
        return {"error": str(e), "symbol": symbol}


def nse_quarterly_results_all(period: str = "Quarterly") -> dict:
    """
    NSE financial results for ALL mainboard equities — no 20-record cap.
    Returns ~3,800+ records from the F&O-eligible universe with result dates and PDF links.
    period: 'Quarterly' or 'Annual'.
    Use this for market-wide earnings scans (beat/miss analysis, revision momentum).
    """
    try:
        from nselib import NSE
        with NSE() as nse:
            s = nse._session._s
            r = s.get(
                "https://www.nseindia.com/api/corporates-financial-results",
                params={"index": "equities", "fo_sec": "true", "period": period, "csv": "true"},
                timeout=30,
            )
            text = r.content.decode("utf-8-sig", errors="replace")
            rows = list(csv.DictReader(io.StringIO(text)))
        return {"period": period, "count": len(rows), "results": rows}
    except Exception as e:
        return {"error": str(e)}


def nse_shareholding(symbol: str) -> dict:
    """
    NSE shareholding pattern for a company: promoter, FII, DII, public breakdown.
    Returns last 4–8 quarters of pattern history.
    """
    try:
        from nselib import NSE
        with NSE() as nse:
            data = nse.shareholding_patterns(symbol.upper())
        return {"symbol": symbol.upper(), "shareholding": data}
    except Exception as e:
        return {"error": str(e), "symbol": symbol}
