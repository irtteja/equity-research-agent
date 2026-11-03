"""
BSE/NSE announcement fetcher and impact analyser.
Reads corporate announcements, classifies them, stores impact reasoning.
"""
import sys, os
_NSELIB_PATH = os.path.expanduser("~/irtteja/nselib")
if _NSELIB_PATH not in sys.path:
    sys.path.insert(0, _NSELIB_PATH)

import requests
from datetime import date, datetime, timedelta

_BSE_ANNOUNCE_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
_BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.bseindia.com/",
}

BSE_CATEGORIES = [
    "Board Meeting", "Result", "Dividend", "AGM/EGM",
    "Insider Trading / SAST", "Buyback", "Rights Issue",
    "Amalgamation/Mergers", "Bonus", "Stock Split",
    "Change in Management", "Press Release",
]


def get_bse_announcements(category: str = "Result", days_back: int = 1) -> dict:
    """
    Fetch BSE corporate announcements by category.
    category options: 'Result', 'Dividend', 'Board Meeting', 'Insider Trading / SAST',
    'Buyback', 'AGM/EGM', 'Bonus', 'Stock Split', 'Press Release', etc.
    days_back: how many past days to fetch (default 1 = today only).
    """
    try:
        from_dt = (date.today() - timedelta(days=days_back)).strftime("%Y%m%d")
        to_dt = date.today().strftime("%Y%m%d")
        params = {
            "pageno": "1",
            "strCat": category,
            "strPrevDate": from_dt,
            "strScrip": "",
            "strSearch": "P",
            "strToDate": to_dt,
            "strType": "C",
            "subcategory": "-1",
        }
        resp = requests.get(_BSE_ANNOUNCE_URL, params=params, headers=_BSE_HEADERS, timeout=15)
        data = resp.json()
        items = data.get("Table", [])
        # Filter out SME platform companies (segment = "sme" in BSE API)
        _SME_SEGMENTS = {"sme", "bse sme", "emerge"}
        filtered = [
            i for i in items
            if str(i.get("Segment", "") or i.get("segment", "")).strip().lower() not in _SME_SEGMENTS
        ]
        return {
            "category": category,
            "from": from_dt,
            "to": to_dt,
            "count": len(filtered),
            "announcements": [
                {
                    "scrip_code": str(i.get("SCRIP_CD", "")),
                    "company": i.get("SLONGNAME", ""),
                    "headline": i.get("HEADLINE", ""),
                    "category": i.get("CATEGORYNAME", ""),
                    "subcategory": i.get("SUBCATNAME", ""),
                    "date": i.get("News_submission_dt", ""),
                    "attachment": i.get("ATTACHMENTNAME", ""),
                }
                for i in filtered
            ],
        }
    except Exception as e:
        return {"error": str(e), "category": category}


def get_nse_corporate_announcements(symbol: str = None, days_back: int = 7) -> dict:
    """
    Fetch NSE corporate announcements.
    - With symbol: per-company announcements (past days_back days).
    - Without symbol: market-wide bulk pull (all equities, no 20-record cap).
    days_back applies to market-wide pull; per-symbol always returns up to 180 days.
    """
    import csv, io
    try:
        from nselib import NSE
        _fmt = lambda d: d.strftime("%d-%m-%Y")
        with NSE() as nse:
            if symbol:
                # Per-symbol: use nselib's announcements() for full history
                rows = nse.announcements(
                    symbol=symbol.upper(),
                    from_date=date.today() - timedelta(days=180),
                    to_date=date.today(),
                )
                results = nse.financial_results(symbol=symbol.upper())
                actions = nse.corporate_actions(
                    symbol=symbol.upper(),
                    from_date=date.today() - timedelta(days=365),
                    to_date=date.today() + timedelta(days=90),
                )
                return {
                    "symbol": symbol.upper(),
                    "announcements": rows,
                    "financial_results": results,
                    "corporate_actions": actions,
                }
            else:
                # Market-wide bulk: bypass 20-record cap with date-range CSV endpoints
                fd = _fmt(date.today() - timedelta(days=days_back))
                td = _fmt(date.today())
                s = nse._session._s

                def _fetch_csv(url, params):
                    r = s.get(url, params=params, timeout=20)
                    text = r.content.decode("utf-8-sig", errors="replace")
                    return list(csv.DictReader(io.StringIO(text)))

                announcements = _fetch_csv(
                    "https://www.nseindia.com/api/corporate-announcements",
                    {"index": "equities", "from_date": fd, "to_date": td, "csv": "true"},
                )
                actions = _fetch_csv(
                    "https://www.nseindia.com/api/corporates-corporateActions",
                    {"index": "equities", "from_date": fd, "to_date": td, "csv": "true"},
                )
                meetings = _fetch_csv(
                    "https://www.nseindia.com/api/corporate-board-meetings",
                    {"index": "equities", "from_date": fd, "to_date": td, "csv": "true"},
                )
                return {
                    "symbol": None,
                    "from": fd,
                    "to": td,
                    "announcements": {"count": len(announcements), "data": announcements},
                    "corporate_actions": {"count": len(actions), "data": actions},
                    "board_meetings": {"count": len(meetings), "data": meetings},
                }
    except Exception as e:
        return {"error": str(e)}


