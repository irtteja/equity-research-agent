"""
FundamentalsAnalyst agent — earnings, announcements, XBRL financials, conviction scores.
Brain: maps query intent → tool calls → structured fundamental output.
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
    return [
        m for m in messages
        if isinstance(m, dict) and "call_id" in m and "output" in m
        and "name" not in m
    ]


def _parse_output(output: str) -> dict:
    try:
        return json.loads(output)
    except Exception:
        try:
            import ast
            return ast.literal_eval(output)
        except Exception:
            return {"raw": output}


def _extract_symbols(text: str) -> list[str]:
    return re.findall(r"\b([A-Z][A-Z0-9&-]{1,14})\b", text)


def _fmt_announcement(a: dict) -> str:
    sym  = a.get("symbol", a.get("sym", ""))
    desc = a.get("desc", a.get("description", a.get("subject", "")))[:120]
    dt   = a.get("date", a.get("bm_date", ""))
    return f"  [{dt}] {sym}: {desc}"


async def fundamentals_brain(system: str | None, messages: list, tools: list[dict]) -> tuple[str | None, list[dict]]:
    query = _last_user(messages).strip()
    results = _tool_results(messages)

    # --- Synthesize tool results ---
    if results:
        lines = ["**Fundamentals Analysis**\n"]
        for r in results:
            data = _parse_output(r.get("output", "{}"))

            if "error" in data:
                lines.append(f"⚠ {data['error']}")
                continue

            # Announcements — returns {"count": N, "announcements": [...]}
            if "announcements" in data:
                ann_list = data["announcements"]
                lines.append(f"**Announcements** ({data.get('count', len(ann_list))} total, showing top 8)")
                for a in ann_list[:8]:
                    sym  = a.get("SYMBOL", a.get("symbol", ""))
                    subj = a.get("SUBJECT", a.get("subject", a.get("desc", "")))[:100]
                    dt   = a.get("DATE", a.get("date", ""))
                    lines.append(f"  [{dt}] **{sym}**: {subj}")
                continue

            # Legacy list format
            if isinstance(data, list) and data and (
                "desc" in (data[0] if data else {}) or "SYMBOL" in (data[0] if data else {})
            ):
                lines.append(f"**Announcements** ({len(data)} results)")
                for a in data[:8]:
                    lines.append(_fmt_announcement(a))
                continue

            # Financial results (XBRL)
            sym = data.get("symbol", data.get("ticker", ""))
            if sym:
                lines.append(f"**{sym} Fundamentals**")
                for k, v in data.items():
                    if k not in ("symbol", "ticker", "error") and v is not None:
                        lines.append(f"  {k}: {v}")
                lines.append("")

            # Conviction / graph scores
            if "conviction_score" in data:
                cs = data["conviction_score"]
                lines.append(
                    f"  Conviction: {cs:.2f}  "
                    f"Grade: {data.get('grade', '?')}  "
                    f"Trend: {data.get('conviction_trend', '?')}"
                )

        return "\n".join(lines), []

    # --- Decide tools to call ---
    q_lower = query.lower()
    symbols = _extract_symbols(query)

    # Earnings / results calendar
    if any(w in q_lower for w in ["result", "earning", "quarterly", "q1", "q2", "q3", "q4", "xbrl"]):
        if symbols:
            return None, [{"name": "nse_integrated_filing", "arguments": {"symbol": symbols[0]}}]
        return None, [{"name": "nse_financial_results_bulk", "arguments": {"days": 7}}]

    # Announcements
    if any(w in q_lower for w in ["announcement", "bse", "nse news", "filing", "regulatory"]):
        sym = symbols[0] if symbols else None
        args = {"days": 3}
        if sym:
            args["symbol"] = sym
        return None, [{"name": "nse_all_announcements", "arguments": args}]

    # Board meetings / dividends
    if any(w in q_lower for w in ["board meeting", "dividend", "split", "bonus", "corporate action"]):
        args = {"days": 30}
        if symbols:
            args["symbol"] = symbols[0]
        return None, [{"name": "nse_corporate_actions_bulk", "arguments": args}]

    # Conviction score from graph
    if any(w in q_lower for w in ["conviction", "score", "rank", "rated", "grade"]):
        sym = symbols[0] if symbols else None
        if sym:
            return None, [{"name": "get_conviction_score", "arguments": {"ticker": sym}}]

    # Recall / memory
    if any(w in q_lower for w in ["recall", "remember", "past", "history", "research", "snapshot"]):
        sym = symbols[0] if symbols else None
        if sym:
            return None, [{"name": "recall_company_snapshot", "arguments": {"symbol": sym}}]
        return None, [{"name": "recall_search_facts", "arguments": {"query": query[:100]}}]

    # Fallback generic fundamentals for a symbol
    if symbols:
        return None, [
            {"name": "nse_integrated_filing", "arguments": {"symbol": symbols[0]}},
            {"name": "nse_all_announcements",  "arguments": {"symbol": symbols[0], "days": 7}},
        ]

    return (
        "I'm the Fundamentals Analyst. Ask me about earnings results, NSE announcements, "
        "corporate actions, XBRL filings, or conviction scores. "
        "Example: 'Latest results for RELIANCE' or 'Board meetings this week'."
    ), []


def build_fundamentals_analyst():
    import sys
    sys.path.insert(0, "/home/user/agency-swarm/src")
    from agency_swarm import Agent, function_tool

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from tools.indian_markets import (
        nse_all_announcements, nse_financial_results_bulk,
        nse_corporate_actions_bulk, nse_integrated_filing,
    )
    from tools.recall_tool import recall_company_snapshot, recall_insights, recall_search_facts

    # Conviction score from graph.db
    def get_conviction_score(ticker: str) -> dict:
        """Read conviction score + grade from graph.db for a ticker."""
        import sqlite3
        db = os.path.expanduser("~/equity-research-agent/data/graph.db")
        try:
            con = sqlite3.connect(db)
            cur = con.execute(
                "SELECT properties FROM nodes WHERE node_type='CompositeRank' AND name=? ORDER BY created_at DESC LIMIT 1",
                (ticker.upper(),)
            )
            row = cur.fetchone()
            con.close()
            if not row:
                return {"error": f"No conviction data for {ticker}"}
            props = json.loads(row[0])
            return {"ticker": ticker, **props}
        except Exception as e:
            return {"error": str(e)}

    model = ClaudeCodeModel(fundamentals_brain, "fundamentals-analyst-v1")
    return Agent(
        name="FundamentalsAnalyst",
        instructions=(
            "You are a fundamentals specialist for NSE India equities. "
            "You analyze earnings results, corporate announcements, board meetings, "
            "XBRL filings, and conviction scores from the knowledge graph. "
            "Be precise: quote specific numbers, dates, and percentages. "
            "Never surface SME/Emerge stocks. Mainboard only."
        ),
        tools=[
            function_tool(nse_all_announcements),
            function_tool(nse_financial_results_bulk),
            function_tool(nse_corporate_actions_bulk),
            function_tool(nse_integrated_filing),
            function_tool(recall_company_snapshot),
            function_tool(recall_insights),
            function_tool(recall_search_facts),
            function_tool(get_conviction_score),
        ],
        model=model,
    )
