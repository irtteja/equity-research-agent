"""
Storage for structured quarterly financial results (XBRL-sourced via financial_results_detail).

Layout:
  memory_store/results/<TICKER>.jsonl  — one line per quarter, oldest to newest
  memory_store/results/_index.json     — {ticker: {latest_period, quarters, last_updated}}
"""
import json
import os
from datetime import date
from pathlib import Path

STORE_DIR = Path(__file__).parent.parent / "memory_store" / "results"


def _ticker_path(symbol: str) -> Path:
    return STORE_DIR / f"{symbol.upper()}.jsonl"


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


def _save_index(index: dict):
    _index_path().write_text(json.dumps(index, indent=2, default=str))


def _quarter_key(q: dict) -> str:
    """Stable unique key for a quarter — prefer period_end date."""
    return q.get("period_end", "") or q.get("quarter", "")


def save_results(symbol: str, quarters: list[dict]) -> dict:
    """
    Save quarterly results for a ticker. Idempotent — skips quarters already stored.
    Returns {"saved": n, "skipped": n}.
    """
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    symbol = symbol.upper()
    path = _ticker_path(symbol)

    existing_keys = set()
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    existing_keys.add(_quarter_key(json.loads(line)))
                except Exception:
                    pass

    saved, skipped = 0, 0
    new_quarters = []
    for q in quarters:
        key = _quarter_key(q)
        if key and key in existing_keys:
            skipped += 1
            continue
        q = dict(q)
        q["symbol"] = symbol
        q.setdefault("fetched_at", date.today().isoformat())
        new_quarters.append(q)
        saved += 1

    if new_quarters:
        with open(path, "a") as f:
            for q in new_quarters:
                f.write(json.dumps(q, default=str) + "\n")

        all_quarters = get_results(symbol)
        latest = max(all_quarters, key=lambda x: x.get("period_end", ""), default={})
        index = _load_index()
        index[symbol] = {
            "latest_period":  latest.get("period_end", ""),
            "latest_quarter": latest.get("quarter", ""),
            "quarters":       len(all_quarters),
            "last_updated":   date.today().isoformat(),
        }
        _save_index(index)

    return {"saved": saved, "skipped": skipped}


def get_results(symbol: str) -> list[dict]:
    """All quarters for a ticker, sorted oldest first."""
    path = _ticker_path(symbol.upper())
    if not path.exists():
        return []
    quarters = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                quarters.append(json.loads(line))
            except Exception:
                pass
    return sorted(quarters, key=lambda x: x.get("period_end", ""))


def get_latest(symbol: str) -> dict | None:
    """Most recent quarter for a ticker."""
    quarters = get_results(symbol)
    return quarters[-1] if quarters else None


def get_deltas(symbol: str) -> list[dict]:
    """
    Quarter-over-quarter deltas for key P&L metrics.
    Returns list of {period_end, quarter, metric, current, previous, delta_pct}.
    """
    quarters = get_results(symbol)
    if len(quarters) < 2:
        return []

    metrics = [
        "revenue_from_operations", "pat", "ebit",
        "total_income", "total_expenses", "eps_basic",
    ]
    deltas = []
    for i in range(1, len(quarters)):
        curr, prev = quarters[i], quarters[i - 1]
        for m in metrics:
            c, p = curr.get(m), prev.get(m)
            if c is not None and p is not None and p != 0:
                pct = round((c - p) / abs(p) * 100, 2)
                deltas.append({
                    "period_end":   curr.get("period_end"),
                    "quarter":      curr.get("quarter"),
                    "vs_period":    prev.get("period_end"),
                    "metric":       m,
                    "current":      c,
                    "previous":     p,
                    "delta_pct":    pct,
                    "direction":    "improving" if pct > 0 else "deteriorating",
                })
    return deltas


def get_index() -> dict:
    """Summary index of all tickers with stored results."""
    return _load_index()


def list_symbols() -> list[str]:
    """All tickers that have stored results."""
    if not STORE_DIR.exists():
        return []
    return sorted(p.stem for p in STORE_DIR.glob("*.jsonl") if p.stem != "_index")