def get_all_bse_announcements_today() -> dict:
    """
    Pull BSE announcements across all major categories for today.
    Good for daily morning scan — returns everything filed since yesterday.
    """
    all_items = []
    errors = []
    for cat in ["Result", "Board Meeting", "Dividend", "Insider Trading / SAST",
                "Buyback", "Bonus", "Stock Split", "Press Release"]:
        try:
            result = get_bse_announcements(category=cat, days_back=1)
            for item in result.get("announcements", []):
                item["category"] = cat
                all_items.append(item)
        except Exception as e:
            errors.append(f"{cat}: {e}")

    return {
        "date": date.today().isoformat(),
        "total": len(all_items),
        "announcements": sorted(all_items, key=lambda x: x.get("date", ""), reverse=True),
        "errors": errors,
    }


def get_concall_transcripts(symbol: str, days_back: int = 180) -> dict:
    """
    Fetch NSE concall/analyst-meet filings for a symbol.
    Returns announcements with category 'Analysts/Institutional Investor Meet/Con. Call Updates',
    each carrying attchmntFile (PDF URL of the transcript or investor presentation).

    Use this to discover available concall PDFs before feeding them to the document swarm.
    days_back: how far back to search (default 180 = ~2 quarters).
    """
    try:
        from nselib import NSE
        with NSE() as nse:
            rows = nse.announcements(
                symbol=symbol.upper(),
                from_date=date.today() - timedelta(days=days_back),
                to_date=date.today(),
                category="Analysts/Institutional Investor Meet/Con. Call Updates",
            )
        filings = [
            {
                "symbol":       r.get("symbol", ""),
                "company":      r.get("sm_name", ""),
                "date":         r.get("sort_date", "")[:10],
                "headline":     r.get("attchmntText", "")[:200],
                "pdf_url":      r.get("attchmntFile", ""),
                "file_size":    r.get("attFileSize", ""),
                "seq_id":       r.get("seq_id", ""),
            }
            for r in rows
            if r.get("attchmntFile", "").endswith(".pdf")
        ]
        return {
            "symbol":   symbol.upper(),
            "days_back": days_back,
            "count":    len(filings),
            "filings":  filings,
        }
    except Exception as e:
        return {"error": str(e), "symbol": symbol}


def get_insider_trades(days_back: int = 7) -> dict:
    """BSE insider trading disclosures (SAST/PIT) for the past N days."""
    return get_bse_announcements(category="Insider Trading / SAST", days_back=days_back)


def get_upcoming_results_calendar() -> dict:
    """BSE result calendar — companies scheduled to announce quarterly results."""
    try:
        from bse import BSE
        bse = BSE(download_folder=os.path.expanduser("~/.bse_cache"))
        return {"result_calendar": bse.resultCalendar()}
    except Exception as e:
        return {"error": str(e)}


def get_nse_financial_results_bulk(period: str = "Quarterly") -> dict:
    """
    NSE financial results for ALL mainboard equities — no 20-record cap.
    Returns ~3,800+ records from the F&O-eligible universe.
    period: 'Quarterly' or 'Annual'.
    """
    import csv, io
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


def get_nse_integrated_filing(symbol: str, from_date: str = None, to_date: str = None) -> dict:
    """
    NSE integrated filing results per company — returns iXBRL viewer URLs + PDF download links.
    Filing types: INTEGRATED_FILING_INDAS (financials), INTEGRATED_FILING_GOVERNANCE.
    PDF URLs are from nsearchives.nseindia.com and are publicly accessible.
    from_date / to_date: 'DD-MM-YYYY'. Defaults to past 2 years.
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
        enriched = [
            {
                "symbol":      f.get("symbol", symbol.upper()),
                "company":     f.get("companyName", ""),
                "filing_type": f.get("filingType", ""),
                "period":      f.get("period", ""),
                "date":        f.get("sort_date", "")[:10],
                "xbrl_url":    f.get("xbrl", ""),
                "pdf_url":     f.get("pdfLink", "") or f.get("attachment", ""),
            }
            for f in filings
        ]
        return {"symbol": symbol.upper(), "from": fd, "to": td, "count": len(enriched), "filings": enriched}
    except Exception as e:
        return {"error": str(e), "symbol": symbol}
