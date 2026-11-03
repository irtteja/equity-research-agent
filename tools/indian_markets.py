"""
Indian equity data via nselib (NSE) and bselib (BSE).
Falls back gracefully when live NSE APIs are blocked from cloud IPs.

Bulk endpoints (bypass NSE's 20-record UI cap):
  - nse_all_announcements()        corporate-announcements with date range
  - nse_corporate_actions_bulk()   corporates-corporateActions with date range -> all records
  - nse_board_meetings_bulk()      corporate-board-meetings with date range
  - nse_financial_results_bulk()   corporates-financial-results (3800+ records, quarterly)
  - nse_integrated_filing()        integrated-filing-results per symbol (iXBRL + PDF links)
  - nse_surveillance()             ASM / GSM / rumour-verification lists
"""
import sys
import os
import csv
import io
from datetime import date, timedelta
from typing import Optional

# Make nselib importable from local clone
_NSELIB_PATH = os.path.expanduser("~/irtteja/nselib")
if _NSELIB_PATH not in sys.path:
    sys.path.insert(0, _NSELIB_PATH)

_BSE_DOWNLOAD_FOLDER = os.path.expanduser("~/.bse_cache")

# SME platform identifiers — never surface these
_NSE_SME_SERIES = {"SM", "ST"}          # NSE Emerge series codes
_BSE_SME_GROUPS = {"M ", "MT", "XM"}   # BSE SME group codes


def _is_sme_nse(row: dict) -> bool:
    return str(row.get("series", "")).strip().upper() in _NSE_SME_SERIES


def _is_sme_bse(row: dict) -> bool:
    return str(row.get("Group", "") or row.get("group", "")).strip().upper() in _BSE_SME_GROUPS


# ------------------------------------------------------------------ #
#  NSE tools (via nselib)                                             #
# ------------------------------------------------------------------ #

def nse_equity_quote(symbol: str) -> dict:
    """Live NSE quote: OHLC, LTP, volume, 52w high/low, % change for a symbol like RELIANCE."""
    try:
        from nselib import NSE
        with NSE() as nse:
            return nse.equity_quote(symbol.upper())
    except Exception as e:
        return {"error": str(e), "symbol": symbol}


def nse_price_history(symbol: str, days: int = 365) -> dict:
    """Historical daily OHLCV from NSE for a symbol. Uses bhavcopy archive as fallback (works from cloud)."""
    try:
        from nselib import NSE
        with NSE() as nse:
            rows = nse.fetch_equity_historical_data(symbol.upper(), days=days)
        if not rows:
            return {"error": f"No history found for {symbol}"}
        return {
            "symbol": symbol.upper(),
            "days_requested": days,
            "records": len(rows),
            "from": rows[0].get("date") or rows[0].get("CH_TIMESTAMP", ""),
            "to": rows[-1].get("date") or rows[-1].get("CH_TIMESTAMP", ""),
            "data": rows[-30:],  # last 30 rows inline; full data in 'records' count
        }
    except Exception as e:
        return {"error": str(e), "symbol": symbol}


def nse_option_chain(symbol: str, expiry: Optional[str] = None) -> dict:
    """NSE option chain with CE/PE data side by side. Also returns max pain strike. symbol: NIFTY, BANKNIFTY, or stock symbol."""
    try:
        from nselib import NSE
        with NSE() as nse:
            chain = nse.compile_option_chain(symbol.upper(), expiry=expiry)
            mp = nse.max_pain(symbol.upper(), expiry=expiry)
        return {
            "symbol": symbol.upper(),
            "max_pain": mp,
            "chain_rows": len(chain) if isinstance(chain, list) else None,
            "chain_sample": chain[:5] if isinstance(chain, list) else chain,
        }
    except Exception as e:
        return {"error": str(e), "symbol": symbol}


def nse_corporate_actions(symbol: str) -> dict:
    """NSE corporate actions: dividends, splits, bonuses for a symbol (past 1 year + next 90 days)."""
    try:
        from nselib import NSE
        with NSE() as nse:
            actions = nse.corporate_actions(
                symbol=symbol.upper(),
                from_date=date.today() - timedelta(days=365),
                to_date=date.today() + timedelta(days=90),
            )
        return {"symbol": symbol.upper(), "actions": actions}
    except Exception as e:
        return {"error": str(e), "symbol": symbol}


