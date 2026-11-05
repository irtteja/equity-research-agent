"""
Orchestrator agent — routes queries to MarketAnalyst or FundamentalsAnalyst,
collects their outputs, and writes the final synthesis.
"""
import sys, os
_SWARM_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, "/home/user/agency-swarm/src")
sys.path.insert(1, "/usr/local/lib/python3.13/site-packages")
if _SWARM_ROOT not in sys.path:
    sys.path.append(_SWARM_ROOT)

import json
import re
from swarm.model import ClaudeCodeModel


_MARKET_KEYWORDS = {
    "price", "ltp", "quote", "momentum", "technical", "option", "chain",
    "mover", "gainer", "loser", "52w", "volume", "chart", "trend", "nifty",
    "index", "market", "today", "pcr", "max pain",
}
_FUNDAMENTAL_KEYWORDS = {
    "result", "earning", "quarterly", "announcement", "board meeting",
    "dividend", "split", "bonus", "filing", "xbrl", "conviction", "score",
    "rank", "recall", "research", "corporate action", "concall",
}


def _last_user(messages: list) -> str:
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return str(c)
    return ""


def _tool_results(messages: list) -> list[dict]:
    return [
        m for m in messages
        if isinstance(m, dict) and "call_id" in m and "output" in m
        and "name" not in m
    ]


def _classify(query: str) -> tuple[str, int]:
    """Return ('market'|'fundamentals'|'both'|'unknown', confidence 0-100)."""
    q = query.lower()
    m_hits = sum(1 for w in _MARKET_KEYWORDS      if w in q)
    f_hits = sum(1 for w in _FUNDAMENTAL_KEYWORDS if w in q)

    if m_hits > 0 and f_hits == 0:
        return "market", min(100, m_hits * 30)
    if f_hits > 0 and m_hits == 0:
        return "fundamentals", min(100, f_hits * 30)
    if m_hits > 0 and f_hits > 0:
        return "both", 60
    return "unknown", 0


async def orchestrator_brain(system: str | None, messages: list, tools: list[dict]) -> tuple[str | None, list[dict]]:
    query  = _last_user(messages).strip()
    results = _tool_results(messages)

    # After tool results come back (from send_message to sub-agents), synthesize
    if results:
        combined = []
        for r in results:
            output = r.get("output", "")
            try:
                data = json.loads(output)
                # SendMessage returns the agent's text reply as a plain string or dict
                if isinstance(data, str):
                    combined.append(data)
                elif isinstance(data, dict):
                    combined.append(data.get("content") or data.get("result") or str(data))
                else:
                    combined.append(str(data))
            except Exception:
                # Plain text reply from sub-agent
                combined.append(output)

        final = "**Research Summary**\n\n" + "\n\n---\n\n".join(combined)
        return final, []

    route, confidence = _classify(query)

    # Route to appropriate specialist via SendMessage
    # tool name is 'send_message', params: recipient_agent, message, additional_instructions
    if route == "market":
        return None, [{
            "name": "send_message",
            "arguments": {"recipient_agent": "MarketAnalyst", "message": query, "additional_instructions": ""},
        }]

    if route == "fundamentals":
        return None, [{
            "name": "send_message",
            "arguments": {"recipient_agent": "FundamentalsAnalyst", "message": query, "additional_instructions": ""},
        }]

    if route == "both":
        return None, [
            {"name": "send_message", "arguments": {"recipient_agent": "MarketAnalyst",       "message": query, "additional_instructions": ""}},
            {"name": "send_message", "arguments": {"recipient_agent": "FundamentalsAnalyst", "message": query, "additional_instructions": ""}},
        ]

    # Unknown — provide a helpful menu
    return (
        "**NSE Equity Research Swarm** — I can route your query to:\n\n"
        "📈 **MarketAnalyst** — prices, quotes, momentum, option chains, movers\n"
        "📊 **FundamentalsAnalyst** — earnings, announcements, XBRL, conviction scores\n\n"
        "Try: 'What is the price of RELIANCE?' or 'Latest results for HDFC Bank' "
        "or 'Compare INFY vs TCS momentum'."
    ), []


def build_orchestrator(market_agent, fundamentals_agent):
    sys.path.insert(0, "/home/user/agency-swarm/src")
    from agency_swarm import Agent, function_tool

    model = ClaudeCodeModel(orchestrator_brain, "orchestrator-v1")
    # SendMessage is auto-added by Agency when communication_flows includes this agent
    return Agent(
        name="Orchestrator",
        instructions=(
            "You are the NSE Equity Research orchestrator. "
            "Classify incoming queries and route them to the right specialist:\n"
            "- Price / market data → MarketAnalyst\n"
            "- Earnings / announcements / fundamentals → FundamentalsAnalyst\n"
            "- Broad research → both\n"
            "Synthesize sub-agent outputs into a clean, numbered summary. "
            "No SME/Emerge stocks. Mainboard India equities only."
        ),
        tools=[],
        model=model,
    )
