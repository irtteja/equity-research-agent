import csv
import io
from datetime import date, timedelta
from typing import Any


def _fmt(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def _fmt_numeric(d: date) -> str:
    return d.strftime("%d-%m-%Y")


# NSE archive: index name → constituent CSV filename
_CONSTITUENT_CSV: dict[str, str] = {
    "NIFTY 50":                         "ind_nifty50list.csv",
    "NIFTY NEXT 50":                    "ind_niftynext50list.csv",
    "NIFTY 100":                        "ind_nifty100list.csv",
    "NIFTY 200":                        "ind_nifty200list.csv",
    "NIFTY 500":                        "ind_nifty500list.csv",
    "NIFTY MIDCAP 50":                  "ind_niftymidcap50list.csv",
    "NIFTY MIDCAP 100":                 "ind_niftymidcap100list.csv",
    "NIFTY MIDCAP 150":                 "ind_niftymidcap150list.csv",
    "NIFTY MIDCAP SELECT":              "ind_niftymidcapselect_list.csv",
    "NIFTY SMALLCAP 50":                "ind_niftysmallcap50list.csv",
    "NIFTY SMALLCAP 100":               "ind_niftysmallcap100list.csv",
    "NIFTY SMALLCAP 250":               "ind_niftysmallcap250list.csv",
    "NIFTY LARGEMIDCAP 250":            "ind_niftylargemidcap250list.csv",
    "NIFTY MIDSMALLCAP 400":            "ind_niftymidsmallcap400list.csv",
    "NIFTY MICROCAP 250":               "ind_niftymicrocap250_list.csv",
    "NIFTY TOTAL MARKET":               "ind_niftytotalmarket_list.csv",
    "NIFTY BANK":                       "ind_niftybanklist.csv",
    "NIFTY IT":                         "ind_niftyitlist.csv",
    "NIFTY AUTO":                       "ind_niftyautolist.csv",
    "NIFTY PHARMA":                     "ind_niftypharmalist.csv",
    "NIFTY FMCG":                       "ind_niftyfmcglist.csv",
    "NIFTY METAL":                      "ind_niftymetallist.csv",
    "NIFTY REALTY":                     "ind_niftyrealtylist.csv",
    "NIFTY HEALTHCARE INDEX":           "ind_niftyhealthcarelist.csv",
    "NIFTY INFRASTRUCTURE":             "ind_niftyinfralist.csv",
    "NIFTY ENERGY":                     "ind_niftyenergylist.csv",
    "NIFTY MEDIA":                      "ind_niftymedialist.csv",
    "NIFTY PSU BANK":                   "ind_niftypsubanklist.csv",
    "NIFTY CPSE":                       "ind_niftycpselist.csv",
    "NIFTY PSE":                        "ind_niftypselist.csv",
    "NIFTY OIL & GAS":                  "ind_niftyoilgaslist.csv",
    "NIFTY MNC":                        "ind_niftymnclist.csv",
    "NIFTY COMMODITIES":                "ind_niftycommoditieslist.csv",
    "NIFTY INDIA CONSUMPTION":          "ind_niftyconsumptionlist.csv",
    "NIFTY CONSUMER DURABLES":          "ind_niftyconsumerdurableslist.csv",
    "NIFTY INDIA MANUFACTURING":        "ind_niftyindiamanufacturing_list.csv",
    "NIFTY INDIA DEFENCE":              "ind_niftyindiadefence_list.csv",
    "NIFTY MIDSMALL HEALTHCARE":        "ind_niftymidsmallhealthcare_list.csv",
    "NIFTY INDIA DIGITAL":              "ind_niftyindiadigital_list.csv",
    "NIFTY INDIA TOURISM":              "ind_niftyindiatourism_list.csv",
    "NIFTY TRANSPORTATION & LOGISTICS": "ind_niftytransportationandlogistics_list.csv",
    "NIFTY MIDCAP150 QUALITY 50":       "ind_niftymidcap150quality50list.csv",
    "NIFTY CORE HOUSING":               "ind_niftycorehousing_list.csv",
    "NIFTY MOBILITY":                   "ind_niftymobility_list.csv",
}

NSE_ARCHIVE = "https://nsearchives.nseindia.com"


class MarketMixin:
    """
    Live market data: status, stock lists, gainers, losers,
    advance/decline, deals, pre-open, and holidays.
    """

    # ------------------------------------------------------------------ #
    #  Market status & holidays                                            #
    # ------------------------------------------------------------------ #

    def status(self) -> list[dict]:
        """Open/closed status for all NSE segments (CM, FO, CD, etc.)."""
        data = self._session.get("marketStatus")
        return data.get("marketState", data) if isinstance(data, dict) else data

    def holidays(self, type: str = "trading") -> dict:
        """
        Exchange holiday calendar.
        type: 'trading' | 'clearing'
        Returns dict keyed by segment (CM, FO, CD…).
        """
        return self._session.get("holiday-master", params={"type": type})

    # ------------------------------------------------------------------ #
    #  Stock universe                                                      #
    # ------------------------------------------------------------------ #

    def fetch_index_constituents(self, index: str = "NIFTY 50") -> list[dict]:
        """
        Constituent stocks of an NSE index — company name, symbol, industry, ISIN.
        Fetched from NSE's static archive (works from any IP).
        Returns a list of dicts with keys: Company Name, Industry, Symbol, Series, ISIN Code.
        """
        key = index.upper()
        filename = _CONSTITUENT_CSV.get(key)
        if not filename:
            # fuzzy match
            match = next(
                (k for k in _CONSTITUENT_CSV if key in k or k in key), None
            )
            if not match:
                from ..exceptions import NSEDataError
                raise NSEDataError(
                    f"No constituent data for {index!r}. "
                    f"Supported: {sorted(_CONSTITUENT_CSV.keys())}"
                )
            filename = _CONSTITUENT_CSV[match]

        url  = f"{NSE_ARCHIVE}/content/indices/{filename}"
        resp = self._session.get_raw(url)
        reader = csv.DictReader(io.StringIO(resp.text))
        return [dict(row) for row in reader]

    def list_equity_stocks_by_index(self, index: str = "NIFTY 50") -> list[dict]:
        """
        Constituent stocks of an NSE index.
        Returns the same data as fetch_index_constituents().
        """
        return self.fetch_index_constituents(index)

    def list_etf(self) -> dict:
        """All ETFs listed on NSE."""
        return self._session.get("etf")


    # ------------------------------------------------------------------ #
    #  Gainers / losers / breadth                                         #
    # ------------------------------------------------------------------ #

    def gainers(self, data: dict, count: int | None = None) -> list[dict]:
        """
        Top gainers from a universe returned by list_equity_stocks_by_index().
        Sorted by pChange descending.
        """
        stocks = data.get("data", [])
        result = sorted(
            [s for s in stocks if s.get("pChange", 0) > 0],
            key=lambda s: s.get("pChange", 0),
            reverse=True,
        )
        return result[:count] if count else result

    def losers(self, data: dict, count: int | None = None) -> list[dict]:
        """
        Top losers. Sorted by pChange ascending (biggest loss first).
        """
        stocks = data.get("data", [])
        result = sorted(
            [s for s in stocks if s.get("pChange", 0) < 0],
            key=lambda s: s.get("pChange", 0),
        )
        return result[:count] if count else result

    def advance_decline(self, index: str = "NIFTY 50") -> dict:
        """
        Advance / decline / unchanged count for a given index.
        Fetched from live allIndices data.
        """
        all_data = self._session.get("allIndices")
        indices  = all_data.get("data", []) if isinstance(all_data, dict) else []
        idx = next(
            (i for i in indices if i.get("index", "").upper() == index.upper()),
            None,
        )
        if not idx:
            return {"index": index, "advances": None, "declines": None, "unchanged": None, "total": None, "ratio": None}
        adv = int(idx.get("advances", 0) or 0)
        dec = int(idx.get("declines", 0) or 0)
        unc = int(idx.get("unchanged", 0) or 0)
        return {
            "index":     index,
            "advances":  adv,
            "declines":  dec,
            "unchanged": unc,
            "total":     adv + dec + unc,
            "ratio":     round(adv / dec, 2) if dec else None,
        }

    def live_volume_gainers(self) -> dict:
        """Stocks with unusual volume surge relative to their average."""
        return self._session.get("live-analysis-volume-gainers")

    # ------------------------------------------------------------------ #
    #  Pre-open market                                                     #
    # ------------------------------------------------------------------ #

    def pre_open(self, category: str = "ALL") -> dict:
        """
        Pre-open session data (IEP, buy/sell qty, final price).
        Available 09:00–09:15 IST only.
        category: 'NIFTY 50' | 'NIFTY BANK' | 'ALL'
        """
        return self._session.get(
            "market-data-pre-open",
            params={"key": category},
        )

    # ------------------------------------------------------------------ #
    #  Block & bulk deals                                                  #
    # ------------------------------------------------------------------ #

    def block_deals(self) -> dict:
        """Today's block deals. Available during deal windows only."""
        return self._session.get("block-deal")

    def bulk_deals(
        self,
        option_type: str  = "bulk_deals",
        from_date:   date | None = None,
        to_date:     date | None = None,
    ) -> list[dict]:
        """
        Bulk / block / short-selling deals over a date range (max 365 days).
        option_type: 'bulk_deals' | 'block_deals' | 'short_selling'
        """
        to_date   = to_date   or date.today()
        from_date = from_date or (to_date - timedelta(days=7))

        if (to_date - from_date).days > 365:
            raise ValueError("Date range cannot exceed 365 days for bulk_deals()")
        if from_date > to_date:
            raise ValueError("from_date must be before to_date")

        resp = self._session.get(
            "historicalOR/bulk-block-short-deals",
            params={
                "optionType": option_type,
                "from":       _fmt_numeric(from_date),
                "to":         _fmt_numeric(to_date),
            },
        )
        return resp.get("data", resp) if isinstance(resp, dict) else resp

    def get_fii_dii_trade_react(self):
        """FII/DII provisional trading activity (buy/sell/net values)."""
        path = "fiidiiTradeReact"
        return (self._guardian.guarded_get if self._guardian else self._session.get)(path, None)

    def get_live_variations(self, index="gainers"):
        """Live top gainers/losers. index: 'gainers' or 'loosers' (NSE's spelling)."""
        path = "live-analysis-variations"
        params = {"index": index}
        return (self._guardian.guarded_get if self._guardian else self._session.get)(path, params)

    def get_price_band_hitters(self):
        """Securities currently hitting their upper/lower price bands."""
        path = "live-analysis-price-band-hitter"
        return (self._guardian.guarded_get if self._guardian else self._session.get)(path, None)

    def live_analysis_variations(self, index: str = "gainers", params: dict = None):
        """Fetch live market variations (e.g. top gainers/losers).
    
        index: variation category passed to the API (default 'gainers').
        params: optional extra query parameters; merged over the defaults.
        """
        query = {"index": index}
        if params:
            query.update(params)
        return (self._guardian.guarded_get if self._guardian else self._session.get)("live-analysis-variations", query)

    def pre_open_market_data(self, key='NIFTY'):
        """Fetch pre-open market data for the given category key (e.g. NIFTY, BANKNIFTY, FO, ALL)."""
        path = "market-data-pre-open"
        params = {"key": key}
        return (self._guardian.guarded_get if self._guardian else self._session.get)(path, params)

    def market_turnover(self) -> list[dict]:
        """
        Previous day and live intraday turnover by segment.
        Segments: Equities, Index Futures, Index Options, Stock Futures,
                  Stock Options, Currency Futures, Currency Options,
                  Commodity Futures, Commodity Options, Interest Rate Futures.
        Each record: { name, yesterday: {volume, value, openInterest},
                                 today: {volume, value, openInterest} }
        value is in ₹ (not crores).
        """
        resp = self._session.get("market-turnover")
        return resp.get("data", []) if isinstance(resp, dict) else resp

    def fii_dii_trade_activity(self):
        """Fetch FII/DII trading activity (provisional cash market buy/sell values)."""
        path = "fiidiiTradeReact"
        params = {}
        return (self._guardian.guarded_get if self._guardian else self._session.get)(path, params)

    def get_live_analysis_variations(self, index: str = "gainers"):
        """Live top gainers/losers variations. index: 'gainers' or 'loosers'."""
        params = {"index": index}
        return (self._guardian.guarded_get if self._guardian else self._session.get)("live-analysis-variations", params)

    def stocks_traded(self):
        """Full market snapshot: advances/declines/unchanged counts + per-stock LTP, % change, volume, market cap."""
        return (self._guardian.guarded_get if self._guardian else self._session.get)("live-analysis-stocksTraded", {})

    def live_variations_gainers(self):
        """Live analysis: top gainers variations."""
        data = (self._guardian.guarded_get if self._guardian else self._session.get)("live-analysis-variations", {"index": "gainers"})
        return data