def nse_corporate_actions_bulk(from_date: str = None, to_date: str = None) -> dict:
    """
    NSE corporate actions for ALL mainboard equities in a date range (no 20-record cap).
    from_date / to_date: 'DD-MM-YYYY'. Defaults to past 30 days + next 90 days.
    Returns dividends, splits, bonuses, rights issues.
    """
    try:
        from nselib import NSE
        _fmt = lambda d: d.strftime("%d-%m-%Y")
        fd = from_date or _fmt(date.today() - timedelta(days=30))
        td = to_date or _fmt(date.today() + timedelta(days=90))
        with NSE() as nse:
            s = nse._session._s
            r = s.get(
                "https://www.nseindia.com/api/corporates-corporateActions",
                params={"index": "equities", "from_date": fd, "to_date": td, "csv": "true"},
                timeout=20,
            )
            text = r.content.decode("utf-8-sig", errors="replace")
            rows = list(csv.DictReader(io.StringIO(text)))
        return {"from": fd, "to": td, "count": len(rows), "actions": rows}
    except Exception as e:
        return {"error": str(e)}


def nse_financial_results(symbol: str, period: str = "Quarterly") -> dict:
    """NSE quarterly/annual financial results filings for a symbol. period: Quarterly or Annual."""
    try:
        from nselib import NSE
        with NSE() as nse:
            results = nse.financial_results(symbol=symbol.upper(), period=period)
        return {"symbol": symbol.upper(), "period": period, "results": results}
    except Exception as e:
        return {"error": str(e), "symbol": symbol}


def nse_financial_results_bulk(period: str = "Quarterly") -> dict:
    """
    NSE financial results for ALL mainboard equities (no 20-record cap).
    Returns ~3,800+ records covering all F&O-eligible companies.
    period: 'Quarterly' or 'Annual'.
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


def nse_all_announcements(from_date: str = None, to_date: str = None) -> dict:
    """
    NSE corporate announcements for ALL equities in a date range (no 20-record cap).
    from_date / to_date: 'DD-MM-YYYY'. Defaults to today only.
    Use days_back parameter pattern: pass from_date = (today - N).strftime('%d-%m-%Y').
    """
    try:
        from nselib import NSE
        _fmt = lambda d: d.strftime("%d-%m-%Y")
        fd = from_date or _fmt(date.today())
        td = to_date or _fmt(date.today())
        with NSE() as nse:
            s = nse._session._s
            r = s.get(
                "https://www.nseindia.com/api/corporate-announcements",
                params={"index": "equities", "from_date": fd, "to_date": td, "csv": "true"},
                timeout=20,
            )
            text = r.content.decode("utf-8-sig", errors="replace")
            rows = list(csv.DictReader(io.StringIO(text)))
        return {"from": fd, "to": td, "count": len(rows), "announcements": rows}
    except Exception as e:
        return {"error": str(e)}


def nse_board_meetings_bulk(from_date: str = None, to_date: str = None) -> dict:
    """
    NSE board meetings for ALL mainboard equities in a date range (no 20-record cap).
    from_date / to_date: 'DD-MM-YYYY'. Defaults to past 7 days + next 30 days.
    Returns upcoming and recently held board meetings, including dividend/results agenda items.
    """
    try:
        from nselib import NSE
        _fmt = lambda d: d.strftime("%d-%m-%Y")
        fd = from_date or _fmt(date.today() - timedelta(days=7))
        td = to_date or _fmt(date.today() + timedelta(days=30))
        with NSE() as nse:
            s = nse._session._s
            r = s.get(
                "https://www.nseindia.com/api/corporate-board-meetings",
                params={"index": "equities", "from_date": fd, "to_date": td, "csv": "true"},
                timeout=20,
            )
            text = r.content.decode("utf-8-sig", errors="replace")
            rows = list(csv.DictReader(io.StringIO(text)))
        return {"from": fd, "to": td, "count": len(rows), "meetings": rows}
    except Exception as e:
        return {"error": str(e)}


def nse_integrated_filing(symbol: str, from_date: str = None, to_date: str = None) -> dict:
    """
    NSE integrated filing results for a symbol — iXBRL viewer links + PDF download URLs.
    Returns INTEGRATED_FILING_INDAS (financials) and INTEGRATED_FILING_GOVERNANCE filings.
    from_date / to_date: 'DD-MM-YYYY'. Defaults to past 2 years.
    PDF URLs are from nsearchives.nseindia.com and are cloud-accessible.
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
        filings = data.get("data", []) if isinstance(data, dict) else data
        # Annotate each filing with a direct PDF URL if not already present
        enriched = []
        for f in (filings if isinstance(filings, list) else []):
            enriched.append({
                "symbol":      f.get("symbol", symbol.upper()),
                "company":     f.get("companyName", ""),
                "filing_type": f.get("filingType", ""),
                "period":      f.get("period", ""),
                "date":        f.get("sort_date", "")[:10],
                "xbrl_url":    f.get("xbrl", ""),
                "pdf_url":     f.get("pdfLink", "") or f.get("attachment", ""),
                "raw":         f,
            })
        return {"symbol": symbol.upper(), "from": fd, "to": td, "count": len(enriched), "filings": enriched}
    except Exception as e:
        return {"error": str(e), "symbol": symbol}


