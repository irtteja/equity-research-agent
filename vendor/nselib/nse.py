"""
NSE — the main entry point.

Combines all domain mixins and optionally wires up the Claude Brain
for natural-language queries backed by live NSE data.
"""

from datetime import date
from pathlib import Path
from typing import Any

from .core     import NSESession
from .brain    import Brain
from .guardian import Guardian
from .exceptions import BrainNotConfiguredError
from .modules import (
    QuotesMixin,
    IndicesMixin,
    MarketMixin,
    HistoryMixin,
    CorporateMixin,
    FnoMixin,
    DownloadsMixin,
)


class NSE(
    QuotesMixin,
    IndicesMixin,
    MarketMixin,
    HistoryMixin,
    CorporateMixin,
    FnoMixin,
    DownloadsMixin,
):
    """
    NSE data library with optional Claude-powered intelligence.

    Usage
    -----
    Direct data access::

        with NSE() as nse:
            q = nse.equity_quote("RELIANCE")
            chain = nse.compile_option_chain("NIFTY")

    Natural language queries (uses the local claude CLI — no API key needed
    when running inside a Claude Code session)::

        with NSE(brain=True) as nse:
            print(nse.ask("What is the current NIFTY 50 level?"))
            print(nse.analyze("HDFC BANK"))
            print(nse.market_summary())

    Self-healing mode (intercepts broken calls and auto-patches them)::

        with NSE(guard=True) as nse:
            nse.equity_quote("RELIANCE")   # heals itself if something breaks

    Outside a Claude Code session (explicit API key)::

        with NSE(anthropic_api_key="sk-ant-...") as nse:
            print(nse.ask("..."))
    """

    def __init__(
        self,
        download_folder: str | Path = ".",
        rate:            float = 3.0,
        timeout:         int   = 15,
        brain:           bool  = False,
        guard:           bool  = False,
        model:           str   = "claude-haiku-5-5",
        patches_file:    str | Path | None = Path.home() / ".nselib" / "patches.json",
        anthropic_api_key: str | None = None,
    ):
        self._session  = NSESession(
            download_folder=download_folder,
            rate=rate,
            timeout=timeout,
        )
        self._brain:    Brain    | None = None
        self._guardian: Guardian | None = None

        # Brain activates when: brain=True, guard=True, or api_key supplied
        if brain or guard or anthropic_api_key:
            self._brain = Brain(api_key=anthropic_api_key, model=model)
            self._register_brain_tools()

        if guard:
            if self._brain is None:
                self._brain = Brain(api_key=anthropic_api_key, model=model)
                self._register_brain_tools()
            self._guardian = Guardian(self._session, self._brain,
                                       patches_file=patches_file)
            # Redirect all session.get() calls through the Guardian
            self._session.get = self._guardian.guarded_get

    # ------------------------------------------------------------------ #
    #  Context manager                                                     #
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "NSE":
        self._session.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._session.__exit__(*args)

    @property
    def guardian(self) -> Guardian:
        if self._guardian is None:
            raise BrainNotConfiguredError()
        return self._guardian

    # ------------------------------------------------------------------ #
    #  Brain API                                                           #
    # ------------------------------------------------------------------ #

    def ask(self, query: str) -> str:
        """Natural language query backed by live NSE data via Claude."""
        if self._brain is None:
            raise BrainNotConfiguredError()
        return self._brain.ask(query)

    def analyze(self, symbol: str) -> str:
        """Comprehensive Claude analysis of a stock."""
        if self._brain is None:
            raise BrainNotConfiguredError()
        return self._brain.analyze(symbol)

    def market_summary(self) -> str:
        """Claude-generated daily market summary."""
        if self._brain is None:
            raise BrainNotConfiguredError()
        return self._brain.market_summary()

    def screen(self, criteria: str) -> str:
        """Natural language stock screener powered by Claude."""
        if self._brain is None:
            raise BrainNotConfiguredError()
        return self._brain.screen(criteria)

    # ------------------------------------------------------------------ #
    #  Brain tool registration                                             #
    # ------------------------------------------------------------------ #

    def _register_brain_tools(self) -> None:
        """Register all NSE data methods as callable Claude tools."""
        b = self._brain

        b.register(
            "equity_quote",
            "Get live OHLCV quote for an NSE equity symbol.",
            {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "NSE symbol e.g. RELIANCE"},
                },
                "required": ["symbol"],
            },
            self.equity_quote,
        )

        b.register(
            "index_quote",
            "Get live value and change for an NSE index (e.g. 'NIFTY 50', 'NIFTY BANK').",
            {
                "type": "object",
                "properties": {
                    "index": {"type": "string", "description": "Index name e.g. 'NIFTY 50'"},
                },
                "required": ["index"],
            },
            self.index_quote,
        )

        b.register(
            "list_indices",
            "Get all NSE indices with live values, change, and turnover.",
            {"type": "object", "properties": {}},
            self.list_indices,
        )

        b.register(
            "gainers",
            "Top gainers from a universe. Call list_equity_stocks_by_index first to get data.",
            {
                "type": "object",
                "properties": {
                    "data":  {"type": "object", "description": "Output of list_equity_stocks_by_index"},
                    "count": {"type": "integer", "description": "Number of top gainers to return"},
                },
                "required": ["data"],
            },
            self.gainers,
        )

        b.register(
            "losers",
            "Top losers from a universe. Call list_equity_stocks_by_index first to get data.",
            {
                "type": "object",
                "properties": {
                    "data":  {"type": "object"},
                    "count": {"type": "integer"},
                },
                "required": ["data"],
            },
            self.losers,
        )

        b.register(
            "list_equity_stocks_by_index",
            "Get all stocks in a given index with live price and change.",
            {
                "type": "object",
                "properties": {
                    "index": {"type": "string", "description": "e.g. 'NIFTY 50', 'NIFTY BANK'"},
                },
                "required": [],
            },
            self.list_equity_stocks_by_index,
        )

        b.register(
            "advance_decline",
            "Advance/decline/unchanged count and ratio for an index.",
            {
                "type": "object",
                "properties": {
                    "index": {"type": "string"},
                },
                "required": [],
            },
            self.advance_decline,
        )

        b.register(
            "fetch_equity_historical_data",
            "Historical daily OHLCV for an equity. Use 'days' for convenience.",
            {
                "type": "object",
                "properties": {
                    "symbol":    {"type": "string"},
                    "from_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "to_date":   {"type": "string", "description": "YYYY-MM-DD"},
                    "days":      {"type": "integer", "description": "Last N days"},
                    "series":    {"type": "string", "default": "EQ"},
                },
                "required": ["symbol"],
            },
            lambda symbol, from_date=None, to_date=None, days=None, series="EQ": (
                self.fetch_equity_historical_data(
                    symbol,
                    from_date=date.fromisoformat(from_date) if from_date else None,
                    to_date=date.fromisoformat(to_date) if to_date else None,
                    days=days,
                    series=series,
                )
            ),
        )

        b.register(
            "fetch_historical_index_data",
            "Historical daily OHLC for an NSE index.",
            {
                "type": "object",
                "properties": {
                    "index":     {"type": "string"},
                    "from_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "to_date":   {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["index"],
            },
            lambda index, from_date=None, to_date=None: (
                self.fetch_historical_index_data(
                    index,
                    from_date=date.fromisoformat(from_date) if from_date else None,
                    to_date=date.fromisoformat(to_date) if to_date else None,
                )
            ),
        )

        b.register(
            "option_chain",
            "Full option chain for an index (NIFTY, BANKNIFTY) or stock symbol.",
            {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                },
                "required": ["symbol"],
            },
            self.option_chain,
        )

        b.register(
            "compile_option_chain",
            "Flatten option chain into strike rows with CE/PE data side-by-side.",
            {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "expiry": {"type": "string", "description": "DD-Mon-YYYY, defaults to nearest"},
                },
                "required": ["symbol"],
            },
            self.compile_option_chain,
        )

        b.register(
            "max_pain",
            "Calculate max pain strike for an options expiry.",
            {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "expiry": {"type": "string"},
                },
                "required": ["symbol"],
            },
            self.max_pain,
        )

        b.register(
            "corporate_actions",
            "Dividends, splits, bonuses for a symbol or all upcoming actions.",
            {
                "type": "object",
                "properties": {
                    "symbol":    {"type": "string"},
                    "from_date": {"type": "string"},
                    "to_date":   {"type": "string"},
                },
                "required": [],
            },
            lambda symbol=None, from_date=None, to_date=None: (
                self.corporate_actions(
                    symbol=symbol,
                    from_date=date.fromisoformat(from_date) if from_date else None,
                    to_date=date.fromisoformat(to_date) if to_date else None,
                )
            ),
        )

        b.register(
            "financial_results",
            "Quarterly/annual results filings for a symbol.",
            {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "period": {"type": "string", "default": "Quarterly"},
                },
                "required": [],
            },
            self.financial_results,
        )

        b.register(
            "status",
            "Current open/closed status of all NSE market segments.",
            {"type": "object", "properties": {}},
            self.status,
        )

        b.register(
            "pre_open",
            "Pre-open session IEP and order data (available 09:00–09:15 IST).",
            {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "NIFTY 50 | NIFTY BANK | ALL"},
                },
                "required": [],
            },
            self.pre_open,
        )


        b.register(
            "shareholding_patterns",
            "Promoter/FII/DII/public shareholding pattern for a symbol.",
            {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                },
                "required": ["symbol"],
            },
            self.shareholding_patterns,
        )
