#!/usr/bin/env python3
"""
New-source ingestion job — runs daily after daily_monitor.

Fetches 7 market-wide and per-ticker NSE data sources:
  Market-wide (once per day):
    1. delivery_bhavcopy  — delivery % for all EQ stocks
    2. surveillance       — ASM/GSM/T2T list
    3. participant_oi     — FII/DII/Client/Pro OI in F&O
    4. fii_dii            — FII/DII flow to flat-file store

  Per-ticker (for watchlist / result-day companies):
    5. insider_trading    — SEBI insider disclosures (last 30 days)
    6. pledge_data        — Promoter pledge filings  (last 30 days)
    7. sast_reg29         — SEBI SAST Reg 29         (last 30 days)

Storage:
  Flat files: memory_store/{delivery_bhavcopy,participant_oi,surveillance,fii_dii}/YYYY-MM.jsonl
  Graph:      SurveillanceFlag, DeliveryMetric, ParticipantOI, InsiderTrade, PledgeData, SASTFiling nodes
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_NSELIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor")
if not os.path.isdir(os.path.join(_NSELIB_PATH, "nselib")):
    _NSELIB_PATH = os.path.expanduser("~/irtteja/nselib")
if _NSELIB_PATH not in sys.path:
    sys.path.insert(0, _NSELIB_PATH)

import csv
import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).parent.parent

_SME_SERIES = {"SM", "ST", "SG", "SS"}


def _sme(series: str) -> bool:
    return str(series).strip().upper() in _SME_SERIES


# ── Flat-file helpers ─────────────────────────────────────────────────────────

def _append_jsonl(store_dir: Path, dt: date, records: list[dict]) -> int:
    """Append records to memory_store/<subdir>/YYYY-MM.jsonl. Returns count written."""
    if not records:
        return 0
    store_dir.mkdir(parents=True, exist_ok=True)
    path = store_dir / f"{dt.strftime('%Y-%m')}.jsonl"
    with open(path, "a") as f:
        for rec in records:
            f.write(json.dumps(rec, default=str) + "\n")
    return len(records)


def _already_stored(store_dir: Path, dt: date, key_field: str = "date") -> bool:
    """Return True if any record for dt already exists in this month's JSONL."""
    path = store_dir / f"{dt.strftime('%Y-%m')}.jsonl"
    if not path.exists():
        return False
    date_str = dt.isoformat()
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line.strip())
            if rec.get(key_field, "") == date_str:
                return True
        except Exception:
            pass
    return False


# ── 1. Delivery bhavcopy ──────────────────────────────────────────────────────

