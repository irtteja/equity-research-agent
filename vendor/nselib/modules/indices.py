from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..exceptions import NSEDataError

# Repo-relative store paths
_STORE    = Path(__file__).parent.parent.parent / "data" / "constituents"
_BHAV_DIR = Path(__file__).parent.parent.parent / "data" / "bhav"

# Columns to keep from the bhavcopy CSV (new NSE format)
_BHAV_COLS = [
    "TradDt", "TckrSymb", "SctySrs",
    "OpnPric", "HghPric", "LwPric", "ClsPric", "PrvsClsgPric",
    "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd", "ISIN",
    "DELIV_QTY", "DELIV_PER",
]


def _parse_mto(mto_path: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """Parse NSE MTO DAT file → {(symbol, series): (deliv_qty, deliv_per)}

    Format: record_type=20, cols: rec_type, sr_no, symbol, series,
            traded_qty, deliv_qty, deliv_pct
    """
    result: dict[tuple[str, str], tuple[str, str]] = {}
    try:
        with open(mto_path, newline="") as f:
            for row in csv.reader(f):
                if len(row) < 7 or row[0].strip() != "20":
                    continue
                result[(row[2].strip(), row[3].strip())] = (
                    row[5].strip(), row[6].strip()
                )
    except Exception:
        pass
    return result
# Only equity cash-market series
_EQ_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}

# Our investable universe: Broad Market + Mid/Small/Micro Cap + Sectoral + Thematic
# + Consumption/Services indices only.
# Excluded: Strategy/Factor, Equal Weight, Top-N EW, ESG, Shariah,
#           Leverage/Inverse, Dividend/Liquidity, Corporate Group, G-Sec/Bond.
UNIVERSE_INDICES: list[str] = [
    # Broad Market (8)
    "NIFTY 50", "NIFTY NEXT 50", "NIFTY 100", "NIFTY 200", "NIFTY 500",
    "NIFTY TOTAL MKT", "NIFTY LARGEMID250", "NIFTY MIDSMALL 50 50",

    # Mid / Small / Micro Cap (11)
    "NIFTY MIDCAP 50", "NIFTY MIDCAP 100", "NIFTY MIDCAP 150",
    "NIFTY MID SELECT", "NIFTY MID LIQ 15",
    "NIFTY SMLCAP 50", "NIFTY SMLCAP 100", "NIFTY SMLCAP 250",
    "NIFTY SMALLCAP 500", "NIFTY MIDSML 400", "NIFTY MICROCAP250",

    # Sectoral (18)
    "NIFTY BANK", "NIFTY PVT BANK", "NIFTY PSU BANK",
    "NIFTY FIN SERVICE", "NIFTY FINSEREXBNK", "NIFTY FINSRV25 50",
    "NIFTY IT", "NIFTY AUTO", "NIFTY PHARMA", "NIFTY FMCG",
    "NIFTY METAL", "NIFTY MEDIA", "NIFTY REALTY", "NIFTY ENERGY",
    "NIFTY OIL AND GAS", "NIFTY HEALTHCARE", "NIFTY CONSR DURBL", "NIFTY CEMENT",

    # Thematic (23)
    "NIFTY INFRA", "NIFTY INFRALOG", "NIFTY COMMODITIES", "NIFTY CHEMICALS",
    "NIFTY PSE", "NIFTY CPSE", "NIFTY MNC", "NIFTY MOBILITY",
    "NIFTY HOUSING", "NIFTY COREHOUSING", "NIFTY RURAL", "NIFTY WAVES",
    "NIFTY IPO", "NIFTY EV", "NIFTY IND DEFENCE",
    "NIFTY IND DIGITAL", "NIFTY IND TOURISM", "NIFTY INDIA MFG",
    "NIFTY INTERNET", "NIFTY RAILWAYSPSU", "NIFTY REITS REALTY",
    "NIFTY CAPITAL MKT", "NIFTY TRANS LOGIS",

    # Consumption / Services (7)
    "NIFTY CONSUMPTION", "NIFTY NEW CONSUMP", "NIFTY NONCYC CONS",
    "NIFTY SERV SECTOR", "NIFTY GROWSECT 15", "NIFTY FPI 150", "NIFTY MIDSML HLTH",
]

