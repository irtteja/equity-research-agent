"""
fetch_corp_events — pulls announcements and board meetings from NSE and stores
them as CorporateEvent nodes in the knowledge graph.

Sources:
  NSE.all_announcements()  — last N days of exchange filings
  NSE.board_meetings()     — upcoming result-related board meetings

Filters:
  - Excludes SME/Emerge companies
  - Keeps only high-signal event categories:
    Results | Dividend | Corporate_Action | Management | Other
  - Deduplicates by (symbol, normalised subject)

Outputs:
  - list[dict] of events
  - Optionally upserts CorporateEvent nodes into graph.db
"""
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.graph_store import get_store

# ── Category mapping ──────────────────────────────────────────────────────────

_RESULTS_KWORDS   = {"financial results", "quarterly results", "outcome of board",
                     "unaudited financial", "audited financial", "half year result",
                     "annual results"}
_DIVIDEND_KWORDS  = {"dividend", "bonus", "rights issue", "rights entitlement"}
_CORP_ACT_KWORDS  = {"merger", "acquisition", "demerger", "amalgamation",
                     "open offer", "buyback", "buy-back", "qip",
                     "qualified institutional", "scheme of arrangement"}
_MGMT_KWORDS      = {"appointment", "resignation", "director", "ceo", "cfo",
                     "managing director", "key managerial", "kmp"}

_PRIORITY_ANY = (
    _RESULTS_KWORDS | _DIVIDEND_KWORDS | _CORP_ACT_KWORDS | _MGMT_KWORDS
    | {"agm", "egm", "demerger", "rights"}
)


def _categorise(subject: str) -> str:
    s = subject.lower()
    if any(k in s for k in _RESULTS_KWORDS):
        return "Results"
    if any(k in s for k in _DIVIDEND_KWORDS):
        return "Dividend"
    if any(k in s for k in _CORP_ACT_KWORDS):
        return "Corporate_Action"
    if any(k in s for k in _MGMT_KWORDS):
        return "Management"
    if "agm" in s or "egm" in s:
        return "Management"
    return "Other"


def _is_sme(symbol: str, company: str) -> bool:
    co = company.lower()
    return "sme" in co or "emerge" in co or symbol.upper().endswith("SME")


def _norm_subject(s: str) -> str:
    """Normalise subject for dedup — strip trailing punctuation / whitespace."""
    return " ".join(s.lower().split())[:60]


def _short_hash(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:10]


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_announcements(days_back: int = 3) -> list[dict]:
    """
    Fetch NSE announcements for the last `days_back` days.
    Returns filtered, deduplicated list of event dicts.
    """
    from vendor.nselib.nse import NSE

    today  = date.today()
    from_dt = today - timedelta(days=days_back)

    with NSE() as nse:
        raw = nse.all_announcements(
            from_date=from_dt, to_date=today, index="equities"
        )

    seen:   set  = set()
    events: list = []

    for a in raw:
        symbol  = (a.get("SYMBOL") or "").strip().upper()
        company = (a.get("COMPANY NAME") or "").strip()
        subject = (a.get("SUBJECT") or "").strip()
        details = (a.get("DETAILS") or "").strip()
        ts      = (a.get("BROADCAST DATE/TIME") or a.get("RECEIPT") or "")

        if not symbol or not subject:
            continue
        if _is_sme(symbol, company):
            continue

        subj_lc = subject.lower()
        if not any(k in subj_lc for k in _PRIORITY_ANY):
            continue

        key = f"{symbol}:{_norm_subject(subject)}"
        if key in seen:
            continue
        seen.add(key)

        events.append({
            "symbol":   symbol,
            "company":  company,
            "subject":  subject,
            "time":     str(ts),
            "details":  details[:200],
            "category": _categorise(subject),
        })

    # Sort by time desc (most recent first)
    events.sort(key=lambda x: x["time"], reverse=True)
    return events


def fetch_board_meetings(days_ahead: int = 21) -> list[dict]:
    """
    Upcoming board meetings (result-related) in the next `days_ahead` days.
    """
    from vendor.nselib.nse import NSE

    today  = date.today()
    to_dt  = today + timedelta(days=days_ahead)

    with NSE() as nse:
        raw = nse.board_meetings(from_date=today, to_date=to_dt)

    result_kw = {"financial results", "quarterly results", "half year", "annual result"}
    meetings  = []
    seen      = set()

    for b in raw:
        symbol  = (b.get("bm_symbol") or "").strip().upper()
        purpose = (b.get("bm_purpose") or "").strip()
        desc    = (b.get("bm_desc")    or "").strip()
        dt_str  = (b.get("bm_date")    or "")
        company = (b.get("sm_name")    or "")

        if not symbol:
            continue
        if _is_sme(symbol, company):
            continue

        combined = (purpose + " " + desc).lower()
        if not any(k in combined for k in result_kw):
            continue

        key = f"{symbol}:{dt_str}"
        if key in seen:
            continue
        seen.add(key)

        meetings.append({
            "symbol":  symbol,
            "company": company,
            "date":    dt_str,
            "purpose": purpose,
            "desc":    desc[:200],
        })

    meetings.sort(key=lambda x: x["date"])
    return meetings


def _save_events_to_graph(events: list[dict], meetings: list[dict]):
    """Upsert CorporateEvent nodes into graph.db."""
    gs = get_store()

    for ev in events:
        node_id = _short_hash(f"{ev['symbol']}:{ev['time']}:{ev['subject'][:40]}")
        gs.add_node(
            node_id  = f"corpev_{node_id}",
            node_type= "CorporateEvent",
            name     = f"{ev['symbol']}:{ev['category']}:{ev['time'][:10]}",
            ticker   = ev["symbol"],
            properties = ev,
        )

    for bm in meetings:
        node_id = _short_hash(f"{bm['symbol']}:{bm['date']}")
        gs.add_node(
            node_id  = f"boardmtg_{node_id}",
            node_type= "CorporateEvent",
            name     = f"{bm['symbol']}:BoardMeeting:{bm['date']}",
            ticker   = bm["symbol"],
            properties = {**bm, "category": "BoardMeeting"},
        )


def run_all(save_to_graph: bool = True) -> dict:
    """Fetch announcements + board meetings, optionally persist to graph."""
    summary = {"announcements": 0, "board_meetings": 0, "saved": False, "error": None}
    try:
        events   = fetch_announcements(days_back=3)
        meetings = fetch_board_meetings(days_ahead=21)
        summary["announcements"]  = len(events)
        summary["board_meetings"] = len(meetings)
        if save_to_graph:
            _save_events_to_graph(events, meetings)
            summary["saved"] = True
    except Exception as e:
        summary["error"] = str(e)
    return summary


if __name__ == "__main__":
    result = run_all(save_to_graph=True)
    print(json.dumps(result, indent=2))
