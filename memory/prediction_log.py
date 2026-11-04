"""
prediction_log — tracks signal predictions vs actual price outcomes.

Schema: prediction_log table in graph.db
Records every ConfluenceSignal recommendation with entry price and later
fills in t+30/60/90 day returns so we can measure signal accuracy over time.

Called by:
  agents/alert_agent.py
  agents/backtest_agent.py (seed + update)
  python -m memory.prediction_log --seed / --update / --accuracy
"""
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _ensure_table():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS prediction_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT NOT NULL,
                signal      TEXT NOT NULL,
                rec_date    TEXT NOT NULL,
                entry_price REAL,
                confidence  REAL,
                t30_price   REAL,
                t60_price   REAL,
                t90_price   REAL,
                t30_return  REAL,
                t60_return  REAL,
                t90_return  REAL,
                outcome     TEXT DEFAULT 'pending',
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now')),
                UNIQUE(ticker, rec_date, signal)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pred_ticker ON prediction_log(ticker)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pred_outcome ON prediction_log(outcome)")


_ensure_table()


def record_prediction(ticker: str, signal: str, rec_date: str,
                      entry_price: float = None, confidence: float = None) -> int:
    """Insert a new prediction. Returns inserted id or existing id on conflict."""
    with _conn() as c:
        try:
            cur = c.execute(
                """INSERT INTO prediction_log (ticker, signal, rec_date, entry_price, confidence)
                   VALUES (?,?,?,?,?)""",
                (ticker.upper(), signal.upper(), rec_date, entry_price, confidence)
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            row = c.execute(
                "SELECT id FROM prediction_log WHERE ticker=? AND rec_date=? AND signal=?",
                (ticker.upper(), rec_date, signal.upper())
            ).fetchone()
            return row["id"] if row else -1


def _get_price_on_date(ticker: str, target_date: date) -> float | None:
    """Find the closest PriceSnapshot price at or after target_date."""
    with _conn() as c:
        # Look ±5 days around the target date
        for delta in range(0, 6):
            d_str = (target_date + timedelta(days=delta)).isoformat()
            row = c.execute(
                """SELECT properties FROM nodes
                   WHERE node_type='PriceSnapshot' AND ticker=?
                   AND json_extract(properties,'$.date') = ?
                   LIMIT 1""",
                (ticker, d_str)
            ).fetchone()
            if row:
                try:
                    return json.loads(row["properties"]).get("close")
                except Exception:
                    pass
        # Fallback: get latest PriceSnapshot for ticker
        row = c.execute(
            """SELECT properties FROM nodes
               WHERE node_type='PriceSnapshot' AND ticker=?
               ORDER BY created_at DESC LIMIT 1""",
            (ticker,)
        ).fetchone()
        if row:
            try:
                return json.loads(row["properties"]).get("close")
            except Exception:
                pass
    return None


def update_outcomes(ticker: str = None) -> int:
    """
    Update t30/t60/t90 prices and returns for predictions that are due.
    Returns number of rows updated.
    """
    updated = 0
    with _conn() as c:
        q = "SELECT * FROM prediction_log WHERE outcome='pending'"
        params = []
        if ticker:
            q += " AND ticker=?"
            params.append(ticker.upper())
        rows = c.execute(q, params).fetchall()

    for row in rows:
        rec_date = date.fromisoformat(row["rec_date"])
        entry    = row["entry_price"]
        if not entry:
            continue

        updates = {}
        today   = date.today()

        if row["t30_price"] is None and (today - rec_date).days >= 30:
            p = _get_price_on_date(row["ticker"], rec_date + timedelta(days=30))
            if p:
                updates["t30_price"]  = p
                updates["t30_return"] = round((p - entry) / entry, 4)

        if row["t60_price"] is None and (today - rec_date).days >= 60:
            p = _get_price_on_date(row["ticker"], rec_date + timedelta(days=60))
            if p:
                updates["t60_price"]  = p
                updates["t60_return"] = round((p - entry) / entry, 4)

        if row["t90_price"] is None and (today - rec_date).days >= 90:
            p = _get_price_on_date(row["ticker"], rec_date + timedelta(days=90))
            if p:
                updates["t90_price"]  = p
                updates["t90_return"] = round((p - entry) / entry, 4)

        # Determine outcome once t30 is known
        if updates.get("t30_return") is not None or row["t30_return"] is not None:
            ret = updates.get("t30_return", row["t30_return"])
            sig = row["signal"].upper()
            if sig == "BUY":
                outcome = "correct" if ret > 0 else "incorrect"
            elif sig == "AVOID":
                outcome = "correct" if ret < 0 else "incorrect"
            else:
                outcome = "pending"
            updates["outcome"] = outcome

        if updates:
            updates["updated_at"] = datetime.now().isoformat()
            set_clause = ", ".join(f"{k}=?" for k in updates)
            vals = list(updates.values()) + [row["id"]]
            with _conn() as c:
                c.execute(f"UPDATE prediction_log SET {set_clause} WHERE id=?", vals)
            updated += 1

    return updated


def prediction_accuracy(days_back: int = 90) -> dict:
    """
    Returns accuracy breakdown by signal type.
    {signal: {total, correct, incorrect, pending, accuracy_pct}}
    """
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT signal, outcome, COUNT(*) as n FROM prediction_log "
            "WHERE rec_date >= ? GROUP BY signal, outcome",
            (cutoff,)
        ).fetchall()

    result = {}
    for row in rows:
        sig = row["signal"]
        if sig not in result:
            result[sig] = {"total": 0, "correct": 0, "incorrect": 0, "pending": 0}
        result[sig][row["outcome"]] += row["n"]
        result[sig]["total"] += row["n"]

    for sig, v in result.items():
        decided = v["correct"] + v["incorrect"]
        v["accuracy_pct"] = round(v["correct"] / decided * 100, 1) if decided > 0 else None

    return result


