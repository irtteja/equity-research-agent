# equity-research-agent

Deep equity research tools for Claude — NSE, BSE, global markets, technicals, valuation, and self-improving tool generation.

## Setup

```bash
git clone https://github.com/irtteja/equity-research-agent
cd equity-research-agent
pip install -r requirements.txt
pip install -e ~/irtteja/nselib   # your local nselib clone
```

## Add to Claude Code as MCP server

In your Claude Code `settings.json`:

```json
{
  "mcpServers": {
    "equity-research": {
      "command": "python",
      "args": ["/path/to/equity-research-agent/mcp_server.py"]
    }
  }
}
```

Then in any Claude session: *"Research RELIANCE"*, *"Compare TCS vs INFY"*, *"NIFTY 50 top gainers today"*, *"BSE announcements for HDFC"*

## Tools (33 total)

**NSE** — `nse_equity_quote`, `nse_price_history`, `nse_option_chain`, `nse_corporate_actions`, `nse_financial_results`, `nse_shareholding`, `nse_index_data`, `nse_lookup`

**BSE** — `bse_equity_quote`, `bse_weekly_hl`, `bse_equity_meta`, `bse_price_volume_12m`, `bse_trading_stats`, `bse_advance_decline`, `bse_gainers`, `bse_losers`, `bse_bhav_copy`, `bse_announcements`, `bse_corporate_actions`, `bse_result_calendar`, `bse_results_snapshot`, `bse_get_scrip_code`, `bse_lookup`

**Global (yfinance)** — `get_price_history`, `get_current_quote`, `get_financial_statements`, `get_key_ratios`, `run_technical_analysis`, `get_news_sentiment`, `run_dcf_valuation`, `get_peer_comparison`

## Self-improvement

After each research session the agent identifies gaps, writes a new Python tool to fill them, and saves it to `generated_tools/`. Next session it loads automatically.
