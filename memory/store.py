"""
GitHub-based persistent fact store.
Primary source of truth: JSONL files committed to this repo.

Structure:
  memory_store/companies/{TICKER}/facts/{YYYY-MM}.jsonl
  memory_store/companies/{TICKER}/triage/{YYYY-MM-DD}.json
  memory_store/events/{EVENT_ID}.json
  memory_store/index/companies.json
  memory_store/index/contradictions.json
"""
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_STORE_ROOT = _REPO_ROOT / "memory_store"


def _ensure(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ─── Facts ────────────────────────────────────────────────────────────────────

def save_facts(ticker: str, facts: list[dict], month: str = None) -> dict:
    """
    Append a batch of facts to the company's monthly JSONL file.
    month: 'YYYY-MM' — defaults to current month.
    Returns: { "saved": N, "file": path }
    """
    if not facts:
        return {"saved": 0}
    month = month or date.today().strftime("%Y-%m")
    path = _ensure(_STORE_ROOT / "companies" / ticker / "facts" / f"{month}.jsonl")
    with open(path, "a") as f:
        for fact in facts:
            f.write(json.dumps(fact, default=str) + "\n")
    return {"saved": len(facts), "file": str(path.relative_to(_REPO_ROOT))}


def load_facts(ticker: str, months: list[str] = None) -> list[dict]:
    """
    Load facts for a ticker. months: list of 'YYYY-MM'. None = all available.
    """
    base = _STORE_ROOT / "companies" / ticker / "facts"
    if not base.exists():
        return []
    files = sorted(base.glob("*.jsonl"))
    if months:
        files = [f for f in files if f.stem in months]
    facts = []
    for fpath in files:
        with open(fpath) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        facts.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return facts


def get_recent_facts(ticker: str, n_months: int = 3) -> list[dict]:
    """Return facts from the last n_months months."""
    today = date.today()
    months = []
    year, month = today.year, today.month
    for _ in range(n_months):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return load_facts(ticker, months)


# ─── Triage log ──────────────────────────────────────────────────────────────

def save_triage_log(ticker: str, entries: list[dict], day: str = None) -> dict:
    """Save triage decisions for a day. day: 'YYYY-MM-DD'."""
    day = day or date.today().isoformat()
    path = _ensure(_STORE_ROOT / "companies" / ticker / "triage" / f"{day}.json")
    existing = []
    if path.exists():
        try:
            with open(path) as f:
                existing = json.load(f)
        except Exception:
            pass
    existing.extend(entries)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2, default=str)
    return {"saved": len(entries), "file": str(path.relative_to(_REPO_ROOT))}


# ─── Company profile ─────────────────────────────────────────────────────────

def get_profile(ticker: str) -> dict:
    """
    Load the current state of a company before ingestion.
    Returns tracked metrics, recent facts, last values, active events.
    """
    facts = get_recent_facts(ticker, n_months=6)
    if not facts:
        return {
            "ticker": ticker,
            "facts_found": 0,
            "tracked_metrics": [],
            "last_values": {},
            "active_events": _get_active_events(),
            "unresolved_contradictions": [],
        }

    # Derive tracked metrics and last known values
    metrics: dict[str, Any] = {}
    for fact in facts:
        raw = fact.get("raw", fact)
        metric = raw.get("metric") or raw.get("metric_name")
        if not metric:
            continue
        period = raw.get("period") or raw.get("period_date", "")
        if metric not in metrics or period > metrics[metric].get("period", ""):
            metrics[metric] = {
                "metric": metric,
                "period": period,
                "value": raw.get("value"),
                "unit": raw.get("unit"),
                "fact_id": fact.get("fact_id", ""),
            }

    return {
        "ticker": ticker,
        "facts_found": len(facts),
        "tracked_metrics": list(metrics.keys()),
        "last_values": metrics,
        "active_events": _get_active_events(),
        "unresolved_contradictions": _get_contradictions(ticker),
    }


# ─── Events ──────────────────────────────────────────────────────────────────

def save_event(event: dict) -> dict:
    event_id = event.get("event_id", f"EVT-{date.today().isoformat()}")
    path = _ensure(_STORE_ROOT / "events" / f"{event_id}.json")
    with open(path, "w") as f:
        json.dump(event, f, indent=2, default=str)
    _update_event_index()
    return {"saved": event_id, "file": str(path.relative_to(_REPO_ROOT))}


def _get_active_events() -> list[dict]:
    events_dir = _STORE_ROOT / "events"
    if not events_dir.exists():
        return []
    active = []
    for fpath in sorted(events_dir.glob("*.json")):
        try:
            with open(fpath) as f:
                ev = json.load(f)
            if ev.get("status") not in ("closed",):
                active.append(ev)
        except Exception:
            pass
    return active


def _update_event_index():
    events = _get_active_events()
    path = _ensure(_STORE_ROOT / "index" / "events.json")
    with open(path, "w") as f:
        json.dump({"updated": date.today().isoformat(), "events": events}, f, indent=2, default=str)


# ─── Contradictions ──────────────────────────────────────────────────────────

def save_contradiction(contradiction: dict) -> dict:
    path = _ensure(_STORE_ROOT / "index" / "contradictions.json")
    existing = {"contradictions": []}
    if path.exists():
        try:
            with open(path) as f:
                existing = json.load(f)
        except Exception:
            pass
    existing["contradictions"].append(contradiction)
    existing["updated"] = date.today().isoformat()
    with open(path, "w") as f:
        json.dump(existing, f, indent=2, default=str)
    return {"saved": contradiction.get("contradiction_id", "?")}


def _get_contradictions(ticker: str = None) -> list[dict]:
    path = _STORE_ROOT / "index" / "contradictions.json"
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        items = data.get("contradictions", [])
        if ticker:
            items = [c for c in items if ticker in (c.get("tickers") or [])]
        return [c for c in items if c.get("resolution_status") != "resolved"]
    except Exception:
        return []


# ─── Company index ────────────────────────────────────────────────────────────

def update_company_index(ticker: str, metadata: dict = None):
    path = _ensure(_STORE_ROOT / "index" / "companies.json")
    companies = {}
    if path.exists():
        try:
            with open(path) as f:
                companies = json.load(f)
        except Exception:
            pass
    companies[ticker] = {
        **(companies.get(ticker) or {}),
        **(metadata or {}),
        "last_updated": date.today().isoformat(),
    }
    with open(path, "w") as f:
        json.dump(companies, f, indent=2, default=str)


def list_tracked_companies() -> list[str]:
    path = _STORE_ROOT / "index" / "companies.json"
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return list(json.load(f).keys())
    except Exception:
        return []
