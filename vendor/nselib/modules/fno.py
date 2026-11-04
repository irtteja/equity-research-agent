import csv as _csv
import io as _io
from datetime import date, timedelta
from typing import Any

from ..exceptions import NSEDataError

_NEXTAPI  = "NextApi/apiClient/GetQuoteApi"
_ARCHIVE  = "https://nsearchives.nseindia.com"
_LOTS_URL = f"{_ARCHIVE}/content/fo/fo_mktlots.csv"

_INDEX_SYMS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}


class FnoMixin:
    """F&O: option chains, expiries, lot sizes, underlying list."""

    def option_chain(self, symbol: str) -> dict:
        """
        Full option chain for an index or stock.
        Use 'NIFTY', 'BANKNIFTY', 'FINNIFTY' for indices,
        or any F&O-eligible equity symbol.
        Returns {'data': [...], 'expiries': [...], 'strikes': [...], 'timestamp': ...}
        """
        sym = symbol.upper()
        raw = self._session.get(
            _NEXTAPI,
            params={"functionName": "getSymbolDerivativesData", "symbol": sym},
        )
        expiry_raw = self._session.get(
            _NEXTAPI,
            params={"functionName": "getOptionChainDropdown", "symbol": sym},
        )
        return {
            "data":      raw.get("data", []),
            "expiries":  expiry_raw.get("expiryDates", []),
            "strikes":   expiry_raw.get("strikePrice", []),
            "timestamp": raw.get("timestamp", ""),
        }

    def index_option_chain(self, symbol: str) -> dict:
        """Option chain specifically for index derivatives (NIFTY, BANKNIFTY…)."""
        return self.option_chain(symbol)

    def equity_option_chain(self, symbol: str) -> dict:
        """Option chain for stock options."""
        return self.option_chain(symbol)

    def compile_option_chain(self, symbol: str, expiry: str | None = None) -> list[dict]:
        """
        Flatten raw option chain into a list of strike rows, each with
        CE and PE data side-by-side plus the underlying price.

        expiry: 'DD-Mon-YYYY' (e.g. '27-Mar-2025').
                Defaults to the nearest expiry if omitted.
        """
        raw     = self.option_chain(symbol)
        records = raw.get("data", [])
        expiries = raw.get("expiries", [])

        if not records:
            raise NSEDataError(f"No option chain data returned for {symbol!r}")

        chosen = expiry or (expiries[0] if expiries else None)

        # Group CE and PE rows by (strike, expiry)
        by_strike: dict[tuple, dict] = {}
        underlying_value: float | None = None

        for rec in records:
            exp = rec.get("expiryDate", "")
            if chosen and exp.upper() != chosen.upper():
                continue

            raw_strike = rec.get("strikePrice")
            strike = float(str(raw_strike).strip()) if raw_strike is not None else None
            otype  = rec.get("optionType", "").upper()
            key    = (strike, exp)

            if key not in by_strike:
                by_strike[key] = {"strike": strike, "expiry": exp}

            if otype == "CE":
                by_strike[key].update({
                    "ce_ltp":    rec.get("lastPrice"),
                    "ce_oi":     rec.get("openInterest"),
                    "ce_chg_oi": rec.get("changeinOpenInterest"),
                    "ce_volume": rec.get("totalTradedVolume"),
                    "ce_iv":     rec.get("impliedVolatility") if "impliedVolatility" in rec else None,
                })
            elif otype == "PE":
                by_strike[key].update({
                    "pe_ltp":    rec.get("lastPrice"),
                    "pe_oi":     rec.get("openInterest"),
                    "pe_chg_oi": rec.get("changeinOpenInterest"),
                    "pe_volume": rec.get("totalTradedVolume"),
                    "pe_iv":     rec.get("impliedVolatility") if "impliedVolatility" in rec else None,
                })

            if underlying_value is None:
                underlying_value = rec.get("underlyingValue")

        rows = sorted(by_strike.values(), key=lambda r: r["strike"] or 0)
        if underlying_value is not None:
            for r in rows:
                r["underlying"] = underlying_value
        return rows

    def max_pain(self, symbol: str, expiry: str | None = None) -> dict:
        """
        Max pain strike — the strike where total option buyer loss is maximum.
        Returns {'strike': float, 'expiry': str, 'total_pain': float}.
        """
        chain = self.compile_option_chain(symbol, expiry)
        if not chain:
            raise NSEDataError(f"Empty chain for {symbol!r}")

        strikes = [r["strike"] for r in chain if r["strike"] is not None]
        best_strike: float | None = None
        min_pain = float("inf")

        for test_strike in strikes:
            pain = 0.0
            for row in chain:
                s = row["strike"]
                if s is None:
                    continue
                ce_oi = row.get("ce_oi") or 0
                pe_oi = row.get("pe_oi") or 0
                if test_strike > s:
                    pain += (test_strike - s) * ce_oi
                elif test_strike < s:
                    pain += (s - test_strike) * pe_oi
            if pain < min_pain:
                min_pain = pain
                best_strike = test_strike

        return {
            "strike":     best_strike,
            "expiry":     chain[0].get("expiry"),
            "total_pain": min_pain,
        }

    def get_futures_expiry(self, symbol: str) -> list[str]:
        """List of active expiry dates for a futures symbol."""
        raw = self._session.get(
            _NEXTAPI,
            params={"functionName": "getOptionChainDropdown", "symbol": symbol.upper()},
        )
        return raw.get("expiryDates", [])

    def fno_lots(self) -> dict[str, int]:
        """Current lot sizes for all F&O symbols (first active expiry)."""
        resp = self._session.get_raw(_LOTS_URL)
        reader = _csv.DictReader(_io.StringIO(resp.text))
        result: dict[str, int] = {}
        skip = {"UNDERLYING", "SYMBOL"}
        for row in reader:
            # CSV headers are whitespace-padded — strip all keys
            clean = {k.strip(): v.strip() for k, v in row.items()}
            sym = clean.get("SYMBOL", "")
            if not sym:
                continue
            # Take first non-empty expiry column as the current lot size
            for k, v in clean.items():
                if k not in skip and v:
                    try:
                        result[sym] = int(v)
                    except ValueError:
                        pass
                    break
        return result

    def fetch_fno_underlying(self) -> list[dict]:
        """All F&O eligible underlyings with symbol and lot size."""
        lots = self.fno_lots()
        return [{"symbol": sym, "lot_size": size} for sym, size in lots.items()]

    def futures_quote(self, symbol: str) -> dict:
        """Live derivatives data for an index or stock (all expiries)."""
        raw = self._session.get(
            _NEXTAPI,
            params={"functionName": "getSymbolDerivativesData", "symbol": symbol.upper()},
        )
        return raw

    def get_oi_spurts_underlyings(self):
        """Open interest spurts aggregated by underlying."""
        path = "live-analysis-oi-spurts-underlyings"
        return (self._guardian.guarded_get if self._guardian else self._session.get)(path, None)

    def get_oi_spurts_contracts(self):
        """Open interest spurts by individual derivative contract."""
        path = "live-analysis-oi-spurts-contracts"
        return (self._guardian.guarded_get if self._guardian else self._session.get)(path, None)

    def get_derivatives_equity_snapshot(self, index="futures"):
        """Equity derivatives snapshot. index: 'futures' or 'options'."""
        path = "snapshot-derivatives-equity"
        params = {"index": index}
        return (self._guardian.guarded_get if self._guardian else self._session.get)(path, params)

    def snapshot_derivatives_equity(self, symbol: str):
        """Fetch the derivatives snapshot (futures/options) for an equity symbol."""
        return (self._guardian.guarded_get if self._guardian else self._session.get)("snapshot-derivatives-equity", {"symbol": symbol.upper()})

    def get_snapshot_derivatives_equity(self, index: str = "futures"):
        """Equity derivatives snapshot. index: 'futures' or 'options'."""
        params = {"index": index}
        return (self._guardian.guarded_get if self._guardian else self._session.get)("snapshot-derivatives-equity", params)

    # ------------------------------------------------------------------ #
    #  Archive-based data (works from any IP)                              #
    # ------------------------------------------------------------------ #

    def fno_ban_list(self, trade_date: date | None = None) -> list[str]:
        """
        F&O securities in ban period for a given trade date.
        Defaults to most recent available trading day.
        Returns list of symbols currently banned (exceeded 95% MWPL).
        """
        from datetime import timedelta
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = (
                f"{_ARCHIVE}/archives/fo/sec_ban/fo_secban_{check.strftime('%d%m%Y')}.csv"
            )
            try:
                r = self._session._s.get(url, timeout=8)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    symbols = []
                    for line in r.text.splitlines()[1:]:
                        parts = line.strip().split(",")
                        if len(parts) >= 2 and parts[1].strip():
                            symbols.append(parts[1].strip())
                    return symbols
            except Exception:
                continue
        return []

    def participant_oi(self, trade_date: date | None = None) -> list[dict]:
        """
        Participant-wise Open Interest in equity derivatives (FII/DII/Client/Pro).
        Defaults to most recent available trading day.

        Each dict: participant, future_index_long, future_index_short,
          future_stock_long, future_stock_short,
          option_index_call_long, option_index_put_long,
          option_index_call_short, option_index_put_short,
          option_stock_call_long, option_stock_put_long,
          option_stock_call_short, option_stock_put_short,
          total_long, total_short
        """
        import csv as _csv2, io as _io2
        from datetime import timedelta
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = (
                f"{_ARCHIVE}/content/nsccl/"
                f"fao_participant_oi_{check.strftime('%d%m%Y')}.csv"
            )
            try:
                r = self._session._s.get(url, timeout=8)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    reader = _csv2.reader(_io2.StringIO(r.text))
                    rows = list(reader)
                    # row[0] = header description, row[1] = column names, rows[2:] = data
                    data_rows = [row for row in rows[2:] if row and row[0].strip() and row[0].strip() != "TOTAL"]
                    keys = [
                        "participant",
                        "future_index_long", "future_index_short",
                        "future_stock_long", "future_stock_short",
                        "option_index_call_long", "option_index_put_long",
                        "option_index_call_short", "option_index_put_short",
                        "option_stock_call_long", "option_stock_put_long",
                        "option_stock_call_short", "option_stock_put_short",
                        "total_long", "total_short",
                    ]
                    out = []
                    for row in data_rows:
                        if len(row) < 15:
                            continue
                        rec = {keys[i]: row[i].strip() for i in range(len(keys))}
                        for k in keys[1:]:
                            try:
                                rec[k] = int(rec[k].replace(",", ""))
                            except ValueError:
                                pass
                        out.append(rec)
                    return out
            except Exception:
                continue
        return []

    def participant_volume(self, trade_date: date | None = None) -> list[dict]:
        """
        Participant-wise trading volume in equity derivatives (FII/DII/Client/Pro).
        Same structure as participant_oi() but shows contracts traded (not OI).
        """
        import csv as _csv2, io as _io2
        from datetime import timedelta
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = (
                f"{_ARCHIVE}/content/nsccl/"
                f"fao_participant_vol_{check.strftime('%d%m%Y')}.csv"
            )
            try:
                r = self._session._s.get(url, timeout=8)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    reader = _csv2.reader(_io2.StringIO(r.text))
                    rows = list(reader)
                    data_rows = [row for row in rows[2:] if row and row[0].strip() and row[0].strip() != "TOTAL"]
                    keys = [
                        "participant",
                        "future_index_long", "future_index_short",
                        "future_stock_long", "future_stock_short",
                        "option_index_call_long", "option_index_put_long",
                        "option_index_call_short", "option_index_put_short",
                        "option_stock_call_long", "option_stock_put_long",
                        "option_stock_call_short", "option_stock_put_short",
                        "total_long", "total_short",
                    ]
                    out = []
                    for row in data_rows:
                        if len(row) < 15:
                            continue
                        rec = {keys[i]: row[i].strip() for i in range(len(keys))}
                        for k in keys[1:]:
                            try:
                                rec[k] = int(rec[k].replace(",", ""))
                            except ValueError:
                                pass
                        out.append(rec)
                    return out
            except Exception:
                continue
        return []

    def pcr(self, symbol: str, expiry: str | None = None) -> dict:
        """
        Put-Call Ratio for an index or stock options expiry.
        expiry: DD-Mon-YYYY e.g. '27-Nov-2025'. Defaults to nearest expiry.
        Returns: { expiry, total_ce_oi, total_pe_oi, pcr, symbol }
        """
        chain = self.compile_option_chain(symbol, expiry)
        if not chain:
            from ..exceptions import NSEDataError
            raise NSEDataError(f"No option chain data for {symbol}")
        expiry_used = next((r.get("expiry") for r in chain if r.get("expiry")), expiry or "")
        ce_oi = sum(r.get("ce_oi", 0) or 0 for r in chain)
        pe_oi = sum(r.get("pe_oi", 0) or 0 for r in chain)
        return {
            "symbol":       symbol.upper(),
            "expiry":       expiry_used,
            "total_ce_oi":  ce_oi,
            "total_pe_oi":  pe_oi,
            "pcr":          round(pe_oi / ce_oi, 3) if ce_oi else None,
        }

    def mwpl_oi(self, trade_date: date | None = None) -> list[dict]:
        """
        Market-Wide Position Limit (MWPL) utilization — NCL Open Interest.
        Stocks shown are those near or above MWPL (candidates for F&O ban).

        Each dict: date, isin, scrip_name, symbol, mwpl, ncl_oi, ncl_futeq_oi

        Published after market close. Defaults to most recent available trading day.
        """
        import csv as _csv2, io as _io2
        from datetime import timedelta
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = (
                f"{_ARCHIVE}/archives/nsccl/mwpl/"
                f"ncloi_{check.strftime('%d%m%Y')}.zip"
            )
            try:
                import zipfile as _zf, io as _zipio
                r = self._session._s.get(url, timeout=10)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    with _zf.ZipFile(_zipio.BytesIO(r.content)) as zf:
                        csv_file = next(n for n in zf.namelist() if n.endswith(".csv"))
                        text = zf.read(csv_file).decode("utf-8", errors="replace")
                    reader = _csv2.DictReader(_io2.StringIO(text))
                    out = []
                    for row in reader:
                        clean = {k.strip(): v.strip() for k, v in row.items()}
                        try:
                            out.append({
                                "date":         clean.get("Date", ""),
                                "isin":         clean.get("ISIN", ""),
                                "scrip_name":   clean.get("Scrip Name", ""),
                                "symbol":       clean.get("NSE Symbol", ""),
                                "mwpl":         float(clean.get("MWPL", 0) or 0),
                                "ncl_oi":       float(clean.get("NCL Open Interest", "0").replace(",", "") or 0),
                                "ncl_futeq_oi": float(clean.get("NCL FutEq OI", "0").replace(",", "") or 0),
                            })
                        except (ValueError, KeyError):
                            continue
                    return out
            except Exception:
                continue
        return []

    def combined_oi(self, trade_date: date | None = None) -> list[dict]:

        """
        Combined Open Interest across NSE + BSE for F&O eligible stocks.
        Shows MWPL utilization and whether fresh positions are blocked.

        Each dict: date, isin, scrip_name, symbol, mwpl, open_interest,
                   futeq_oi, limit_next_day ('No Fresh Positions' or '')

        Published after market close. Defaults to most recent available trading day.
        """
        import csv as _csv2, io as _io2
        from datetime import timedelta
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = (
                f"{_ARCHIVE}/archives/nsccl/mwpl/"
                f"combineoi_{check.strftime('%d%m%Y')}.zip"
            )
            try:
                import zipfile as _zf, io as _zipio
                r = self._session._s.get(url, timeout=10)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    with _zf.ZipFile(_zipio.BytesIO(r.content)) as zf:
                        csv_file = next(n for n in zf.namelist() if n.endswith(".csv"))
                        text = zf.read(csv_file).decode("utf-8", errors="replace")
                    reader = _csv2.DictReader(_io2.StringIO(text))
                    out = []
                    for row in reader:
                        clean = {k.strip(): v.strip() for k, v in row.items()}
                        try:
                            out.append({
                                "date":           clean.get("Date", ""),
                                "isin":           clean.get("ISIN", ""),
                                "scrip_name":     clean.get("Scrip Name", ""),
                                "symbol":         clean.get("NSE Symbol", ""),
                                "mwpl":           float(clean.get("MWPL", 0) or 0),
                                "open_interest":  float(clean.get("Open Interest", "0").replace(",", "") or 0),
                                "futeq_oi":       float(clean.get("Future Equivalent Open Interest", "0").replace(",", "") or 0),
                                "limit_next_day": clean.get("Limit for Next Day", ""),
                            })
                        except (ValueError, KeyError):
                            continue
                    return out
            except Exception:
                continue
        return []

    # ------------------------------------------------------------------ #
    #  F&O segment daily report files                                     #
    # ------------------------------------------------------------------ #

    def fo_settlement_prices(self, trade_date: date | None = None) -> list[dict]:
        """Daily MTM settlement prices for all F&O contracts.

        Returns: [{date, instrument, underlying, expiry, settlement_price}]
        """
        import csv as _c, io as _i
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = f"{_ARCHIVE}/archives/nsccl/sett/FOSett_prce_{check.strftime('%d%m%Y')}.csv"
            try:
                r = self._session._s.get(url, timeout=15)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    reader = _c.DictReader(_i.StringIO(r.text))
                    out = []
                    for row in reader:
                        exp = (row.get("EXPIRY DATE") or "").strip()
                        if not exp:
                            continue
                        try:
                            out.append({
                                "date":             (row.get("DATE") or "").strip(),
                                "instrument":       (row.get("INSTRUMENT") or "").strip(),
                                "underlying":       (row.get("UNDERLYING") or "").strip(),
                                "expiry":           exp,
                                "settlement_price": float((row.get("MTM SETTLEMENT PRICE") or "0").strip() or 0),
                            })
                        except (ValueError, TypeError):
                            continue
                    return out
            except Exception:
                continue
        return []

    def fo_daily_volatility(self, trade_date: date | None = None) -> list[dict]:
        """EWMA daily and annualised volatility for F&O underlying stocks.

        Returns: [{symbol, close, prev_close, log_return, prev_daily_vol,
                   current_daily_vol, annualised_vol}]
        """
        import csv as _c, io as _i
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = f"{_ARCHIVE}/archives/nsccl/volt/FOVOLT_{check.strftime('%d%m%Y')}.csv"
            try:
                r = self._session._s.get(url, timeout=15)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    reader = _c.DictReader(_i.StringIO(r.text))
                    cols = None
                    out = []
                    for row in reader:
                        if cols is None:
                            cols = list(row.keys())
                        sym = (row.get(cols[1]) if len(cols) > 1 else "").strip()
                        if not sym:
                            continue
                        def _f(idx, _cols=cols, _row=row):
                            k = _cols[idx] if idx < len(_cols) else ""
                            return float((_row.get(k) or "0").strip() or 0)
                        try:
                            out.append({
                                "symbol":            sym,
                                "close":             _f(2),
                                "prev_close":        _f(3),
                                "log_return":        _f(4),
                                "prev_daily_vol":    _f(5),
                                "current_daily_vol": _f(6),
                                "annualised_vol":    _f(7),
                            })
                        except (ValueError, IndexError):
                            continue
                    return out
            except Exception:
                continue
        return []

    def fo_exposure_limits(self, trade_date: date | None = None) -> list[dict]:
        """ELM (Extreme Loss Margin) and additional margin rates for F&O symbols.

        Returns: [{symbol, instrument_type, normal_elm_pct, additional_elm_pct, total_elm_pct}]
        """
        import csv as _c, io as _i
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = f"{_ARCHIVE}/archives/exp_lim/ael_{check.strftime('%d%m%Y')}.csv"
            try:
                r = self._session._s.get(url, timeout=15)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    reader = _c.DictReader(_i.StringIO(r.text))
                    out = []
                    for row in reader:
                        sym = (row.get("Symbol") or "").strip()
                        if not sym:
                            continue
                        def _f(k, _row=row): return float((_row.get(k) or "0").strip() or 0)
                        add_key = next((k for k in row if "Additional" in k), "")
                        tot_key = next((k for k in row if "Total" in k), "")
                        out.append({
                            "symbol":             sym,
                            "instrument_type":    (row.get("Instrument Type") or "").strip(),
                            "normal_elm_pct":     _f("Normal ELM Margin%"),
                            "additional_elm_pct": _f(add_key),
                            "total_elm_pct":      _f(tot_key),
                        })
                    return out
            except Exception:
                continue
        return []

    def fo_delta_equivalent_oi(self, trade_date: date | None = None) -> list[dict]:
        """Combined delta-equivalent Open Interest across NSE + BSE for F&O stocks.

        Returns: [{date, isin, scrip_name, symbol, notional_oi, delta_eq_oi}]
        """
        import csv as _c, io as _i
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = f"{_ARCHIVE}/archives/nsccl/mwpl/combineoi_deleq_{check.strftime('%d%m%Y')}.csv"
            try:
                r = self._session._s.get(url, timeout=15)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    reader = _c.DictReader(_i.StringIO(r.text))
                    out = []
                    for row in reader:
                        sym = (row.get("Symbol") or "").strip()
                        if not sym:
                            continue
                        def _f(k, _row=row): return float((_row.get(k) or "0").replace(",", "") or 0)
                        out.append({
                            "date":        (row.get("Date") or "").strip(),
                            "isin":        (row.get("ISIN") or "").strip(),
                            "scrip_name":  (row.get("Scrip Name") or "").strip(),
                            "symbol":      sym,
                            "notional_oi": _f("Notional Open Interest"),
                            "delta_eq_oi": _f("Portfolio-wise Futures Equivalent Open Interest"),
                        })
                    return out
            except Exception:
                continue
        return []

    def fo_contract_delta(self, trade_date: date | None = None) -> list[dict]:
        """NCL delta factors per F&O contract for margin computation.

        Returns: [{date, symbol, expiry, strike_price, option_type, delta_factor}]
        """
        import csv as _c, io as _i
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = f"{_ARCHIVE}/content/nsccl/Contract_Delta_{check.strftime('%d%m%Y')}.csv"
            try:
                r = self._session._s.get(url, timeout=15)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    reader = _c.DictReader(_i.StringIO(r.text))
                    out = []
                    for row in reader:
                        sym = (row.get("Symbol") or "").strip()
                        if not sym:
                            continue
                        try:
                            out.append({
                                "date":         (row.get("Date") or "").strip(),
                                "symbol":       sym,
                                "expiry":       (row.get("Expiry day") or "").strip(),
                                "strike_price": float((row.get("Strike Price") or "0").strip() or 0),
                                "option_type":  (row.get("Option Type") or "").strip(),
                                "delta_factor": float((row.get("Delta Factor") or "0").strip() or 0),
                            })
                        except (ValueError, TypeError):
                            continue
                    return out
            except Exception:
                continue
        return []

    def fo_top10_clearing_members(self, trade_date: date | None = None) -> list[dict]:
        """Top 10 clearing members by volume in equity derivatives.

        Returns: [{rank, index_futures_vol, index_futures_trnvr, stock_futures_vol,
                   stock_futures_trnvr, index_options_vol, stock_options_vol, total_vol, total_trnvr}]
        """
        import csv as _c, io as _i
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = f"{_ARCHIVE}/content/nsccl/fao_top10cm_to_{check.strftime('%d%m%Y')}.csv"
            try:
                r = self._session._s.get(url, timeout=15)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    lines = [l for l in r.text.splitlines() if l.strip()]
                    if len(lines) < 2:
                        continue
                    reader = _c.DictReader(_i.StringIO("\n".join(lines[1:])))
                    out = []
                    rank = 0
                    for row in reader:
                        rank += 1
                        def _f(k, _row=row):
                            v = (_row.get(k) or "0").strip().replace(",", "")
                            try: return float(v)
                            except: return 0.0
                        out.append({
                            "rank":                rank,
                            "index_futures_vol":   _f("Indx Futures Vol(Contracts)"),
                            "index_futures_trnvr": _f("Index Futures Trnvr(Crores)"),
                            "stock_futures_vol":   _f("Stock Futures Vol(Contracts)"),
                            "stock_futures_trnvr": _f("Stock Futures Trnvr(Crores)"),
                            "index_options_vol":   _f(next((k for k in row if "Index Options Vol" in k), "")),
                            "index_options_trnvr": _f(next((k for k in row if "Index Options Trnvr" in k), "")),
                            "stock_options_vol":   _f(next((k for k in row if "Stock Options Vol" in k), "")),
                            "stock_options_trnvr": _f(next((k for k in row if "Stock Options Trnvr" in k), "")),
                            "total_vol":           _f(next((k for k in row if "Total Vol" in k), "")),
                            "total_trnvr":         _f(next((k for k in row if "Total Trnvr" in k), "")),
                        })
                    return out
            except Exception:
                continue
        return []

    def fo_index_position_limits(self, trade_date: date | None = None) -> list[dict]:
        """Index derivatives position limits per category (FPI, MF, TM, clients).

        Returns: [{index, instrument, qty_limit, category}]
        """
        import csv as _c, io as _i
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = f"{_ARCHIVE}/content/fo/Index_deriv_pos_limit_{check.strftime('%d%m%Y')}.csv"
            try:
                r = self._session._s.get(url, timeout=15)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    reader = _c.DictReader(_i.StringIO(r.text))
                    out = []
                    for row in reader:
                        idx = (row.get("index") or "").strip()
                        if not idx:
                            continue
                        try:
                            out.append({
                                "index":      idx,
                                "instrument": (row.get("instrument") or "").strip(),
                                "qty_limit":  int(float((row.get("qty_limit") or "0").strip() or 0)),
                                "category":   (row.get("category") or "").strip(),
                            })
                        except (ValueError, TypeError):
                            continue
                    return out
            except Exception:
                continue
        return []

    def fo_market_activity(self, trade_date: date | None = None) -> dict:
        """F&O Market Activity Report — complete futures and options bhavcopy.

        Returns: {date, futures: [{instrument, symbol, expiry, open, high, low, close,
                                   oi, traded_value, traded_qty, num_contracts, num_trades}],
                         options: [{..., strike, option_type, notional_value, premium_value}]}
        """
        import csv as _c, io as _i, zipfile as _zf
        d = trade_date or date.today()
        for offset in range(5):
            check = d - timedelta(days=offset)
            url = f"{_ARCHIVE}/archives/fo/mkt/fo{check.strftime('%d%m%Y')}.zip"
            try:
                r = self._session._s.get(url, timeout=30)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    ds = check.strftime('%d%m%Y')
                    with _zf.ZipFile(_i.BytesIO(r.content)) as zf:
                        def _read_csv(fname):
                            try:
                                text = zf.read(fname).decode("utf-8", errors="replace")
                                return list(_c.DictReader(_i.StringIO(text)))
                            except Exception:
                                return []

                        def _clean(rows, renames):
                            out = []
                            for row in rows:
                                # Strip whitespace from keys AND values; coerce None to ""
                                c = {k.strip(): (v.strip() if isinstance(v, str) else (v or ""))
                                     for k, v in row.items() if k and k.strip()}
                                c = {renames.get(k, k): v for k, v in c.items()}
                                if c.get("symbol", ""):
                                    out.append(c)
                            return out

                        fut_renames = {
                            "INSTRUMENT": "instrument", "SYMBOL": "symbol",
                            "EXP_DATE": "expiry", "OPEN_PRICE": "open",
                            "HI_PRICE": "high", "LO_PRICE": "low",
                            "CLOSE_PRICE": "close", "OPEN_INT*": "oi",
                            "TRD_VAL": "traded_value", "TRD_QTY": "traded_qty",
                            "NO_OF_CONT": "num_contracts", "NO_OF_TRADE": "num_trades",
                        }
                        opt_renames = dict(fut_renames)
                        opt_renames.update({
                            "STR_PRICE": "strike", "OPT_TYPE": "option_type",
                            "NOTION_VAL": "notional_value", "PR_VAL": "premium_value",
                        })
                        return {
                            "date":    check.isoformat(),
                            "futures": _clean(_read_csv(f"fo{ds}.csv"), fut_renames),
                            "options": _clean(_read_csv(f"op{ds}.csv"), opt_renames),
                        }
            except Exception:
                continue
        return {"date": None, "futures": [], "options": []}

    def fo_latency_statistics(self, trade_date: date | None = None) -> list[dict]:
        """FO segment order latency percentiles (microseconds).

        Uses daily-reports API to resolve the exact filename (published for last
        trading day). Returns one row per segment with latency stats.
        """
        import csv as _c, io as _i
        base = trade_date or (date.today() - timedelta(days=1))
        for offset in range(5):
            check = base - timedelta(days=offset)
            if check.weekday() >= 5:
                continue
            ds = check.strftime("%d%m%Y")
            url = f"{_ARCHIVE}/content/FO_Latency_stats{ds}.csv"
            try:
                r = self._session._s.get(url, timeout=15)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    lines = [l for l in r.text.splitlines() if l.strip()]
                    if len(lines) < 2:
                        continue
                    reader = _c.DictReader(_i.StringIO("\n".join(lines[1:])))
                    out = []
                    for row in reader:
                        seg = (row.get("Segment") or "").strip()
                        if not seg or len(seg) > 10:
                            continue
                        out.append({
                            "segment":            seg,
                            "min_us":             (row.get("Min") or "").strip(),
                            "mean_us":            (row.get("Mean") or "").strip(),
                            "p50_us":             (row.get("50%ile") or "").strip(),
                            "p90_us":             (row.get("90%ile") or "").strip(),
                            "p99_us":             (row.get("99%ile") or "").strip(),
                            "max_us":             (row.get("Max") or "").strip(),
                            "order_messages":     (row.get("No. of Order Messages (Million msgs)") or "").strip(),
                        })
                    if out:
                        return out
            except Exception:
                continue
        return []

    def fo_client_oi_limits(self, trade_date: date | None = None) -> list[dict]:
        """Client-wise position limits for F&O stocks (contracts per client per exchange).

        Returns [{symbol, client_limit, exchange}] — shows the maximum net OI a
        single client may hold in that stock's futures/options.
        """
        import csv as _c, io as _i
        base = trade_date or date.today()
        for offset in range(5):
            check = base - timedelta(days=offset)
            if check.weekday() >= 5:
                continue
            ds = check.strftime("%d-%b-%Y").upper()
            url = f"{_ARCHIVE}/content/nsccl/oi_cli_limit_{ds}.lst"
            try:
                r = self._session._s.get(url, timeout=15)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    reader = _c.DictReader(_i.StringIO(r.text))
                    out = []
                    for row in reader:
                        sym = (row.get("SYMBOL") or "").strip()
                        if not sym:
                            continue
                        out.append({
                            "symbol":       sym,
                            "client_limit": (row.get("CLIENT_LIMIT") or "").strip(),
                            "exchange":     (row.get("EXCHANGE") or "").strip(),
                        })
                    if out:
                        return out
            except Exception:
                continue
        return []

    def fo_contracts(self, trade_date: date | None = None) -> list[dict]:
        """NSE F&O contract master file — all active contracts with lot size, ISIN, expiry.

        Returns key fields from the MII contract file (NSE-exclusive contracts).
        ~5000+ rows covering all futures and options contracts.
        """
        import gzip as _gz, csv as _c, io as _i
        base = trade_date or date.today()
        for offset in range(5):
            check = base - timedelta(days=offset)
            if check.weekday() >= 5:
                continue
            ds = check.strftime("%d%m%Y")
            url = f"{_ARCHIVE}/content/fo/NSE_FO_contract_{ds}.csv.gz"
            try:
                r = self._session._s.get(url, timeout=60)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    with _gz.open(_i.BytesIO(r.content)) as f:
                        text = f.read().decode("utf-8", errors="replace")
                    reader = _c.DictReader(_i.StringIO(text))
                    out = []
                    for row in reader:
                        sym = (row.get("TckrSymb") or "").strip()
                        if not sym:
                            continue
                        out.append({
                            "instrument_id":  (row.get("FinInstrmId") or "").strip(),
                            "symbol":         sym,
                            "name":           (row.get("FinInstrmNm") or "").strip(),
                            "expiry_epoch":   (row.get("XpryDt") or "").strip(),
                            "strike":         (row.get("StrkPric") or "").strip(),
                            "option_type":    (row.get("OptnTp") or "").strip(),
                            "lot_size":       (row.get("NewBrdLotQty") or "").strip(),
                            "instrument_type": (row.get("FinInstrmTp") or "").strip(),
                            "isin":           (row.get("ISIN") or "").strip(),
                            "settlement":     (row.get("SttlmMtd") or "").strip(),
                            "permitted":      (row.get("PrtdToTrad") or "").strip(),
                        })
                    if out:
                        return out
            except Exception:
                continue
        return []

    def fo_spread_contracts(self, trade_date: date | None = None) -> list[dict]:
        """NSE F&O spread contract definitions (calendar spreads between expiries).

        Returns [{symbol, near_leg_id, far_leg_id, ref_price, price_range_low, price_range_high}].
        """
        import gzip as _gz, csv as _c, io as _i
        base = trade_date or date.today()
        for offset in range(5):
            check = base - timedelta(days=offset)
            if check.weekday() >= 5:
                continue
            ds = check.strftime("%d%m%Y")
            url = f"{_ARCHIVE}/content/fo/NSE_FO_spdcontract_{ds}.csv.gz"
            try:
                r = self._session._s.get(url, timeout=30)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    with _gz.open(_i.BytesIO(r.content)) as f:
                        text = f.read().decode("utf-8", errors="replace")
                    reader = _c.DictReader(_i.StringIO(text))
                    out = []
                    for row in reader:
                        sym = (row.get("TckrSymb1") or "").strip()
                        if not sym:
                            continue
                        out.append({
                            "symbol":           sym,
                            "near_leg_id":      (row.get("FinInstrmIdLeg1") or "").strip(),
                            "far_leg_id":       (row.get("FinInstrmIdLeg2") or "").strip(),
                            "near_expiry":      (row.get("XpryDt1") or "").strip(),
                            "far_expiry":       (row.get("XpryDt2") or "").strip(),
                            "lot_size":         (row.get("NewBrdLotQty1") or "").strip(),
                            "ref_price":        (row.get("RefPric") or "").strip(),
                            "price_range_low":  (row.get("DayLwPricDiffRg") or "").strip(),
                            "price_range_high": (row.get("DayHghPricDiffRg") or "").strip(),
                            "eligible":         (row.get("Elgblty") or "").strip(),
                        })
                    if out:
                        return out
            except Exception:
                continue
        return []

    def fo_ael_option_contracts(self, trade_date: date | None = None) -> list[dict]:
        """Contract-wise Additional Exposure Limit (AEL) for F&O option contracts.

        Returns [{instrument, symbol, expiry, strike, option_type, buy_elm_pct, sell_elm_pct}].
        No-header CSV — columns inferred from NSE documentation.
        """
        import gzip as _gz, csv as _c, io as _i
        base = trade_date or date.today()
        for offset in range(5):
            check = base - timedelta(days=offset)
            if check.weekday() >= 5:
                continue
            ds = check.strftime("%d%m%Y")
            url = f"{_ARCHIVE}/content/fo/F_AEL_OPT_CONTRACTS_{ds}.csv.gz"
            try:
                r = self._session._s.get(url, timeout=30)
                if r.status_code == 200 and b"DOCTYPE" not in r.content[:50]:
                    with _gz.open(_i.BytesIO(r.content)) as f:
                        text = f.read().decode("utf-8", errors="replace")
                    out = []
                    for row in _c.reader(_i.StringIO(text)):
                        if len(row) < 7:
                            continue
                        instrument = row[0].strip()
                        if not instrument or instrument.startswith("#"):
                            continue
                        out.append({
                            "instrument":   instrument,
                            "symbol":       row[1].strip(),
                            "expiry":       row[2].strip(),
                            "strike":       row[3].strip(),
                            "option_type":  row[4].strip(),
                            "buy_elm_pct":  row[5].strip(),
                            "sell_elm_pct": row[6].strip(),
                        })
                    if out:
                        return out
            except Exception:
                continue
        return []
