"""
Recall tools — expose memory/recall.py as callable MCP tools.

Claude can call these mid-session to pull any fact from memory on demand,
rather than only receiving pre-loaded context at prompt-build time.

Registered automatically into mcp_server.py via RECALL_TOOLS list.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def recall_company_snapshot(symbol: str) -> dict:
    """
    Full company brief: prices + quarterly results + announcements + insights + documents.
    Use this at the start of any company analysis to load everything known.
    Returns {data, text} — text is ready for direct reading.
    """
    from memory.recall import company_snapshot
    return company_snapshot(symbol.upper())


def recall_prices(symbol: str, days: int = 30) -> dict:
    """
    Last N trading days of OHLCV price data for a symbol from bhavcopy.
    Returns {data: [{date,open,high,low,close,volume},...], text}.
    """
    from memory.recall import get_price_series
    return get_price_series(symbol.upper(), days=days)


def recall_results(symbol: str, n: int = 4) -> dict:
    """
    Last N quarters of XBRL financial results: revenue, PAT, EBITDA, EPS, QoQ deltas.
    Returns {data: [quarters...], text}.
    """
    from memory.recall import get_results
    return get_results(symbol.upper(), n=n)


def recall_announcements(symbol: str, days: int = 30, category: str = "") -> dict:
    """
    Recent NSE corporate announcements for a symbol.
    Optional category filter (e.g. 'Results', 'Board Meeting', 'Dividend').
    Returns {data: [announcements...], text}.
    """
    from memory.recall import get_announcements
    return get_announcements(symbol.upper(), days=days, category=category)


def recall_insights(symbol: str) -> dict:
    """
    Extracted insights and knowledge base entries for a symbol from past analysis.
    Returns {data: [insights...], text}.
    """
    from memory.recall import get_insights
    return get_insights(symbol.upper())


def recall_documents(symbol: str, doc_type: str = "") -> dict:
    """
    Index of ingested documents (annual reports, transcripts, results) for a symbol.
    Optional doc_type filter: annual_report | transcript | results | regulatory | news.
    Returns {data: [doc_index_entries...], text}.
    """
    from memory.recall import get_documents
    return get_documents(symbol.upper(), doc_type=doc_type)


def recall_search_documents(symbol: str, query: str, top_k: int = 5) -> dict:
    """
    Keyword search across all ingested document chunks for a symbol.
    Use when you need specific text from annual reports or transcripts.
    Returns {data: [matching_chunks...], text}.
    """
    from memory.recall import search_documents
    return search_documents(symbol.upper(), query=query, top_k=top_k)


def recall_macro(date: str = "") -> dict:
    """
    Macro snapshot for a date (YYYY-MM-DD). Defaults to most recent available.
    Returns global indices, commodities, currencies, FII/DII flows.
    Returns {data, text}.
    """
    from memory.recall import get_macro
    return get_macro(date=date or None)


def recall_search_facts(query: str, symbol: str = "") -> dict:
    """
    Cross-source keyword search across announcements and knowledge base.
    Optional symbol to narrow scope. Use for free-text research queries.
    Returns {data: [matching_facts...], text}.
    """
    from memory.recall import search_facts
    return search_facts(query=query, symbol=symbol.upper() if symbol else "")


def recall_self_model() -> dict:
    """
    Load the agent's current self-model: confirmed patterns, calibration signals,
    watch list, sector notes, recent session log.
    Returns {text} — formatted context block.
    """
    from memory.self_model import as_context, load
    model = load()
    return {
        "data": model,
        "text": as_context() or "(self-model not yet populated)",
    }


RECALL_TOOLS = [
    recall_company_snapshot,
    recall_prices,
    recall_results,
    recall_announcements,
    recall_insights,
    recall_documents,
    recall_search_documents,
    recall_macro,
    recall_search_facts,
    recall_self_model,
]
