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
        return {
            "category": category,
            "from": from_dt,
            "to": to_dt,
            "count": len(items),
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
                for i in items
            ],
        }
    except Exception as e:
        return {"error": str(e), "category": category}


def get_nse_corporate_announcements(symbol: str = None) -> dict:
    """
    Fetch NSE corporate announcements. Optionally filter by symbol (e.g. 'RELIANCE').
    Returns board meetings, results, dividends, and other filings.
    """
    try:
        from nselib import NSE
        with NSE() as nse:
            results = nse.financial_results(symbol=symbol) if symbol else nse.financial_results()
            actions = nse.corporate_actions(
                symbol=symbol,
                from_date=date.today() - timedelta(days=30),
                to_date=date.today() + timedelta(days=60),
            ) if symbol else nse.corporate_actions(
                from_date=date.today() - timedelta(days=7),
                to_date=date.today() + timedelta(days=60),
            )
        return {
            "symbol": symbol,
            "financial_results": results,
            "corporate_actions": actions,
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


def get_insider_trades(days_back: int = 7) -> dict:
    """BSE insider trading disclosures (SAST/PIT) for the past N days."""
    return get_bse_announcements(category="Insider Trading / SAST", days_back=days_back)


def get_upcoming_results_calendar() -> dict:
    """BSE result calendar — companies scheduled to announce quarterly results."""
    try:
        from bselib.bse import BSE
        bse = BSE(download_folder=os.path.expanduser("~/.bselib_cache"))
        return {"result_calendar": bse.resultCalendar()}
    except Exception as e:
        return {"error": str(e)}
