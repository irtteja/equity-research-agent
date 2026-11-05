"""
MarketAnalyst agent — handles price data, momentum, technicals, option chains.
Brain: rule-based dispatcher that maps query intent → tool calls → synthesized output.
"""
import sys, os
_SWARM_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, "/home/user/agency-swarm/src")
sys.path.insert(1, "/usr/local/lib/python3.13/site-packages")
if _SWARM_ROOT not in sys.path:
    sys.path.append(_SWARM_ROOT)
if os.path.join(_SWARM_ROOT, "vendor") not in sys.path:
    sys.path.append(os.path.join(_SWARM_ROOT, "vendor"))

import json
import re
from swarm.model import ClaudeCodeModel


def _last_user(messages: list) -> str:
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return str(c)
    return ""


def _tool_results(messages: list) -> list[dict]:
    """Return messages that carry tool call outputs (call_id + output keys)."""
    return [
        m for m in messages
        if isinstance(m, dict) and "call_id" in m and "output" in m
        and "name" not in m          # exclude the function_call request itself
    ]


def _parse_output(output: str) -> dict:
    """Parse tool output — handles JSON or Python repr strings."""
    try:
        return json.loads(output)
    except Exception:
        try:
            import ast
            return ast.literal_eval(output)
        except Exception:
            return {"raw": output}


def _extract_symbols(text: str) -> list[str]:
    """Find uppercase NSE symbols like RELIANCE, HDFC, NIFTY50."""
    return re.findall(r"\b([A-Z][A-Z0-9&-]{1,14})\b", text)


async def market_brain(system: str | None, messages: list, tools: list[dict]) -> tuple[str | None, list[dict]]:
    query = _last_user(messages).strip()
    results = _tool_results(messages)

    # --- If we have tool results, synthesize them ---
    if results:
        parts = []
        for r in results:
            data = _parse_output(r.get("output", "{}"))
            parts.append(data)

        # Build a plain-language synthesis
        summary_lines = ["**Market Analysis Summary**\n"]
        for d in parts:
            if "error" in d:
                summary_lines.append(f"⚠ {d['error']}")
                continue
            sym = d.get("symbol", d.get("ticker", ""))
            if sym:
                # nse_equity_quote returns: last/close, pct_change, year_high, year_low, volume
                # yfinance returns: current_price, price_change_pct, high_52w, low_52w, avg_volume
                ltp = (d.get("last") or d.get("close") or
                       d.get("lastPrice") or d.get("current_price") or d.get("ltp") or "N/A")
                chg = (d.get("pct_change") or d.get("pChange") or
                       d.get("price_change_pct") or "")
                h52 = d.get("year_high") or d.get("high52") or d.get("high_52w") or ""
                l52 = d.get("year_low")  or d.get("low52")  or d.get("low_52w")  or ""
                vol = (d.get("volume") or d.get("totalTradedVolume") or
                       d.get("avg_volume") or "")
                pe  = d.get("pe", "")
                # During live market, last/close may be 0; use prev_close + change as fallback
                if not ltp or ltp == 0:
                    pc = d.get("prev_close", 0)
                    chg_abs = d.get("change", 0)
                    ltp = round(pc + chg_abs, 2) if pc else (d.get("high") or d.get("open") or "N/A")
                summary_lines.append(
                    f"**{sym}**: LTP ₹{ltp}"
                    + (f"  Chg {chg}%" if chg else "")
                    + (f"  52W H/L: ₹{h52}/₹{l52}" if h52 else "")
                    + (f"  Vol: {vol:,}" if vol and isinstance(vol, int) else (f"  Vol: {vol}" if vol else ""))
                    + (f"  P/E: {pe}" if pe else "")
                )
            # option chain summary
            if "maxPainStrike" in d:
                summary_lines.append(
                    f"  Max Pain: {d['maxPainStrike']}  "
                    f"PCR: {d.get('pcr', 'N/A')}  "
                    f"Expiry: {d.get('expiryDate', '')}"
                )
            # price change summary
            if "price_change_pct" in d and "records" in d:
                summary_lines.append(
                    f"  {d['records']} days history  "
                    f"Change: {d['price_change_pct']}%"
                )
        return "\n".join(summary_lines), []

    # --- Decide which tools to call ---
    q_lower = query.lower()
    symbols = _extract_symbols(query)

    # Option chain request
    if any(w in q_lower for w in ["option", "oi ", "pcr", "max pain", "chain"]):
        sym = symbols[0] if symbols else "NIFTY"
        return None, [{"name": "nse_option_chain", "arguments": {"symbol": sym}}]

    # Bulk quote for multiple symbols
    if len(symbols) > 1 and any(w in q_lower for w in ["compare", "vs", "price", "quote", "ltp"]):
        return None, [
            {"name": "nse_equity_quote", "arguments": {"symbol": s}} for s in symbols[:4]
        ]

    # Single quote / momentum
    if symbols and any(w in q_lower for w in ["price", "quote", "ltp", "stock", "how is", "what is", "momentum", "trend"]):
        sym = symbols[0]
        calls = [{"name": "nse_equity_quote", "arguments": {"symbol": sym}}]
        if any(w in q_lower for w in ["momentum", "history", "trend", "chart", "52"]):
            calls.append({"name": "nse_price_history", "arguments": {"symbol": sym, "days": 90}})
        return None, calls

    # Market overview / indices
    if any(w in q_lower for w in ["market", "nifty", "sensex", "index", "overview", "today"]):
        return None, [
            {"name": "nse_equity_quote", "arguments": {"symbol": "NIFTY"}},
            {"name": "nse_index_data",   "arguments": {"index_name": "NIFTY 50"}},
        ]

    # Top movers
    if any(w in q_lower for w in ["top gainer", "top loser", "mover", "best", "worst"]):
        return None, [{"name": "nse_index_data", "arguments": {"index_name": "NIFTY 50"}}]

    # Fallback — if symbols extracted, just quote them
    if symbols:
        return None, [{"name": "nse_equity_quote", "arguments": {"symbol": symbols[0]}}]

    return (
        "I'm the Market Analyst. Ask me about NSE stock prices, momentum, "
        "52-week highs/lows, option chains, or top movers. Example: "
        "'What is the momentum of RELIANCE?' or 'Show NIFTY options'."
    ), []


def build_market_analyst():
    """Return a configured MarketAnalyst agency-swarm Agent."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    sys.path.insert(0, "/home/user/agency-swarm/src")
    from agency_swarm import Agent, function_tool

    # Wrap equity-research-agent tools
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from tools.indian_markets import (
        nse_equity_quote, nse_price_history, nse_option_chain, nse_index_data,
    )

    model = ClaudeCodeModel(market_brain, "market-analyst-v1")
    return Agent(
        name="MarketAnalyst",
        instructions=(
            "You are a market data specialist for NSE India equities. "
            "You fetch real-time quotes, price history, option chains, and index data. "
            "Always return numbers with ₹ symbols for prices. Never surface SME or Emerge stocks. "
            "Summarize clearly: LTP, change %, 52W high/low, volume."
        ),
        tools=[
            function_tool(nse_equity_quote),
            function_tool(nse_price_history),
            function_tool(nse_option_chain),
            function_tool(nse_index_data),
        ],
        model=model,
    )
