"""
NSE Live Feed — persistent 24x7 data feed from NSE India.

Runs every 10 min (market hours) or every 2 h (after-hours) via CCR triggers.
Each run:
  1. Creates NSE(download_folder=memory_store/) → cookies saved → persist across restarts
  2. Loads memory_store/nse_feed_schedule.json → what's due?
  3. Fetches due endpoints from NSE
  4. Writes to graph.db (LiveQuote / IndexSnapshot / MarketMover / FIIDIIDaily / etc.)
  5. Saves updated schedule JSON

No anthropic.Anthropic() calls. No SME/Emerge stocks.
"""
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "vendor"))

DB_PATH       = REPO / "memory_store" / "graph.db"
SCHEDULE_FILE = REPO / "memory_store" / "nse_feed_schedule.json"
IST           = ZoneInfo("Asia/Kolkata")

# Poll intervals (seconds): (market_hours, after_hours)
POLL_SCHEDULE = {
    "live_prices":   (60,    900,  "Live equity quotes for tracked signal tickers"),
    "indices":       (60,    900,  "All NSE indices snapshot"),
    "top_movers":    (120,   1800, "Top gainers / losers / volume"),
    "market_status": (120,   1800, "Market open/close status"),
    "corp_events":   (300,   900,  "Corporate announcements last 7 days"),
    "board_meetings":(600,   3600, "Board meeting calendar"),
    "fii_dii":       (600,   1800, "FII/DII daily trade activity"),
    "option_chains": (300,   3600, "NIFTY + BANKNIFTY option chains"),
    "delivery":      (3600,  3600, "Delivery % data"),
    "insider":       (7200,  21600,"Insider trade filings"),
    "bulk_deals":    (1800,  7200, "Block and bulk deals"),
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    return datetime.now(IST)


def _is_market_hours() -> bool:
    """True if current IST time is within NSE trading hours (Mon-Fri 9:15-15:30)."""
    now = _now_ist()
    if now.weekday() >= 5:   # Saturday / Sunday
        return False
    t = now.time()
    from datetime import time as dtime
    return dtime(9, 15) <= t <= dtime(15, 30)


def _load_schedule() -> dict:
    if SCHEDULE_FILE.exists():
        try:
            return json.loads(SCHEDULE_FILE.read_text())
        except Exception:
            pass
    return {k: {"last": 0, "count": 0, "errors": 0} for k in POLL_SCHEDULE}


def _save_schedule(sched: dict) -> None:
    SCHEDULE_FILE.write_text(json.dumps(sched, indent=2))


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _upsert_node(node_id: str, node_type: str, name: str,
                 ticker: str, props: dict) -> None:
    with _conn() as c:
        existing = c.execute(
            "SELECT properties FROM nodes WHERE id=?", (node_id,)
        ).fetchone()
        if existing:
            merged = json.loads(existing["properties"] or "{}")
            merged.update(props)
            c.execute(
                "UPDATE nodes SET properties=?, name=? WHERE id=?",
                (json.dumps(merged, default=str), name, node_id)
            )
        else:
            c.execute(
                "INSERT INTO nodes (id, node_type, name, ticker, properties, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (node_id, node_type, name, ticker.upper() if ticker else "",
                 json.dumps(props, default=str), datetime.now().isoformat())
            )


