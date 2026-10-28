from .price_data import get_price_history, get_current_quote
from .fundamentals import get_financial_statements, get_key_ratios
from .technical import run_technical_analysis
from .news_sentiment import get_news_sentiment
from .valuation import run_dcf_valuation, get_peer_comparison
from .indian_markets import (
    # NSE (nselib)
    nse_equity_quote,
    nse_price_history,
    nse_option_chain,
    nse_corporate_actions,
    nse_financial_results,
    nse_shareholding,
    nse_index_data,
    nse_lookup,
    # BSE (bselib)
    bse_equity_quote,
    bse_weekly_hl,
    bse_equity_meta,
    bse_price_volume_12m,
    bse_trading_stats,
    bse_advance_decline,
    bse_gainers,
    bse_losers,
    bse_bhav_copy,
    bse_announcements,
    bse_corporate_actions,
    bse_result_calendar,
    bse_results_snapshot,
    bse_get_scrip_code,
    bse_lookup,
)

CORE_TOOLS = [
    # Global (yfinance)
    get_price_history,
    get_current_quote,
    get_financial_statements,
    get_key_ratios,
    run_technical_analysis,
    get_news_sentiment,
    run_dcf_valuation,
    get_peer_comparison,
    # NSE India (nselib)
    nse_equity_quote,
    nse_price_history,
    nse_option_chain,
    nse_corporate_actions,
    nse_financial_results,
    nse_shareholding,
    nse_index_data,
    nse_lookup,
    # BSE India (bselib)
    bse_equity_quote,
    bse_weekly_hl,
    bse_equity_meta,
    bse_price_volume_12m,
    bse_trading_stats,
    bse_advance_decline,
    bse_gainers,
    bse_losers,
    bse_bhav_copy,
    bse_announcements,
    bse_corporate_actions,
    bse_result_calendar,
    bse_results_snapshot,
    bse_get_scrip_code,
    bse_lookup,
]
