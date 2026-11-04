import csv as _csv
import io as _io
import zipfile as _zipfile
from datetime import date, timedelta
from pathlib import Path

from ..exceptions import NSEFileUnavailableError

NSE_ARCHIVE = "https://nsearchives.nseindia.com"


def _fmt(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _chunk(from_date: date, to_date: date, days: int = 100):
    cur = from_date
    while cur <= to_date:
        end = min(cur + timedelta(days=days - 1), to_date)
        yield cur, end
        cur = end + timedelta(days=1)


def _calendar_days(from_date: date, to_date: date):
    cur = from_date
    while cur <= to_date:
        yield cur
        cur += timedelta(days=1)


class HistoryMixin:
    """Historical OHLCV data for equities, F&O, and VIX."""

    # ------------------------------------------------------------------ #
    #  Bhavcopy helpers                                                    #
    # ------------------------------------------------------------------ #

    def _bhavcopy_cache_path(self, d: date) -> Path:
        return (
            self._session.download_folder
            / "bhavcopy"
            / d.strftime("%Y-%m")
            / f"{d.strftime('%Y%m%d')}.csv"
        )

    def _fetch_bhavcopy_day(self, d: date) -> list[dict]:
        """Return all rows from the CM bhavcopy for one trading day.
        Returns [] for weekends / holidays (file not available)."""
        cache = self._bhavcopy_cache_path(d)
        if cache.exists():
            with open(cache, newline="") as f:
                return list(_csv.DictReader(f))

        url = (
            f"{NSE_ARCHIVE}/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"
        )
        try:
            resp = self._session.get_raw(url)
        except NSEFileUnavailableError:
            return []

        zf = _zipfile.ZipFile(_io.BytesIO(resp.content))
        csv_bytes = zf.read(zf.namelist()[0])
        csv_text = csv_bytes.decode("utf-8")

        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(csv_text)

        return list(_csv.DictReader(_io.StringIO(csv_text)))

    def _equity_from_bhavcopy(
        self,
        symbol:    str,
        series:    str,
        from_date: date,
        to_date:   date,
    ) -> list[dict]:
        sym = symbol.upper()
        ser = series.upper()
        rows: list[dict] = []

        for d in _calendar_days(from_date, to_date):
            if d.weekday() >= 5:
                continue
            daily = self._fetch_bhavcopy_day(d)
            for row in daily:
                if row.get("TckrSymb", "").strip() == sym and \
                   row.get("SctySrs", "").strip() == ser:
                    rows.append({
                        "date":       row.get("TradDt", ""),
                        "symbol":     row.get("TckrSymb", ""),
                        "series":     row.get("SctySrs", ""),
                        "open":       float(row["OpnPric"])  if row.get("OpnPric")  else None,
                        "high":       float(row["HghPric"])  if row.get("HghPric")  else None,
                        "low":        float(row["LwPric"])   if row.get("LwPric")   else None,
                        "close":      float(row["ClsPric"])  if row.get("ClsPric")  else None,
                        "last":       float(row["LastPric"]) if row.get("LastPric") else None,
                        "prev_close": float(row["PrvsClsgPric"]) if row.get("PrvsClsgPric") else None,
                        "volume":     int(float(row["TtlTradgVol"])) if row.get("TtlTradgVol") else None,
                        "turnover":   float(row["TtlTrfVal"]) if row.get("TtlTrfVal") else None,
                        "trades":     int(float(row["TtlNbOfTxsExctd"])) if row.get("TtlNbOfTxsExctd") else None,
                    })
                    break

        return sorted(rows, key=lambda r: r["date"])

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def fetch_equity_historical_data(
        self,
        symbol:    str,
        from_date: date | None = None,
        to_date:   date | None = None,
        series:    str = "EQ",
        days:      int | None = None,
    ) -> list[dict]:
        """
        Daily OHLCV for an NSE equity.
        Tries the NSE API first (fast, works on residential IPs).
        Falls back to NSE bhavcopy archive (works from any IP including cloud).

        Args:
            symbol:    NSE symbol e.g. 'RELIANCE'
            from_date: start date (default: 30 days before to_date)
            to_date:   end date (default: today)
            series:    series code (default 'EQ')
            days:      convenience — sets from_date = today - days
        """
        to_date = to_date or date.today()
        if days:
            from_date = to_date - timedelta(days=days)
        from_date = from_date or (to_date - timedelta(days=30))

        if from_date > to_date:
            raise ValueError("from_date must be before to_date")

        # Try the NSE API (historicalOR path — fast, single request per 100-day chunk)
        try:
            rows: list[dict] = []
            for start, end in _chunk(from_date, to_date, days=100):
                resp = self._session.get(
                    "historicalOR/cm/equity",
                    params={
                        "symbol":  symbol.upper(),
                        "series":  f'["{series.upper()}"]',
                        "from":    _fmt(start),
                        "to":      _fmt(end),
                        "csv":     "false",
                    },
                )
                chunk = resp.get("data", []) if isinstance(resp, dict) else []
                rows.extend(chunk)

            if rows:
                rows.sort(key=lambda r: r.get("CH_TIMESTAMP", r.get("mTIMESTAMP", "")))
                return rows
        except Exception:
            pass

        # Fall back to daily bhavcopy files from the NSE archive (works from any IP)
        return self._equity_from_bhavcopy(symbol, series, from_date, to_date)

    def fetch_historical_fno_data(
        self,
        symbol:       str,
        instrument:   str = "FUTIDX",
        from_date:    date | None = None,
        to_date:      date | None = None,
        expiry:       str | None = None,
        option_type:  str | None = None,
        strike_price: float | None = None,
    ) -> list[dict]:
        """
        Daily F&O OHLCV + OI. Ranges >1 year are chunked.

        instrument: FUTIDX | FUTSTK | OPTIDX | OPTSTK | FUTIVX
        option_type: required for OPTIDX / OPTSTK — 'CE' | 'PE'
        """
        inst_upper = instrument.upper()
        if inst_upper in ("OPTIDX", "OPTSTK") and not option_type:
            raise ValueError(f"option_type ('CE' or 'PE') is required for {inst_upper}")

        to_date   = to_date   or date.today()
        from_date = from_date or (to_date - timedelta(days=30))

        if from_date > to_date:
            raise ValueError("from_date must be before to_date")

        rows: list[dict] = []
        for start, end in _chunk(from_date, to_date, days=364):
            params: dict = {
                "instrumentType": inst_upper,
                "symbol":         symbol.upper(),
                "from":           _fmt(start),
                "to":             _fmt(end),
            }
            if expiry:
                params["expiryDate"] = expiry
            if option_type:
                params["optionType"] = option_type.upper()
            if strike_price is not None:
                params["strikePrice"] = str(strike_price)

            resp = self._session.get("historicalOR/fo/derivatives", params=params)
            chunk = resp.get("data", []) if isinstance(resp, dict) else []
            rows.extend(chunk)

        rows.sort(key=lambda r: r.get("FH_TIMESTAMP", r.get("Date", "")))
        return rows