# Legacy alias — full list of all 137 indices that have constituent data
INDICES_WITH_CONSTITUENTS = UNIVERSE_INDICES


def _index_key(index: str) -> str:
    return index.upper().replace(" ", "_").replace("&", "AND")


def _fmt(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _chunk_dates(from_date: date, to_date: date, days: int = 365):
    """Split a date range into chunks of at most `days` days."""
    cur = from_date
    while cur <= to_date:
        end = min(cur + timedelta(days=days - 1), to_date)
        yield cur, end
        cur = end + timedelta(days=1)


class IndicesMixin:
    """Index values, names, and historical data."""

    def list_indices(self) -> dict:
        """All NSE indices with live values, change, and turnover."""
        return self._session.get("allIndices")

    def index_quote(self, index: str) -> dict:
        """
        Live quote for a specific index by name.
        e.g. index_quote("NIFTY 50"), index_quote("NIFTY BANK")
        """
        data = self.list_indices()
        for item in data.get("data", []):
            if item.get("index", "").upper() == index.upper():
                return item
        raise NSEDataError(f"Index not found: {index!r}. Use list_indices() to see all names.")

    def _resolve_index_symbol(self, index: str) -> str:
        """
        Resolve a display name or abbreviation to the API symbol (indexSymbol).
        NSE uses abbreviated symbols (e.g. 'NIFTY FIN SERVICE') in the constituent
        API but full display names (e.g. 'NIFTY FINANCIAL SERVICES') in allIndices.
        Accepts either form.
        """
        data = self.list_indices()
        index_up = index.upper()
        for item in data.get("data", []):
            if (item.get("index", "").upper() == index_up or
                    item.get("indexSymbol", "").upper() == index_up):
                return item.get("indexSymbol", index)
        return index  # pass through unchanged if not found

    def index_constituents(self, index: str) -> list[dict]:
        """
        Live prices for all stocks in a given index.
        Accepts both the full display name (e.g. "NIFTY FINANCIAL SERVICES")
        and the abbreviated symbol (e.g. "NIFTY FIN SERVICE").

        Each record has: symbol, companyName, lastPrice, change, pChange,
        open, dayHigh, dayLow, previousClose, totalTradedVolume,
        totalTradedValue, yearHigh, yearLow, perChange30d, perChange365d.

        The first record is the index itself; the rest are its constituent stocks.
        """
        symbol = self._resolve_index_symbol(index)
        url = (
            "https://www.nseindia.com/api/NextApi/apiClient/marketWatchApi"
            f"?functionName=getIndicesData&symbol={quote(symbol)}"
        )
        resp = self._session.get_raw(url)
        data = resp.json()
        return data.get("data", {}).get("data", [])

    # ------------------------------------------------------------------ #
    #  Index universe                                                      #
    # ------------------------------------------------------------------ #

    _UNIVERSE_CACHE = Path.home() / ".nselib" / "index_universe.json"
    _UNIVERSE_TTL_DAYS = 7

    # Indices to skip — debt, Shariah, SME, leverage/inverse products
    _UNIVERSE_SKIP = {
        "G-SEC", "BOND", "SHARIAH", "SME EMERGE", "BHARAT BOND",
        "COMPOSITE G-SEC", "BENCHMARK G-SEC", "CLEAN PRICE",
        "8-13 YR", "4-8 YR", "10 YR", "11-15 YR", "15 YR AND ABOVE",
        "2X LEVERAGE", "1X INVERSE", "DIVIDEND POINTS", "USD",
    }

    def get_index_universe(self, refresh: bool = False) -> list[dict]:
        """
        Return all EQ stocks covered by at least one NSE equity index.

        Each entry:
            {
                "symbol":  "RELIANCE",
                "indices": ["NIFTY 50", "NIFTY 100", "NIFTY 500", ...],
                "index_count": 12,
            }

        Results are cached at ~/.nselib/index_universe.json and refreshed
        automatically every 7 days (index rebalances are quarterly).
        Pass refresh=True to force an immediate refresh.
        """
        cache = self._UNIVERSE_CACHE

        if not refresh and cache.exists():
            cached = json.loads(cache.read_text())
            age_days = (date.today() - date.fromisoformat(cached["last_updated"])).days
            if age_days < self._UNIVERSE_TTL_DAYS:
                return cached["universe"]

        # --- build fresh universe ---
        # Call list_indices() exactly once; build {indexSymbol: displayName} map.
        all_indices_data = self.list_indices().get("data", [])

        def _is_equity(name: str) -> bool:
            n = name.upper()
            return not any(kw in n for kw in self._UNIVERSE_SKIP)

        # {indexSymbol: displayName} for equity indices only
        equity_index_map: dict[str, str] = {}
        for item in all_indices_data:
            name = item.get("index", "")
            sym  = item.get("indexSymbol", "")
            if name and sym and _is_equity(name):
                equity_index_map[sym] = name

        # symbol → set of index display names
        symbol_indices: dict[str, set] = {}
        failed = []
        for idx_sym, idx_name in equity_index_map.items():
            try:
                # Inline URL construction — bypasses _resolve_index_symbol()
                # so list_indices() is NOT called again per index.
                url = (
                    "https://www.nseindia.com/api/NextApi/apiClient/marketWatchApi"
                    f"?functionName=getIndicesData&symbol={quote(idx_sym)}"
                )
                resp = self._session.get_raw(url)
                rows = resp.json().get("data", {}).get("data", [])
                for r in rows:
                    sym = r.get("symbol", "")
                    if sym and sym != idx_sym:  # first row is the index itself
                        symbol_indices.setdefault(sym, set()).add(idx_name)
            except Exception as e:
                failed.append({"index": idx_name, "error": str(e)})

        universe = [
            {
                "symbol": sym,
                "indices": sorted(idxs),
                "index_count": len(idxs),
            }
            for sym, idxs in sorted(symbol_indices.items())
        ]

        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({
            "last_updated": date.today().isoformat(),
            "total": len(universe),
            "indices_scanned": len(equity_index_map),
            "failed": failed,
            "universe": universe,
        }, indent=2))

        return universe

    # ------------------------------------------------------------------ #
    #  Constituent tracking                                               #
    # ------------------------------------------------------------------ #

    def snapshot_constituents(self, index: str) -> dict:
        """
        Fetch live constituents, save a dated snapshot, and return a diff
        vs the previous snapshot.

        Snapshots are stored in ~/.nselib/constituents/.
        The diff shows which symbols were added or removed since the last
        snapshot — empty lists mean no change.

        Returns:
            {
                "index":    "NIFTY 50",
                "date":     "2026-10-21",
                "symbols":  ["RELIANCE", "TCS", ...],   # current full list
                "added":    [...],                        # new since last run
                "removed":  [...],                        # gone since last run
                "first_run": True/False,
            }
        """
        # resolve to indexSymbol so the key and filtering are always consistent
        symbol   = self._resolve_index_symbol(index)
        key      = _index_key(symbol)
        today    = date.today().isoformat()
        _STORE.mkdir(parents=True, exist_ok=True)

        history_file  = _STORE / f"{key}_history.json"
        snapshot_file = _STORE / f"{key}_latest.json"

        # fetch live constituents (first record is the index itself — skip it)
        raw      = self.index_constituents(symbol)
        symbols  = sorted(
            r["symbol"] for r in raw
            if r.get("symbol") and r.get("symbol") != symbol
        )

        # load previous snapshot if it exists
        prev_symbols: list[str] = []
        first_run = not snapshot_file.exists()
        if not first_run:
            prev_symbols = json.loads(snapshot_file.read_text()).get("symbols", [])

        added   = sorted(set(symbols) - set(prev_symbols))
        removed = sorted(set(prev_symbols) - set(symbols))

        # save new snapshot
        snapshot = {"index": index, "date": today, "symbols": symbols}
        snapshot_file.write_text(json.dumps(snapshot, indent=2))

        # append to history only when something changed (or first run)
        if first_run or added or removed:
            history: list[dict] = []
            if history_file.exists():
                history = json.loads(history_file.read_text())
            history.append({
                "date":    today,
                "added":   added,
                "removed": removed,
                "total":   len(symbols),
            })
            # drop entries older than 1 year
            cutoff = (date.today() - timedelta(days=365)).isoformat()
            history = [e for e in history if e.get("date", "") >= cutoff]
            history_file.write_text(json.dumps(history, indent=2))

        return {
            "index":     index,
            "date":      today,
            "symbols":   symbols,
            "added":     added,
            "removed":   removed,
            "first_run": first_run,
        }

    def constituent_history(self, index: str) -> list[dict]:
        """
        Return the full changelog of constituent additions and removals
        for an index, oldest first.

        Each entry: {"date", "added": [...], "removed": [...], "total": N}

        Returns [] if snapshot_constituents() has never been called for
        this index.
        """
        symbol = self._resolve_index_symbol(index)
        history_file = _STORE / f"{_index_key(symbol)}_history.json"
        if not history_file.exists():
            return []
        return json.loads(history_file.read_text())

    def snapshot_all_constituents(self, workers: int = 4) -> dict[str, dict]:
        """
        Snapshot constituents for every index in UNIVERSE_INDICES.
        Resolves each entry to its indexSymbol via _resolve_index_symbol() so
        both display names and abbreviated symbols are accepted.

        Returns {indexSymbol: snapshot_result}. Indices that return an empty
        constituent list or fail are included with an "error" key instead.
        """
        import concurrent.futures

        # resolve each universe entry to its live indexSymbol
        live_data = self.list_indices().get("data", [])
        live_map: dict[str, str] = {}  # upper(display | symbol) -> indexSymbol
        for item in live_data:
            sym = item.get("indexSymbol", "")
            if sym:
                live_map[item.get("index", "").upper()] = sym
                live_map[sym.upper()] = sym

        universe_symbols: list[str] = []
        for name in UNIVERSE_INDICES:
            resolved = live_map.get(name.upper(), name)
            universe_symbols.append(resolved)

        results: dict[str, dict] = {}

        def _snap(symbol: str):
            try:
                result = self.snapshot_constituents(symbol)
                if not result.get("symbols"):
                    return symbol, {"index": symbol, "error": "no constituent data"}
                return symbol, result
            except Exception as exc:
                return symbol, {"index": symbol, "error": str(exc)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_snap, sym): sym for sym in universe_symbols}
            for fut in concurrent.futures.as_completed(futures):
                sym, result = fut.result()
                results[sym] = result
                changed = bool(result.get("added") or result.get("removed"))
                status  = "CHANGED" if changed else ("FIRST" if result.get("first_run") else "ok")
                err     = result.get("error", "")
                print(f"  [{status:7s}] {sym}" + (f" — {err}" if err else ""))

        self.build_comprehensive_latest()
        self.fetch_constituent_bhav_history()
        return results

    def build_comprehensive_latest(self) -> dict:
        """
        Aggregate all per-index _latest.json files into a single
        Comprehensive_latest.json, deduplicating by stock symbol.

        Each stock entry lists every index it belongs to, sorted alphabetically.
        The file is written to data/constituents/Comprehensive_latest.json.

        Returns the aggregate dict.
        """
        stock_indices: dict[str, list[str]] = {}

        for latest_file in sorted(_STORE.glob("*_latest.json")):
            try:
                snap = json.loads(latest_file.read_text())
            except Exception:
                continue
            index_name = snap.get("index", latest_file.stem.replace("_latest", ""))
            for symbol in snap.get("symbols", []):
                stock_indices.setdefault(symbol, []).append(index_name)

        # sort each stock's index list and the stocks themselves
        stocks = {
            sym: sorted(indices)
            for sym, indices in sorted(stock_indices.items())
        }

        out = {
            "date":          date.today().isoformat(),
            "total_indices": len({idx for idxs in stocks.values() for idx in idxs}),
            "total_stocks":  len(stocks),
            "stocks":        stocks,
        }

        _STORE.mkdir(parents=True, exist_ok=True)
        (_STORE / "Comprehensive_latest.json").write_text(json.dumps(out, indent=2))
        return out

    def fetch_constituent_bhav_history(self, workers: int = 4) -> Path:
        """
        Build/update a 1-year daily bhavcopy for every stock in
        Comprehensive_latest.json.

        - First run: downloads ~250 trading days of bhavcopy
        - Subsequent runs: downloads only days missing since the last update
        - Rows older than 365 days are pruned on every write

        Output: data/bhav/constituents_1y.csv
        Columns: TradDt, TckrSymb, SctySrs, OpnPric, HghPric, LwPric,
                 ClsPric, PrvsClsgPric, TtlTradgVol, TtlTrfVal,
                 TtlNbOfTxsExctd, ISIN, DELIV_QTY, DELIV_PER

        Returns the path to the CSV.
        """
        import concurrent.futures
        import tempfile
        from .downloads import _trading_days

        comp_file = _STORE / "Comprehensive_latest.json"
        if not comp_file.exists():
            raise NSEDataError(
                "Comprehensive_latest.json not found. "
                "Run build_comprehensive_latest() first."
            )

        symbols: set[str] = set(
            json.loads(comp_file.read_text()).get("stocks", {}).keys()
        )

        _BHAV_DIR.mkdir(parents=True, exist_ok=True)
        out_file = _BHAV_DIR / "constituents_1y.csv"

        # load existing rows; find last date and which symbols are already present
        existing: list[dict] = []
        last_saved: str = ""
        existing_symbols: set[str] = set()
        if out_file.exists():
            with open(out_file, newline="") as f:
                existing = list(csv.DictReader(f))
            if existing:
                last_saved = max(r["TradDt"] for r in existing)
                existing_symbols = {r["TckrSymb"] for r in existing}
                # backfill delivery columns for rows that predate this feature
                for r in existing:
                    r.setdefault("DELIV_QTY", "")
                    r.setdefault("DELIV_PER", "")

        to_date = date.today() - timedelta(days=1)
        cutoff  = (date.today() - timedelta(days=365)).isoformat()

        # stocks new to the universe since the last CSV update need a full-year backfill
        new_symbols = symbols - existing_symbols

        # incremental range for all known stocks
        from_date = (
            date.fromisoformat(last_saved) + timedelta(days=1)
            if last_saved
            else to_date - timedelta(days=365)
        )

        incremental_days = _trading_days(from_date, to_date)
        backfill_days    = (
            _trading_days(to_date - timedelta(days=365), to_date)
            if new_symbols else []
        )

        if not incremental_days and not backfill_days:
            print("  bhav: already up to date")
            return out_file

        if incremental_days:
            print(f"  bhav: incremental {len(incremental_days)} day(s) "
                  f"({from_date} → {to_date}) for {len(symbols)} stocks …")
        if new_symbols:
            print(f"  bhav: backfilling {len(new_symbols)} new stock(s) "
                  f"over {len(backfill_days)} day(s) …")

        new_rows: list[dict] = []

        def _fetch_day(dt: date, filter_symbols: set[str]):
            tmp = Path(tempfile.mkdtemp())
            try:
                csv_path = self.equity_bhavcopy(dt=dt, folder=tmp)

                # Try to get delivery data; silently skip if unavailable
                delivery: dict[tuple[str, str], tuple[str, str]] = {}
                got_delivery = False
                try:
                    mto_path = self.delivery_bhavcopy(dt=dt, folder=tmp)
                    delivery = _parse_mto(mto_path)
                    got_delivery = bool(delivery)
                    mto_path.unlink(missing_ok=True)
                except Exception:
                    pass

                rows = []
                with open(csv_path, newline="", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        sym    = row.get("TckrSymb", "").strip()
                        series = row.get("SctySrs",  "").strip()
                        if sym in filter_symbols and series in _EQ_SERIES:
                            r = {c: row.get(c, "") for c in _BHAV_COLS}
                            dq, dp = delivery.get((sym, series), ("", ""))
                            r["DELIV_QTY"] = dq
                            r["DELIV_PER"] = dp
                            rows.append(r)
                csv_path.unlink(missing_ok=True)
                return dt, rows, None, got_delivery
            except Exception as exc:
                return dt, [], exc, False
            finally:
                import shutil; shutil.rmtree(tmp, ignore_errors=True)

        ok = err = deliv_ok = deliv_miss = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for d in incremental_days:
                futures[pool.submit(_fetch_day, d, symbols)] = d
            # backfill days that aren't already covered by incremental
            incremental_set = set(incremental_days)
            for d in backfill_days:
                if d not in incremental_set:
                    futures[pool.submit(_fetch_day, d, new_symbols)] = d
            for fut in concurrent.futures.as_completed(futures):
                dt, rows, exc, got_delivery = fut.result()
                if exc:
                    err += 1
                else:
                    new_rows.extend(rows)
                    ok += 1
                    if got_delivery:
                        deliv_ok += 1
                    else:
                        deliv_miss += 1

        print(f"  bhav: {ok} days ok, {err} failed (holidays/closures) | "
              f"delivery: {deliv_ok} days with MTO, {deliv_miss} without")

        if not new_rows and not existing:
            return out_file

        # merge, deduplicate, prune >365 days, sort
        all_rows = [r for r in existing if r.get("TradDt", "") >= cutoff]
        all_rows.extend(new_rows)

        # deduplicate by (TradDt, TckrSymb)
        seen: set[tuple] = set()
        deduped: list[dict] = []
        for r in all_rows:
            key = (r["TradDt"], r["TckrSymb"])
            if key not in seen:
                seen.add(key)
                deduped.append(r)

        deduped.sort(key=lambda r: (r["TradDt"], r["TckrSymb"]))

        with open(out_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_BHAV_COLS)
            writer.writeheader()
            writer.writerows(deduped)

        print(f"  bhav: {len(deduped):,} rows saved → {out_file}")
        return out_file

    def constituent_changes(self, index: str) -> dict:
        """
        Return only the most recent change record for an index.
        Useful for a quick check: did anything change since last run?

        Returns {} if no history exists yet.
        """
        history = self.constituent_history(index)
        return history[-1] if history else {}

    def fetch_index_names(self) -> dict[str, str]:
        """Return {short_name: full_name} mapping for all indices."""
        data = self.list_indices()
        return {
            item.get("indexSymbol", ""): item.get("index", "")
            for item in data.get("data", [])
        }

    def fetch_historical_index_data(
        self,
        index:     str,
        from_date: date | None = None,
        to_date:   date | None = None,
    ) -> list[dict]:
        """
        Daily OHLC + turnover for an index. Ranges >1 year are
        automatically chunked. Returns oldest-first.
        """
        to_date   = to_date   or date.today()
        from_date = from_date or (to_date - timedelta(days=30))

        rows: list[dict] = []
        for start, end in _chunk_dates(from_date, to_date, days=364):
            resp = self._session.get(
                "historicalOR/indicesHistory",
                params={
                    "indexType": index,
                    "from":      _fmt(start),
                    "to":        _fmt(end),
                },
            )
            chunk = resp.get("data", resp) if isinstance(resp, dict) else resp
            if isinstance(chunk, list):
                rows.extend(chunk)

        rows.sort(key=lambda r: r.get("HistoricalDate", r.get("Date", "")))
        return rows


    def get_index_names(self):
        """List of all NSE index names grouped by category."""
        path = "index-names"
        return (self._guardian.guarded_get if self._guardian else self._session.get)(path, None)
