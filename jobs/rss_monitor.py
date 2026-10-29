#!/usr/bin/env python3
"""
NSE RSS announcement monitor.

Fetches https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml
(refreshes every 5 min per NSE), deduplicates against last-seen state,
logs new items, and flags material categories for attention.

Designed to run every 5 minutes as a scheduled task.
State persists in memory_store/rss_state/state.json so no announcement
is double-counted across runs.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.expanduser("~/irtteja/nselib"))

import json
import xml.etree.ElementTree as ET
from datetime import datetime, date
from pathlib import Path

REPO_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RSS_URL   = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
STATE_DIR = os.path.join(REPO_DIR, "memory_store", "rss_state")
LOG_DIR   = os.path.join(REPO_DIR, "memory_store", "rss_feed")

# Announcements with these categories always surface in the alert output
MATERIAL_CATEGORIES = {
    "financial results", "results", "board meeting", "dividend",
    "merger", "amalgamation", "buyback", "open offer", "rights issue",
    "bonus", "stock split", "delisting", "acquisition", "scheme of arrangement",
    "change in management", "press release",
}


def _parse_category(description: str) -> str:
    """Extract |SUBJECT: <category> embedded in RSS description field."""
    if not description:
        return "unknown"
    marker = "|SUBJECT:"
    idx = description.find(marker)
    if idx == -1:
        return "unknown"
    raw = description[idx + len(marker):].strip()
    # subject ends at next | or end of string
    end = raw.find("|")
    return raw[:end].strip() if end != -1 else raw.strip()


def _load_state() -> set:
    """Load set of already-seen item GUIDs."""
    path = os.path.join(STATE_DIR, "state.json")
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        data = json.load(f)
    return set(data.get("seen_guids", []))


def _save_state(seen: set):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "state.json")
    # Keep only last 5,000 GUIDs to prevent unbounded growth
    trimmed = list(seen)[-5000:]
    with open(path, "w") as f:
        json.dump({"seen_guids": trimmed, "updated": datetime.now().isoformat()}, f)


def _append_log(items: list[dict]):
    """Append new items to today's JSONL log."""
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"{date.today().isoformat()}.jsonl")
    with open(path, "a") as f:
        for item in items:
            f.write(json.dumps(item, default=str) + "\n")


def fetch_rss(nse_session) -> list[dict]:
    """Fetch and parse the RSS feed. Returns list of item dicts."""
    resp = nse_session._s.get(RSS_URL, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for el in root.findall(".//item"):
        guid     = el.findtext("guid") or el.findtext("link") or ""
        title    = el.findtext("title") or ""
        desc     = el.findtext("description") or ""
        pub_date = el.findtext("pubDate") or ""
        link     = el.findtext("link") or ""
        category = _parse_category(desc)
        # symbol is usually the first word of the title
        symbol   = title.split()[0] if title else ""
        items.append({
            "guid":     guid,
            "symbol":   symbol,
            "title":    title,
            "category": category,
            "pub_date": pub_date,
            "link":     link,
        })
    return items


def run(verbose: bool = True) -> dict:
    from nselib import NSE
    nse = NSE(os.path.expanduser("~/.nse_cache"))

    seen = _load_state()

    try:
        all_items = fetch_rss(nse._session)
    except Exception as e:
        print(f"[rss_monitor] ERROR fetching RSS: {e}")
        return {"error": str(e)}

    new_items = [it for it in all_items if it["guid"] not in seen]

    if not new_items:
        if verbose:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[rss_monitor] {ts} — no new items (feed has {len(all_items)} total)")
        return {"new": 0, "total_in_feed": len(all_items)}

    # Update state
    seen.update(it["guid"] for it in new_items)
    _save_state(seen)

    # Persist new items to log
    _append_log(new_items)

    # Classify
    material = [
        it for it in new_items
        if it["category"].lower() in MATERIAL_CATEGORIES
    ]

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[rss_monitor] {ts} — {len(new_items)} new announcements "
          f"({len(material)} material), {len(all_items)} in feed")

    if material and verbose:
        print("  MATERIAL:")
        for it in material:
            print(f"    {it['symbol']:15s}  {it['category']:30s}  {it['pub_date']}")

    return {
        "new":            len(new_items),
        "material":       len(material),
        "total_in_feed":  len(all_items),
        "material_items": material,
    }


if __name__ == "__main__":
    result = run(verbose=True)
