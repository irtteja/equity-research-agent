from .price_data import get_price_history, get_current_quote
from .fundamentals import get_financial_statements, get_key_ratios
from .news_sentiment import get_news_sentiment
from .valuation import run_dcf_valuation, get_peer_comparison


def run_technical_analysis(*args, **kwargs):
    """Lazy wrapper — imports pandas_ta only when called (avoids numba/numpy conflict at import time)."""
    from .technical import run_technical_analysis as _fn
    return _fn(*args, **kwargs)
from .indian_markets import (
    nse_equity_quote, nse_price_history, nse_option_chain,
    nse_corporate_actions, nse_financial_results, nse_shareholding,
    nse_index_data, nse_lookup,
    bse_equity_quote, bse_weekly_hl, bse_equity_meta, bse_price_volume_12m,
    bse_trading_stats, bse_advance_decline, bse_gainers, bse_losers,
    bse_bhav_copy, bse_announcements, bse_corporate_actions,
    bse_result_calendar, bse_results_snapshot, bse_get_scrip_code, bse_lookup,
)
from .macro import (
    get_global_indices, get_commodity_prices, get_currency_rates,
    get_fii_dii_flows, get_block_bulk_deals, get_market_status,
    get_full_macro_snapshot,
)
from .announcements import (
    get_bse_announcements, get_nse_corporate_announcements,
    get_all_bse_announcements_today, get_insider_trades,
    get_upcoming_results_calendar, get_concall_transcripts,
)

CORE_TOOLS = [
    # Global (yfinance)
    get_price_history, get_current_quote, get_financial_statements,
    get_key_ratios, run_technical_analysis, get_news_sentiment,
    run_dcf_valuation, get_peer_comparison,
    # NSE India (nselib)
    nse_equity_quote, nse_price_history, nse_option_chain,
    nse_corporate_actions, nse_financial_results, nse_shareholding,
    nse_index_data, nse_lookup,
    # BSE India (bselib)
    bse_equity_quote, bse_weekly_hl, bse_equity_meta, bse_price_volume_12m,
    bse_trading_stats, bse_advance_decline, bse_gainers, bse_losers,
    bse_bhav_copy, bse_announcements, bse_corporate_actions,
    bse_result_calendar, bse_results_snapshot, bse_get_scrip_code, bse_lookup,
    # Macro monitoring
    get_global_indices, get_commodity_prices, get_currency_rates,
    get_fii_dii_flows, get_block_bulk_deals, get_market_status,
    get_full_macro_snapshot,
    # Announcements + concall transcripts
    get_bse_announcements, get_nse_corporate_announcements,
    get_all_bse_announcements_today, get_insider_trades,
    get_upcoming_results_calendar, get_concall_transcripts,
]