def _signal_tickers() -> list[str]:
    """Return tickers that have ConfluenceSignal nodes (mainboard tracked stocks)."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT DISTINCT ticker FROM nodes WHERE node_type='ConfluenceSignal' "
                "AND ticker IS NOT NULL ORDER BY ticker"
            ).fetchall()
            return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def _filter_sme(tickers: list[str]) -> list[str]:
    """Remove known SME/Emerge tickers (heuristic: mainboard symbols are ≤10 chars usually)."""
    # The tracked ConfluenceSignal tickers are already mainboard-only by construction.
    return [t for t in tickers if t and len(t) <= 15 and not t.endswith("-SME")]


# ── Fetch functions ───────────────────────────────────────────────────────────

def fetch_live_prices(nse, dry_run: bool = False) -> int:
    """Fetch live quotes for signal tickers and write LiveQuote nodes."""
    tickers = _filter_sme(_signal_tickers())[:100]
    if dry_run:
        print(f"  [dry-run] live_prices: would fetch {len(tickers)} tickers")
        return 0
    written = 0
    today = date.today().isoformat()
    ts = datetime.now().isoformat()
    for ticker in tickers:
        try:
            q = nse.equity_quote(ticker)
            if not q:
                continue
            info = q.get("info", q) if isinstance(q, dict) else {}
            pd = q.get("priceInfo", q) if isinstance(q, dict) else {}
            props = {
                "ltp":          pd.get("lastPrice") or info.get("lastPrice"),
                "change_pct":   pd.get("pChange") or info.get("pChange"),
                "volume":       pd.get("totalTradedVolume") or info.get("totalTradedVolume"),
                "high":         pd.get("intraDayHighLow", {}).get("max") if isinstance(pd, dict) else None,
                "low":          pd.get("intraDayHighLow", {}).get("min") if isinstance(pd, dict) else None,
                "high52w":      pd.get("weekHighLow", {}).get("max") if isinstance(pd, dict) else None,
                "low52w":       pd.get("weekHighLow", {}).get("min") if isinstance(pd, dict) else None,
                "open":         pd.get("open") if isinstance(pd, dict) else None,
                "prev_close":   pd.get("previousClose") if isinstance(pd, dict) else None,
                "ts":           ts,
            }
            node_id = f"LiveQuote_{ticker}_{today}"
            _upsert_node(node_id, "LiveQuote", f"{ticker} live {today}", ticker, props)
            written += 1
            time.sleep(0.1)   # ~2 rps
        except Exception as e:
            if "403" in str(e) or "Session" in str(e):
                break  # stop on auth failure
    return written


def fetch_indices(nse, dry_run: bool = False) -> int:
    """Fetch all NSE indices and write IndexSnapshot nodes."""
    if dry_run:
        print("  [dry-run] indices: would fetch allIndices")
        return 0
    try:
        data = nse.index_data()
        if not data:
            return 0
        indices = data if isinstance(data, list) else data.get("data", [])
        today = date.today().isoformat()
        ts = datetime.now().isoformat()
        written = 0
        for idx in indices:
            name = idx.get("index") or idx.get("indexSymbol", "")
            if not name:
                continue
            props = {
                "value":        idx.get("last"),
                "change":       idx.get("change"),
                "change_pct":   idx.get("percentChange"),
                "advances":     idx.get("advances"),
                "declines":     idx.get("declines"),
                "unchanged":    idx.get("unchanged"),
                "ts":           ts,
            }
            nid = f"IndexSnapshot_{name.replace(' ','_')}_{today}"
            _upsert_node(nid, "IndexSnapshot", f"{name} {today}", "", props)
            written += 1
        return written
    except Exception:
        return 0


def fetch_top_movers(nse, dry_run: bool = False) -> int:
    """Fetch top gainers, losers, volume leaders."""
    if dry_run:
        print("  [dry-run] top_movers: would fetch gainers/losers/volume")
        return 0
    written = 0
    today = date.today().isoformat()
    ts = datetime.now().isoformat()
    try:
        # Volume gainers (live)
        vg = nse.live_volume_gainers()
        if vg:
            recs = vg if isinstance(vg, list) else vg.get("data", [])
            for r in (recs or [])[:20]:
                sym = r.get("symbol") or r.get("ticker", "")
                if not sym:
                    continue
                nid = f"MarketMover_vol_{sym}_{today}"
                _upsert_node(nid, "MarketMover", f"{sym} vol-leader {today}", sym, {
                    "mover_type": "volume", "symbol": sym,
                    "change_pct": r.get("pChange"), "volume": r.get("totalTradedVolume"),
                    "ltp": r.get("lastPrice"), "ts": ts,
                })
                written += 1
    except Exception:
        pass
    return written


def fetch_market_status(nse, dry_run: bool = False) -> int:
    """Fetch market open/close status."""
    if dry_run:
        print("  [dry-run] market_status: would fetch marketStatus")
        return 0
    try:
        data = nse.status()
        if not data:
            return 0
        props = {"raw": data, "ts": datetime.now().isoformat()}
        nid = f"MarketStatus_{date.today().isoformat()}"
        _upsert_node(nid, "MarketStatus", f"market status {date.today()}", "", props)
        return 1
    except Exception:
        return 0


def fetch_corp_events(nse, dry_run: bool = False) -> int:
    """Fetch corporate announcements for last 7 days."""
    if dry_run:
        print("  [dry-run] corp_events: would fetch announcements last 7 days")
        return 0
    try:
        from datetime import timedelta
        from_dt = date.today() - timedelta(days=7)
        data = nse.corporate_announcements(from_date=from_dt, to_date=date.today())
        if not data:
            return 0
        recs = data if isinstance(data, list) else data.get("data", [])
        written = 0
        for r in (recs or [])[:200]:
            sym = r.get("symbol", "")
            if not sym:
                continue
            # skip SME
            sm = r.get("smType", "") or r.get("series", "")
            if sm in ("SME", "EMERGE"):
                continue
            dt = r.get("broadcastDate") or r.get("exchdisstime", "")[:10]
            subj = r.get("subject", "")[:80]
            nid = f"CorporateEvent_{sym}_{dt}_{abs(hash(subj))%99999}"
            _upsert_node(nid, "CorporateEvent", f"{sym} {subj[:40]}", sym, {
                "subject":    subj,
                "category":   r.get("desc") or r.get("category", ""),
                "date":       dt,
                "detail":     r.get("body", "")[:200],
                "ts":         datetime.now().isoformat(),
            })
            written += 1
        return written
    except Exception:
        return 0


def fetch_board_meetings(nse, dry_run: bool = False) -> int:
    """Fetch upcoming board meeting calendar."""
    if dry_run:
        print("  [dry-run] board_meetings: would fetch board meeting calendar")
        return 0
    try:
        data = nse.corporate_board_meetings()
        if not data:
            return 0
        recs = data if isinstance(data, list) else data.get("data", [])
        written = 0
        for r in (recs or [])[:100]:
            sym = r.get("symbol", "")
            if not sym:
                continue
            dt = r.get("bm_date", "")
            nid = f"BoardMeeting_{sym}_{dt}"
            _upsert_node(nid, "BoardMeeting", f"{sym} board {dt}", sym, {
                "date":    dt,
                "purpose": r.get("bm_purpose", "")[:100],
                "ts":      datetime.now().isoformat(),
            })
            written += 1
        return written
    except Exception:
        return 0


def fetch_fii_dii(nse, dry_run: bool = False) -> int:
    """Fetch FII/DII trade activity."""
    if dry_run:
        print("  [dry-run] fii_dii: would fetch FII/DII trade activity")
        return 0
    try:
        data = nse.fii_dii_trade_activity()
        if not data:
            return 0
        recs = data if isinstance(data, list) else [data]
        written = 0
        today = date.today().isoformat()
        for r in (recs or [])[:5]:
            dt = r.get("date", today)
            cat = r.get("category", r.get("clientType", ""))
            nid = f"FIIDIIDaily_{cat}_{dt}"
            _upsert_node(nid, "FIIDIIDaily", f"FII/DII {cat} {dt}", "", {
                "category":  cat,
                "date":      dt,
                "buy_value": r.get("buyValue"),
                "sell_value":r.get("sellValue"),
                "net_value": r.get("netValue"),
                "ts":        datetime.now().isoformat(),
            })
            written += 1
        return written
    except Exception:
        return 0


def fetch_option_chains(nse, dry_run: bool = False) -> int:
    """Fetch NIFTY + BANKNIFTY option chain snapshots."""
    if dry_run:
        print("  [dry-run] option_chains: would fetch NIFTY + BANKNIFTY chains")
        return 0
    written = 0
    today = date.today().isoformat()
    ts = datetime.now().isoformat()
    for underlying in ("NIFTY", "BANKNIFTY"):
        try:
            chain = nse.option_chain(underlying)
            if not chain:
                continue
            recs = chain.get("records", chain) if isinstance(chain, dict) else {}
            expiryDates = recs.get("expiryDates", []) if isinstance(recs, dict) else []
            expiry = expiryDates[0] if expiryDates else ""
            data = recs.get("data", []) if isinstance(recs, dict) else []
            total_ce_oi = sum(r.get("CE", {}).get("openInterest", 0) for r in data if isinstance(r, dict) and "CE" in r)
            total_pe_oi = sum(r.get("PE", {}).get("openInterest", 0) for r in data if isinstance(r, dict) and "PE" in r)
            pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else None
            nid = f"OptionChainSnapshot_{underlying}_{today}"
            _upsert_node(nid, "OptionChainSnapshot", f"{underlying} options {today}", underlying, {
                "underlying": underlying,
                "expiry":     expiry,
                "calls_oi":   total_ce_oi,
                "puts_oi":    total_pe_oi,
                "pcr":        pcr,
                "ts":         ts,
            })
            written += 1
            time.sleep(0.5)
        except Exception:
            pass
    return written


def fetch_delivery(nse, dry_run: bool = False) -> int:
    """Fetch delivery % data via bhavcopy."""
    if dry_run:
        print("  [dry-run] delivery: would fetch delivery bhavcopy")
        return 0
    try:
        data = nse.eq_bhavcopy_with_delivery()
        if data is None:
            return 0
        recs = data.to_dict(orient="records") if hasattr(data, "to_dict") else (data if isinstance(data, list) else [])
        written = 0
        today = date.today().isoformat()
        for r in recs[:200]:
            sym = r.get("SYMBOL") or r.get("symbol", "")
            if not sym:
                continue
            # skip SME
            series = r.get("SERIES") or r.get("series", "")
            if series not in ("EQ", "BE", "BL", ""):
                continue
            deliv_qty  = r.get("DELIV_QTY") or r.get("deliv_qty")
            traded_qty = r.get("TTL_TRD_QNTY") or r.get("ttl_trd_qnty")
            deliv_pct  = None
            if deliv_qty and traded_qty and float(traded_qty) > 0:
                deliv_pct = round(float(deliv_qty) / float(traded_qty) * 100, 2)
            nid = f"DeliveryMetric_{sym}_{today}"
            _upsert_node(nid, "DeliveryMetric", f"{sym} delivery {today}", sym, {
                "deliv_qty":   deliv_qty,
                "traded_qty":  traded_qty,
                "deliv_pct":   deliv_pct,
                "close":       r.get("CLOSE_PRICE") or r.get("close"),
                "ts":          datetime.now().isoformat(),
            })
            written += 1
        return written
    except Exception:
        return 0


def fetch_insider(nse, dry_run: bool = False) -> int:
    """Fetch insider trading filings."""
    if dry_run:
        print("  [dry-run] insider: would fetch insider trading PIT data")
        return 0
    try:
        from datetime import timedelta
        data = nse.insider_trading(from_date=date.today() - timedelta(days=30), to_date=date.today())
        if not data:
            return 0
        recs = data if isinstance(data, list) else data.get("data", [])
        written = 0
        for r in (recs or [])[:100]:
            sym = r.get("symbol", "")
            if not sym:
                continue
            dt = r.get("date", "")
            nid = f"InsiderTrade_{sym}_{dt}_{abs(hash(str(r)))%99999}"
            _upsert_node(nid, "InsiderTrade", f"{sym} insider {dt}", sym, {
                "acqMode":    r.get("acqMode"),
                "acqName":    r.get("acqName"),
                "secType":    r.get("secType"),
                "secAcq":     r.get("secAcq"),
                "secVal":     r.get("secVal"),
                "txnType":    r.get("tdpTransactionType"),
                "date":       dt,
                "ts":         datetime.now().isoformat(),
            })
            written += 1
        return written
    except Exception:
        return 0


def fetch_bulk_deals(nse, dry_run: bool = False) -> int:
    """Fetch block and bulk deals."""
    if dry_run:
        print("  [dry-run] bulk_deals: would fetch block/bulk deals")
        return 0
    written = 0
    today = date.today().isoformat()
    for method_name in ("block_deals", "bulk_deals"):
        try:
            fn = getattr(nse, method_name, None)
            if fn is None:
                continue
            data = fn()
            if not data:
                continue
            recs = data if isinstance(data, list) else data.get("data", [])
            for r in (recs or [])[:50]:
                sym = r.get("symbol", "")
                if not sym:
                    continue
                dt = r.get("date", today)
                nid = f"BulkDeal_{method_name}_{sym}_{dt}_{abs(hash(str(r)))%99999}"
                _upsert_node(nid, "BulkDeal", f"{sym} {method_name} {dt}", sym, {
                    "deal_type":  method_name,
                    "client":     r.get("clientName"),
                    "qty":        r.get("quantity"),
                    "price":      r.get("tradePrice"),
                    "buy_sell":   r.get("buySell"),
                    "date":       dt,
                    "ts":         datetime.now().isoformat(),
                })
                written += 1
        except Exception:
            pass
    return written


# ── Dispatch map ─────────────────────────────────────────────────────────────

FETCH_FNS = {
    "live_prices":   fetch_live_prices,
    "indices":       fetch_indices,
    "top_movers":    fetch_top_movers,
    "market_status": fetch_market_status,
    "corp_events":   fetch_corp_events,
    "board_meetings":fetch_board_meetings,
    "fii_dii":       fetch_fii_dii,
    "option_chains": fetch_option_chains,
    "delivery":      fetch_delivery,
    "insider":       fetch_insider,
    "bulk_deals":    fetch_bulk_deals,
}


# ── Main run ─────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, force_all: bool = False, verbose: bool = True) -> dict:
    """
    One polling cycle. Returns summary dict.
    """
    market = _is_market_hours()
    now_ts = time.time()
    sched  = _load_schedule()

    # Ensure all keys exist in schedule
    for k in POLL_SCHEDULE:
        sched.setdefault(k, {"last": 0, "count": 0, "errors": 0})

    if verbose:
        ist_now = _now_ist().strftime("%Y-%m-%d %H:%M IST")
        print(f"[nse_live_feed] {ist_now}  market={'OPEN' if market else 'CLOSED'}  dry_run={dry_run}")

    # Init NSE with persistent cookie path
    nse = None
    if not dry_run:
        try:
            from nselib import NSE
            nse = NSE(download_folder=str(REPO / "memory_store"), rate=2.0, timeout=20)
        except Exception as e:
            print(f"  [ERROR] NSE init failed: {e}")
            return {"error": str(e), "fetched": {}}

    results = {}
    total_written = 0

    for key, (mkt_int, ah_int, desc) in POLL_SCHEDULE.items():
        interval = mkt_int if market else ah_int
        last     = sched[key]["last"]
        due      = force_all or (now_ts - last >= interval)

        if not due:
            remaining = int(interval - (now_ts - last))
            if verbose:
                print(f"  skip  {key:16s}  next in {remaining}s")
            continue

        if verbose:
            print(f"  fetch {key:16s}  ({desc})")

        fn = FETCH_FNS.get(key)
        if fn is None:
            continue

        try:
            t0 = time.time()
            written = fn(nse, dry_run=dry_run)
            elapsed = round(time.time() - t0, 2)
            sched[key]["last"]  = now_ts
            sched[key]["count"] += 1
            results[key] = {"written": written, "elapsed_s": elapsed}
            total_written += written
            if verbose:
                print(f"         → {written} nodes  {elapsed}s")
        except Exception as e:
            sched[key]["errors"] = sched[key].get("errors", 0) + 1
            results[key] = {"error": str(e)[:80]}
            if verbose:
                print(f"         → ERROR: {e}")

    if not dry_run:
        _save_schedule(sched)
        if nse is not None:
            try:
                nse.close()
            except Exception:
                pass

    summary = {
        "ts":          _now_ist().isoformat(),
        "market_open": market,
        "dry_run":     dry_run,
        "total_written": total_written,
        "endpoints":   results,
    }
    if verbose:
        print(f"\n[nse_live_feed] done — {total_written} nodes written across {len(results)} endpoints")
    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="NSE 24x7 live data feed")
    p.add_argument("--dry-run",   action="store_true", help="Show what would be fetched, no actual calls")
    p.add_argument("--force-all", action="store_true", help="Ignore schedule, fetch everything now")
    p.add_argument("--quiet",     action="store_true", help="Suppress verbose output")
    p.add_argument("--json",      action="store_true", help="Print result as JSON")
    args = p.parse_args()

    result = run(dry_run=args.dry_run, force_all=args.force_all, verbose=not args.quiet)
    if args.json:
        import json as _json
        print(_json.dumps(result, indent=2, default=str))
