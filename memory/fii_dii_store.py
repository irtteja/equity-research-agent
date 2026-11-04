"""
FII/DII flow time-series store.

Layout:
  memory_store/fii_dii/YYYY-MM.jsonl  — one line per day's FII or DII record
  memory_store/fii_dii/_index.json    — {YYYY-MM-DD: {fii_net, dii_net}}

Each line:
  {"date": "YYYY-MM-DD", "category": "FII"|"DII"|"PRO"|"CLIENT",
   "buy": float, "sell": float, "net": float, "source": str}
"""
import json
from datetime import date
from pathlib import Path

STORE_DIR = Path(__file__).parent.parent / "memory_store" / "fii_dii"


def _month_path(dt: date) -> Path:
    return STORE_DIR / f"{dt.strftime('%Y-%m')}.jsonl"


def _index_path() -> Path:
    return STORE_DIR / "_index.json"


def _load_index() -> dict:
    p = _index_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_flows(flows: list[dict], trade_date: date | None = None) -> dict:
    """
    Save FII/DII flow records. Each dict must have: category, buy, sell, net.
    Idempotent — skips records with the same (date, category) already stored.
    Returns {"saved": n, "skipped": n}.
    """
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    dt = trade_date or date.today()
    date_str = dt.isoformat()
    path = _month_path(dt)

    existing_keys: set[str] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                existing_keys.add(f"{rec.get('date')}-{rec.get('category')}")
            except Exception:
                pass

    saved, skipped = 0, 0
    new_records = []
    for flow in flows:
        cat = str(flow.get("category", "")).upper()
        key = f"{date_str}-{cat}"
        if key in existing_keys:
            skipped += 1
            continue
        record = {
            "date":     date_str,
            "category": cat,
            "buy":      _to_float(flow.get("buy") or flow.get("buyValue")),
            "sell":     _to_float(flow.get("sell") or flow.get("sellValue")),
            "net":      _to_float(flow.get("net") or flow.get("netValue")),
            "source":   str(flow.get("source", "nse")),
        }
        new_records.append(record)
        saved += 1

    if new_records:
        with open(path, "a") as f:
            for rec in new_records:
                f.write(json.dumps(rec) + "\n")

        # Update index with FII + DII net for quick dashboard reads
        idx = _load_index()
        day_entry = idx.get(date_str, {})
        for rec in new_records:
            if rec["category"] == "FII":
                day_entry["fii_net"] = rec["net"]
                day_entry["fii_buy"] = rec["buy"]
                day_entry["fii_sell"] = rec["sell"]
            elif rec["category"] == "DII":
                day_entry["dii_net"] = rec["net"]
                day_entry["dii_buy"] = rec["buy"]
                day_entry["dii_sell"] = rec["sell"]
        idx[date_str] = day_entry
        _index_path().write_text(json.dumps(idx, indent=2))

    return {"saved": saved, "skipped": skipped}


def get_flows(start_date: str = "", end_date: str = "", category: str = "") -> list[dict]:
    """Return flow records filtered by date range and/or category, oldest first."""
    if not STORE_DIR.exists():
        return []
    records = []
    for path in sorted(STORE_DIR.glob("????-??.jsonl")):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                d = rec.get("date", "")
                if start_date and d < start_date:
                    continue
                if end_date and d > end_date:
                    continue
                if category and rec.get("category", "").upper() != category.upper():
                    continue
                records.append(rec)
            except Exception:
                pass
    return sorted(records, key=lambda x: x.get("date", ""))


def get_latest_flows(n: int = 20) -> list[dict]:
    """Most recent n daily FII+DII records (sorted newest first)."""
    all_records = get_flows()
    return list(reversed(all_records[-n:])) if all_records else []


def get_index() -> dict:
    """Daily summary index for dashboard use."""
    return _load_index()


def _to_float(val) -> float:
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0