def nse_surveillance(list_type: str = "asm") -> dict:
    """
    NSE surveillance lists: ASM (Additional Surveillance Measure) and GSM (Graded Surveillance Measure).
    list_type: 'asm' | 'gsm' | 'all'
    Returns stocks under enhanced surveillance with their stage/criteria.
    """
    try:
        from nselib import NSE
        results = {}
        with NSE() as nse:
            if list_type in ("asm", "all"):
                try:
                    results["asm"] = nse.reportASM()
                except Exception as e:
                    results["asm_error"] = str(e)
            if list_type in ("gsm", "all"):
                try:
                    results["gsm"] = nse.reportGSM()
                except Exception as e:
                    results["gsm_error"] = str(e)
        return results
    except Exception as e:
        return {"error": str(e)}


def nse_shareholding(symbol: str) -> dict:
    """NSE shareholding pattern: promoter, FII, DII, public breakdown for a symbol."""
    try:
        from nselib import NSE
        with NSE() as nse:
            data = nse.shareholding_patterns(symbol.upper())
        return {"symbol": symbol.upper(), "shareholding": data}
    except Exception as e:
        return {"error": str(e), "symbol": symbol}


def nse_index_data(index_name: str = "NIFTY 50") -> dict:
    """Live NSE index quote and top constituents. index_name examples: 'NIFTY 50', 'NIFTY BANK', 'NIFTY IT'."""
    try:
        from nselib import NSE
        with NSE() as nse:
            quote = nse.index_quote(index_name)
            stocks = nse.list_equity_stocks_by_index(index_name)
            gainers = nse.gainers(stocks, count=5)
            losers = nse.losers(stocks, count=5)
            ad = nse.advance_decline(index_name)
        return {
            "index": index_name,
            "quote": quote,
            "advance_decline": ad,
            "top_gainers": gainers,
            "top_losers": losers,
        }
    except Exception as e:
        return {"error": str(e), "index": index_name}


def nse_lookup(query: str) -> dict:
    """Search NSE for a company by name or partial symbol. Returns matching symbols."""
    try:
        from nselib import NSE
        with NSE() as nse:
            results = nse.lookup(query)
        return {"query": query, "matches": results}
    except Exception as e:
        return {"error": str(e), "query": query}


# ------------------------------------------------------------------ #
#  BSE tools (via bselib: pip install bselib)                         #
# ------------------------------------------------------------------ #

def _bse():
    from bse import BSE
    os.makedirs(_BSE_DOWNLOAD_FOLDER, exist_ok=True)
    return BSE(download_folder=_BSE_DOWNLOAD_FOLDER)


def bse_equity_quote(scrip_code: str) -> dict:
    """BSE live quote for a scrip code (e.g. '500325' for Reliance). Returns OHLC, volume, 52w H/L."""
    try:
        bse = _bse()
        return bse.quote(scrip_code)
    except Exception as e:
        return {"error": str(e), "scrip_code": scrip_code}


def bse_weekly_hl(scrip_code: str) -> dict:
    """BSE 52-week high/low for a scrip code."""
    try:
        bse = _bse()
        return bse.quoteWeeklyHL(scrip_code)
    except Exception as e:
        return {"error": str(e), "scrip_code": scrip_code}


def bse_equity_meta(scrip_code: str) -> dict:
    """BSE equity metadata: company name, sector, ISIN, face value, market cap for a scrip code."""
    try:
        bse = _bse()
        return bse.equityMetaInfo(scrip_code)
    except Exception as e:
        return {"error": str(e), "scrip_code": scrip_code}