def ingest_delivery_bhavcopy(trade_date: date | None = None) -> dict:
    """
    Download NSE full delivery bhavcopy and write:
      - Flat file: all EQ stocks' delivery data
      - Graph nodes: top-100 by delivery value (DeliveryMetric)
    """
    dt = trade_date or (date.today() - timedelta(days=1))
    store_dir = REPO / "memory_store" / "delivery_bhavcopy"

    if _already_stored(store_dir, dt):
        return {"status": "already_stored", "date": dt.isoformat()}

    try:
        from nselib import NSE
        from memory.node_types import write_delivery_metric

        with tempfile.TemporaryDirectory() as tmpdir:
            with NSE() as nse:
                csv_path = nse.full_bhavcopy_delivery(dt=dt, folder=tmpdir)

            rows = []
            with open(csv_path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    series = row.get("SERIES", "").strip().upper()
                    if _sme(series):
                        continue
                    if series not in {"EQ", "BE", "BZ"}:
                        continue
                    sym = row.get("SYMBOL", "").strip().upper()
                    if not sym:
                        continue
                    try:
                        deliv_pct = float(row.get("DELIV_PER", 0) or 0)
                        deliv_qty = float(row.get("DELIV_QTY", 0) or 0)
                        traded_qty = float(row.get("TTL_TRD_QNTY", 0) or 0)
                        turnover = float(row.get("TURNOVER_LACS", 0) or 0)
                    except (ValueError, TypeError):
                        continue
                    rows.append({
                        "date":        dt.isoformat(),
                        "symbol":      sym,
                        "series":      series,
                        "delivery_pct": deliv_pct,
                        "delivery_qty": deliv_qty,
                        "traded_qty":  traded_qty,
                        "turnover_lacs": turnover,
                    })

        # Save all to flat file
        _append_jsonl(store_dir, dt, rows)

        # Write graph nodes for top-100 by delivery value (turnover * delivery_pct)
        scored = sorted(rows, key=lambda r: r["turnover_lacs"] * r["delivery_pct"], reverse=True)
        graph_count = 0
        for r in scored[:100]:
            write_delivery_metric(
                ticker=r["symbol"],
                trade_date=r["date"],
                delivery_qty=r["delivery_qty"],
                delivery_pct=r["delivery_pct"],
                traded_qty=r["traded_qty"],
                turnover_lacs=r["turnover_lacs"],
            )
            graph_count += 1

        return {
            "date": dt.isoformat(),
            "total_rows": len(rows),
            "graph_nodes": graph_count,
        }

    except Exception as e:
        return {"error": str(e), "date": dt.isoformat()}


# ── 2. Surveillance ───────────────────────────────────────────────────────────

def ingest_surveillance(trade_date: date | None = None) -> dict:
    """
    Fetch ASM/GSM/T2T surveillance lists and write:
      - Flat file: all flagged stocks
      - Graph nodes: SurveillanceFlag per stock per list type
    """
    dt = trade_date or date.today()
    store_dir = REPO / "memory_store" / "surveillance"

    if _already_stored(store_dir, dt):
        return {"status": "already_stored", "date": dt.isoformat()}

    try:
        from tools.indian_markets import nse_surveillance
        from memory.node_types import write_surveillance_flag

        all_records = []
        graph_count = 0

        for list_type in ["asm", "gsm", "t2t"]:
            try:
                result = nse_surveillance(list_type=list_type)
                items = result.get("data", []) if isinstance(result, dict) else []
                if isinstance(result, list):
                    items = result
                for item in items:
                    sym = str(item.get("symbol", item.get("Symbol", ""))).strip().upper()
                    if not sym:
                        continue
                    stage = str(item.get("stage", item.get("Stage", ""))).strip()
                    reason = str(item.get("reason", item.get("Reason", ""))).strip()
                    all_records.append({
                        "date":      dt.isoformat(),
                        "symbol":    sym,
                        "list_type": list_type,
                        "stage":     stage,
                        "reason":    reason,
                    })
                    write_surveillance_flag(
                        ticker=sym,
                        list_type=list_type,
                        stage=stage,
                        trade_date=dt.isoformat(),
                        reason=reason,
                    )
                    graph_count += 1
            except Exception as e:
                all_records.append({"date": dt.isoformat(), "list_type": list_type, "error": str(e)})

        _append_jsonl(store_dir, dt, all_records)
        return {
            "date":        dt.isoformat(),
            "total_flags": graph_count,
            "graph_nodes": graph_count,
        }

    except Exception as e:
        return {"error": str(e), "date": dt.isoformat()}


# ── 3. Participant OI ─────────────────────────────────────────────────────────

def ingest_participant_oi(trade_date: date | None = None) -> dict:
    """
    Fetch FII/DII/Client/Pro open interest in equity F&O and write:
      - Flat file: participant-wise OI
      - Graph node: single ParticipantOI node per day
    """
    dt = trade_date or (date.today() - timedelta(days=1))
    store_dir = REPO / "memory_store" / "participant_oi"

    if _already_stored(store_dir, dt):
        return {"status": "already_stored", "date": dt.isoformat()}

    try:
        from nselib import NSE
        from memory.node_types import write_participant_oi

        with NSE() as nse:
            rows = nse.participant_oi(trade_date=dt)

        if not rows:
            return {"error": "no data returned", "date": dt.isoformat()}

        records = [{"date": dt.isoformat(), **row} for row in rows]
        _append_jsonl(store_dir, dt, records)
        node_id = write_participant_oi(trade_date=dt.isoformat(), rows=rows)

        return {
            "date":        dt.isoformat(),
            "participants": len(rows),
            "graph_node":  node_id,
        }

    except Exception as e:
        return {"error": str(e), "date": dt.isoformat()}


# ── 4. FII/DII flows ─────────────────────────────────────────────────────────

def ingest_fii_dii(trade_date: date | None = None) -> dict:
    """
    Fetch FII/DII equity cash market flows and persist to fii_dii_store.
    Uses get_fii_dii_flows() from tools.macro (already called in daily_monitor).
    This function ensures the data is also in the flat-file time series.
    """
    dt = trade_date or date.today()

    try:
        from tools.macro import get_fii_dii_flows
        from memory.fii_dii_store import save_flows

        raw = get_fii_dii_flows()
        flows = raw if isinstance(raw, list) else raw.get("data", [])

        # Normalise: ensure each record has category, buy, sell, net
        normalised = []
        for f in flows:
            cat = str(f.get("category", f.get("clientType", f.get("type", "")))).upper().strip()
            if not cat:
                continue
            normalised.append({
                "category": cat,
                "buy":      f.get("buyValue", f.get("buy", 0)),
                "sell":     f.get("sellValue", f.get("sell", 0)),
                "net":      f.get("netValue",  f.get("net",  0)),
                "source":   "nse",
            })

        result = save_flows(normalised, trade_date=dt)
        return {"date": dt.isoformat(), **result}

    except Exception as e:
        return {"error": str(e), "date": dt.isoformat()}


# ── 5. Insider trading ────────────────────────────────────────────────────────

def ingest_insider_trading(symbols: list[str], days_back: int = 30) -> dict:
    """
    Fetch SEBI insider trading disclosures for the given tickers.
    Writes InsiderTrade graph nodes.
    Returns {"total": n, "errors": [...]}.
    """
    from datetime import date, timedelta
    to_date = date.today()
    from_date = to_date - timedelta(days=days_back)
    fd, td = from_date.strftime("%d-%m-%Y"), to_date.strftime("%d-%m-%Y")

    total, errors = 0, []

    try:
        from nselib import NSE
        from memory.node_types import write_insider_trade

        with NSE() as nse:
            for sym in symbols:
                try:
                    records = nse.insider_trading(sym, fd, td) or []
                    for rec in records:
                        date_str = _parse_date(rec.get("date", rec.get("transactionDate", "")))
                        person = str(rec.get("name", rec.get("personName", ""))).strip()
                        txn_type = str(rec.get("transactionType", rec.get("buySell", ""))).strip()
                        try:
                            qty = float(str(rec.get("noOfSharesAcquiredDisposed", rec.get("qty", 0))).replace(",", "") or 0)
                        except (ValueError, TypeError):
                            qty = 0
                        try:
                            value = float(str(rec.get("valueOfSharesAcquiredDisposed", rec.get("value", 0))).replace(",", "") or 0)
                        except (ValueError, TypeError):
                            value = 0
                        if qty > 0 and date_str:
                            write_insider_trade(
                                ticker=sym.upper(),
                                trade_date=date_str,
                                person=person,
                                transaction_type=txn_type,
                                qty=qty,
                                value=value,
                                mode=str(rec.get("mode", "")),
                            )
                            total += 1
                except Exception as e:
                    errors.append(f"{sym}: {e}")

    except Exception as e:
        return {"error": str(e)}

    return {"total": total, "symbols_processed": len(symbols), "errors": errors}


# ── 6. Pledge data ────────────────────────────────────────────────────────────

def ingest_pledge_data(symbols: list[str], days_back: int = 30) -> dict:
    """
    Fetch promoter pledge filings for the given tickers.
    Writes PledgeData graph nodes.
    """
    from datetime import date, timedelta
    to_date = date.today()
    from_date = to_date - timedelta(days=days_back)
    fd, td = from_date.strftime("%d-%m-%Y"), to_date.strftime("%d-%m-%Y")

    total, errors = 0, []

    try:
        from nselib import NSE
        from memory.node_types import write_pledge_data

        with NSE() as nse:
            for sym in symbols:
                try:
                    records = nse.corporate_pledge_data(sym, fd, td) or []
                    for rec in records:
                        date_str = _parse_date(rec.get("date", rec.get("filingDate", "")))
                        promoter = str(rec.get("name", rec.get("promoterName", ""))).strip()
                        try:
                            pledged_qty = float(str(rec.get("pledgedShares", rec.get("qty", 0))).replace(",", "") or 0)
                        except (ValueError, TypeError):
                            pledged_qty = 0
                        try:
                            pledged_pct = float(str(rec.get("pledgePercentage", rec.get("pledgedPct", 0))).replace(",", "") or 0)
                        except (ValueError, TypeError):
                            pledged_pct = 0
                        if date_str:
                            write_pledge_data(
                                ticker=sym.upper(),
                                record_date=date_str,
                                promoter=promoter,
                                pledged_qty=pledged_qty,
                                pledged_pct=pledged_pct,
                            )
                            total += 1
                except Exception as e:
                    errors.append(f"{sym}: {e}")

    except Exception as e:
        return {"error": str(e)}

    return {"total": total, "symbols_processed": len(symbols), "errors": errors}


# ── 7. SAST Reg 29 ────────────────────────────────────────────────────────────

def ingest_sast_reg29(symbols: list[str], days_back: int = 30) -> dict:
    """
    Fetch SEBI SAST Reg 29 (substantial acquisition) disclosures.
    Writes SASTFiling graph nodes.
    """
    from datetime import date, timedelta
    to_date = date.today()
    from_date = to_date - timedelta(days=days_back)
    fd, td = from_date.strftime("%d-%m-%Y"), to_date.strftime("%d-%m-%Y")

    total, errors = 0, []

    try:
        from nselib import NSE
        from memory.node_types import write_sast_filing

        with NSE() as nse:
            for sym in symbols:
                try:
                    records = nse.sast_reg29(sym, fd, td) or []
                    for rec in records:
                        date_str = _parse_date(rec.get("date", rec.get("filingDate", "")))
                        acquirer = str(rec.get("acquirerName", rec.get("name", ""))).strip()
                        try:
                            shares = float(str(rec.get("sharesAcquired", rec.get("qty", 0))).replace(",", "") or 0)
                        except (ValueError, TypeError):
                            shares = 0
                        try:
                            post_pct = float(str(rec.get("postAcquisitionShareholdingPercentage", rec.get("postPct", 0))).replace(",", "") or 0)
                        except (ValueError, TypeError):
                            post_pct = 0
                        if date_str:
                            write_sast_filing(
                                ticker=sym.upper(),
                                filing_date=date_str,
                                acquirer=acquirer,
                                shares_acquired=shares,
                                post_acquisition_pct=post_pct,
                                mode=str(rec.get("mode", "")),
                            )
                            total += 1
                except Exception as e:
                    errors.append(f"{sym}: {e}")

    except Exception as e:
        return {"error": str(e)}

    return {"total": total, "symbols_processed": len(symbols), "errors": errors}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(val: str) -> str:
    """Normalise various date formats to YYYY-MM-DD. Returns '' on failure."""
    val = str(val).strip()
    if not val or val == "None":
        return ""
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            from datetime import datetime
            return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return val[:10] if len(val) >= 10 else ""


def _load_watchlist() -> list[str]:
    """Return tickers from the agent's watchlist (from self_model or company universe)."""
    try:
        from memory.self_model import get_watchlist
        return get_watchlist()
    except Exception:
        pass
    # Fallback: load from index companies
    try:
        idx_path = REPO / "memory_store" / "companies.json"
        if idx_path.exists():
            companies = json.loads(idx_path.read_text())
            return [c["symbol"] for c in companies if isinstance(c, dict) and c.get("symbol")]
    except Exception:
        pass
    return []


# ── run_all ───────────────────────────────────────────────────────────────────

def run_all(
    trade_date: date | None = None,
    per_ticker_symbols: list[str] | None = None,
    per_ticker_days_back: int = 30,
) -> dict:
    """
    Run all 7 ingestion functions. Called from daily_monitor steps 5c/5d/3c.

    per_ticker_symbols: tickers to scan for insider/pledge/SAST.
                        Defaults to watchlist.
    """
    dt = trade_date or date.today()
    symbols = per_ticker_symbols if per_ticker_symbols is not None else _load_watchlist()

    results = {}

    print("  [ingest] delivery bhavcopy...")
    results["delivery_bhavcopy"] = ingest_delivery_bhavcopy(dt)
    print(f"    {results['delivery_bhavcopy']}")

    print("  [ingest] surveillance...")
    results["surveillance"] = ingest_surveillance(dt)
    print(f"    {results['surveillance']}")

    print("  [ingest] participant OI...")
    results["participant_oi"] = ingest_participant_oi(dt)
    print(f"    {results['participant_oi']}")

    print("  [ingest] FII/DII flows...")
    results["fii_dii"] = ingest_fii_dii(dt)
    print(f"    {results['fii_dii']}")

    if symbols:
        print(f"  [ingest] insider trading ({len(symbols)} tickers)...")
        results["insider_trading"] = ingest_insider_trading(symbols, per_ticker_days_back)
        print(f"    {results['insider_trading']}")

        print(f"  [ingest] pledge data ({len(symbols)} tickers)...")
        results["pledge_data"] = ingest_pledge_data(symbols, per_ticker_days_back)
        print(f"    {results['pledge_data']}")

        print(f"  [ingest] SAST Reg29 ({len(symbols)} tickers)...")
        results["sast_reg29"] = ingest_sast_reg29(symbols, per_ticker_days_back)
        print(f"    {results['sast_reg29']}")
    else:
        print("  [ingest] skipping per-ticker sources (empty watchlist)")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Ingest new NSE data sources")
    parser.add_argument("--date", help="Trade date YYYY-MM-DD (default: today)")
    parser.add_argument("--market-only", action="store_true", help="Skip per-ticker sources")
    parser.add_argument("--tickers", help="Comma-separated tickers for per-ticker sources")
    args = parser.parse_args()

    dt = date.fromisoformat(args.date) if args.date else None
    syms = args.tickers.upper().split(",") if args.tickers else ([] if args.market_only else None)

    out = run_all(trade_date=dt, per_ticker_symbols=syms)
    print(json.dumps(out, indent=2, default=str))
