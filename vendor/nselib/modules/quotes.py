from datetime import date
from typing import Any

_NEXTAPI = "NextApi/apiClient/GetQuoteApi"


class QuotesMixin:
    """Live quote data for individual NSE-listed stocks."""

    def quote(self, symbol: str, series: str = "EQ") -> dict:
        """
        Full quote for a symbol: OHLC, LTP, volume, market depth,
        52-week H/L, circuit limits, and last update timestamp.
        """
        raw = self._session.get(
            _NEXTAPI,
            params={
                "functionName": "getSymbolData",
                "marketType":   "N",
                "series":       series.upper(),
                "symbol":       symbol.upper(),
            },
        )
        items = raw.get("equityResponse", raw)
        return items[0] if isinstance(items, list) and items else items

    def equity_quote(self, symbol: str, series: str = "EQ") -> dict:
        """Simplified OHLCV snapshot — date, open, high, low, close, volume."""
        raw = self.quote(symbol, series)
        md  = raw.get("metaData", {})
        td  = raw.get("tradeInfo", {})
        pi  = raw.get("priceInfo", {})
        return {
            "symbol":     md.get("symbol", symbol.upper()),
            "date":       raw.get("lastUpdateTime", ""),
            "open":       md.get("open"),
            "high":       md.get("dayHigh"),
            "low":        md.get("dayLow"),
            "close":      md.get("closePrice"),
            "last":       md.get("closePrice"),
            "prev_close": md.get("previousClose"),
            "change":     md.get("change"),
            "pct_change": md.get("pChange"),
            "volume":     td.get("totalTradedVolume"),
            "turnover":   td.get("totalTradedValue"),
            "year_high":  pi.get("yearHigh"),
            "year_low":   pi.get("yearLow"),
        }

    def equity_meta_info(self, symbol: str) -> dict:
        """
        Company metadata: ISIN, series, sector, indices, circuit limits,
        listing date, board status, and more.
        """
        raw = self.quote(symbol)
        md  = raw.get("metaData", {})
        si  = raw.get("secInfo", {})
        ti  = raw.get("tradeInfo", {})
        return {
            "symbol":       md.get("symbol", symbol.upper()),
            "company_name": md.get("companyName"),
            "isin":         md.get("isinCode"),
            "series":       md.get("series"),
            "market_type":  md.get("marketType"),
            "sector":       si.get("sector"),
            "industry":     si.get("industryInfo"),
            "macro":        si.get("macro"),
            "indices":      si.get("indexList", []),
            "is_fno":       bool(ti.get("ffmc")),
            "is_suspended": si.get("isSuspended") != "Active",
            "listing_date": si.get("listingDate"),
            "face_value":   ti.get("faceValue"),
        }

    def lookup(self, query: str, segment: str = "equity") -> list[dict]:
        """
        Search NSE by company name or symbol.
        segment: 'equity' | 'derivatives' | 'etf' | 'all'

        Uses the constituent CSV for basic symbol search since the
        autocomplete API is no longer available.
        Returns list of matching stocks with symbol, name, series, ISIN.
        """
        from .market import _CONSTITUENT_CSV
        query_upper = query.upper()

        results: list[dict] = []
        seen: set[str] = set()

        for index_name, csv_file in _CONSTITUENT_CSV.items():
            try:
                stocks = self.fetch_index_constituents(index_name)
            except Exception:
                continue
            for s in stocks:
                sym  = s.get("Symbol", "")
                name = s.get("Company Name", "")
                if sym in seen:
                    continue
                if query_upper in sym or query_upper in name.upper():
                    seen.add(sym)
                    results.append({
                        "symbol":   sym,
                        "name":     name,
                        "series":   s.get("Series", ""),
                        "isin":     s.get("ISIN Code", ""),
                        "industry": s.get("Industry", ""),
                    })
            if len(results) >= 20:
                break

        return results

    def get_detailed_scrip_data(self, symbol: str, series: str = "EQ") -> dict:
        """Full quote dict including order book, trade info, and price info."""
        return self.quote(symbol, series)


    def get_equity_master(self):
        """Equity master: indices/categories mapped to their constituent equity segments."""
        path = "equity-master"
        return (self._guardian.guarded_get if self._guardian else self._session.get)(path, None)