def bse_price_volume_12m(scrip_code: str) -> dict:
    """BSE 12-month price and volume history for a scrip code."""
    try:
        bse = _bse()
        return bse.equityPriceVolumeT12M(scrip_code)
    except Exception as e:
        return {"error": str(e), "scrip_code": scrip_code}


def bse_trading_stats(scrip_code: str, flag: str = "", quotetype: str = "EQ") -> dict:
    """BSE trading statistics for a scrip code: delivery %, circuit limits, trading stats."""
    try:
        bse = _bse()
        return bse.getScripTradingStats(scrip_code, flag=flag, quotetype=quotetype)
    except Exception as e:
        return {"error": str(e), "scrip_code": scrip_code}


def bse_advance_decline() -> dict:
    """BSE market-wide advance/decline/unchanged counts across groups."""
    try:
        bse = _bse()
        return {"data": bse.advanceDecline()}
    except Exception as e:
        return {"error": str(e)}


def bse_gainers(by: str = "all", name: str = None, pct_change: str = "all") -> dict:
    """BSE top gainers (mainboard only, no SME). by: 'group'|'index'|'all'. pct_change: 'all'|'10'|'5'|'2'|'0'."""
    try:
        bse = _bse()
        data = bse.gainers(by=by, name=name, pct_change=pct_change)
        if isinstance(data, list):
            data = [r for r in data if not _is_sme_bse(r)]
        return {"gainers": data}
    except Exception as e:
        return {"error": str(e)}


def bse_losers(by: str = "all", name: str = None, pct_change: str = "all") -> dict:
    """BSE top losers (mainboard only, no SME). by: 'group'|'index'|'all'. pct_change: 'all'|'10'|'5'|'2'|'0'."""
    try:
        bse = _bse()
        data = bse.losers(by=by, name=name, pct_change=pct_change)
        if isinstance(data, list):
            data = [r for r in data if not _is_sme_bse(r)]
        return {"losers": data}
    except Exception as e:
        return {"error": str(e)}


def bse_bhav_copy(date_str: str = None) -> dict:
    """BSE daily bhavcopy report (mainboard only, no SME). date_str format: 'DDMMYYYY'. Defaults to latest available."""
    try:
        bse = _bse()
        result = bse.bhavCopyReport(date_str) if date_str else bse.bhavCopyReport()
        if isinstance(result, list):
            result = [r for r in result if not _is_sme_bse(r)]
        return {"bhav_copy": result}
    except Exception as e:
        return {"error": str(e)}


def bse_announcements(category: str = "Board Meeting") -> dict:
    """BSE corporate announcements by category. Common categories: 'Board Meeting', 'Dividend', 'Result', 'AGM'."""
    try:
        bse = _bse()
        return {"category": category, "announcements": bse.announcements(category)}
    except Exception as e:
        return {"error": str(e)}


def bse_corporate_actions(scrip_code: str = None) -> dict:
    """BSE corporate actions: dividends, splits, bonuses. Optionally filter by scrip code."""
    try:
        bse = _bse()
        result = bse.actions(scrip_code) if scrip_code else bse.actions()
        return {"scrip_code": scrip_code, "actions": result}
    except Exception as e:
        return {"error": str(e)}


def bse_result_calendar() -> dict:
    """BSE earnings result calendar — upcoming and recent quarterly result dates."""
    try:
        bse = _bse()
        return {"result_calendar": bse.resultCalendar()}
    except Exception as e:
        return {"error": str(e)}


def bse_results_snapshot() -> dict:
    """BSE results snapshot — summary of latest declared quarterly results."""
    try:
        bse = _bse()
        return {"results_snapshot": bse.resultsSnapshot()}
    except Exception as e:
        return {"error": str(e)}


def bse_get_scrip_code(company_name: str) -> dict:
    """Look up BSE scrip code by company name."""
    try:
        bse = _bse()
        return {"company": company_name, "scrip_code": bse.getScripCode(company_name)}
    except Exception as e:
        return {"error": str(e)}


def bse_lookup(query: str) -> dict:
    """Search BSE for companies matching a name or symbol query. Returns scrip codes and names."""
    try:
        bse = _bse()
        return {"query": query, "matches": bse.lookup(query)}
    except Exception as e:
        return {"error": str(e)}