def get_pending(days_old: int = 30) -> list[dict]:
    """Predictions due for outcome update (created >= days_old days ago)."""
    cutoff = (date.today() - timedelta(days=days_old)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM prediction_log WHERE outcome='pending' AND rec_date <= ?",
            (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_all(ticker: str = None) -> list[dict]:
    with _conn() as c:
        if ticker:
            rows = c.execute(
                "SELECT * FROM prediction_log WHERE ticker=? ORDER BY rec_date DESC",
                (ticker.upper(),)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM prediction_log ORDER BY rec_date DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def seed_from_confluence_signals() -> int:
    """
    Seed prediction_log from existing ConfluenceSignal nodes.
    Uses today as rec_date and the signal's close price as entry_price.
    """
    with _conn() as c:
        signals = c.execute(
            """SELECT ticker, properties, confidence FROM nodes
               WHERE node_type='ConfluenceSignal'"""
        ).fetchall()

    today   = date.today().isoformat()
    seeded  = 0
    for row in signals:
        try:
            props = json.loads(row["properties"]) if row["properties"] else {}
            sig_type = props.get("type", "")
            if "BUY" in sig_type:
                signal = "BUY"
            elif "AVOID" in sig_type or "SELL" in sig_type:
                signal = "AVOID"
            else:
                signal = "HOLD"

            entry_price = props.get("close")
            conf        = row["confidence"]
            ticker      = row["ticker"]
            if not ticker:
                continue

            rec_id = record_prediction(ticker, signal, today, entry_price, conf)
            if rec_id > 0:
                seeded += 1
        except Exception:
            pass

    return seeded


if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))
    p = argparse.ArgumentParser(description="Prediction log utility")
    p.add_argument("--seed",     action="store_true", help="Seed from ConfluenceSignals")
    p.add_argument("--update",   action="store_true", help="Update pending outcomes")
    p.add_argument("--accuracy", action="store_true", help="Print accuracy report")
    p.add_argument("--ticker",   help="Filter by ticker")
    args = p.parse_args()

    if args.seed:
        n = seed_from_confluence_signals()
        print(f"Seeded {n} predictions")
    elif args.update:
        n = update_outcomes(args.ticker)
        print(f"Updated {n} predictions")
    elif args.accuracy:
        import json as _json
        print(_json.dumps(prediction_accuracy(), indent=2))
    else:
        rows = get_all(args.ticker)
        print(f"{len(rows)} predictions")
        for r in rows[:10]:
            print(f"  {r['ticker']:12s} {r['signal']:5s} {r['rec_date']} entry={r['entry_price']} outcome={r['outcome']}")
