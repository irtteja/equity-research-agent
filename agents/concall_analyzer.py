"""
concall_analyzer.py — Extracts structured intelligence from concall data for a ticker.

Inputs:
  - ManagementStatement nodes in graph.db (category, statement, sentiment fields)
  - PDF files in memory_store/concalls/<TICKER>/ (optional, if present)

Outputs:
  - ConcallInsight node per ticker written to graph.db with fields:
      guidance, key_themes, tone, risks_flagged, positive_signals, specific_numbers,
      statement_count, sources_used

No API calls. No anthropic.Anthropic(). All analysis is keyword/regex-based in-session.

Called by: jobs/orchestrator.py (Phase 2 — Analysis)
CLI: python agents/concall_analyzer.py [--ticker TICKER] [--limit N]
"""
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

DB_PATH   = REPO / "memory_store" / "graph.db"
CONCALL_DIR = REPO / "memory_store" / "concalls"


# ── Keywords ─────────────────────────────────────────────────────────────────

GUIDANCE_PATTERNS = [
    r'\b(expect|guidance|target|aim|guide|project|anticipate|forecast)\b',
    r'\b(\d[\d,]*)\s*(crore|cr|lakhs?|mn|million|billion)\b',
    r'\b(\d+(?:\.\d+)?)\s*%',
    r'\b(revenue|volume|margin|ebitda|profit|growth|expansion)\b.*\b(\d+)',
]

RISK_KEYWORDS = [
    'concern', 'headwind', 'challenge', 'pressure', 'competitive',
    'risk', 'uncertainty', 'volatile', 'slowdown', 'decline', 'weak',
    'adverse', 'threat', 'difficult', 'struggle', 'reduce', 'lower',
    'miss', 'downgrade', 'impair', 'loss', 'write-off', 'inflation',
    'regulatory', 'compliance', 'litigation', 'delay', 'disruption',
]

POSITIVE_KEYWORDS = [
    'demand', 'growth', 'expansion', 'order', 'win', 'strong', 'robust',
    'record', 'highest', 'beat', 'exceed', 'improve', 'momentum',
    'opportunity', 'pipeline', 'launch', 'new', 'increase', 'uptick',
    'recovery', 'bullish', 'confident', 'outperform', 'gain', 'acquire',
]

BULLISH_TONE_WORDS  = ['strong', 'robust', 'record', 'highest', 'beat', 'confident', 'growth', 'expansion', 'optimistic', 'positive', 'opportunity', 'pipeline', 'momentum', 'recovery', 'win', 'gain']
CAUTIOUS_TONE_WORDS = ['cautious', 'moderate', 'steady', 'gradual', 'measured', 'selective', 'watchful', 'careful']
DEFENSIVE_TONE_WORDS= ['challenge', 'headwind', 'pressure', 'difficult', 'concern', 'uncertain', 'volatile', 'risk', 'slowdown', 'weak', 'competitive', 'decline']

THEME_KEYWORDS = {
    'revenue_growth':     ['revenue', 'sales', 'topline', 'income'],
    'margin_expansion':   ['margin', 'ebitda', 'profitability', 'operating leverage'],
    'capex_expansion':    ['capex', 'capacity', 'plant', 'investment', 'expand'],
    'export':             ['export', 'global', 'international', 'overseas'],
    'demand_recovery':    ['demand', 'recovery', 'uptick', 'pickup'],
    'new_products':       ['new product', 'launch', 'innovation', 'r&d'],
    'competition':        ['competition', 'competitive', 'market share', 'pricing'],
    'raw_material':       ['raw material', 'input cost', 'commodity', 'procurement'],
    'debt_reduction':     ['debt', 'leverage', 'borrowing', 'repay'],
    'working_capital':    ['working capital', 'receivable', 'inventory', 'cash cycle'],
    'regulatory':         ['regulatory', 'compliance', 'policy', 'government'],
    'digital':            ['digital', 'technology', 'automation', 'platform'],
    'sector_tailwind':    ['infrastructure', 'pli', 'make in india', 'china+1'],
}

NUMBER_PATTERN = re.compile(
    r'(?:'
    r'(?:rs\.?\s*|inr\s*|₹\s*)?'
    r'\d[\d,]*(?:\.\d+)?\s*(?:crore|cr|lakhs?|l|mn|million|billion|bn)?'
    r'|\d+(?:\.\d+)?\s*%'
    r')',
    re.IGNORECASE,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _fetch_statements(ticker: str) -> list[dict]:
    """Return all ManagementStatement rows for a ticker."""
    with _conn() as c:
        rows = c.execute(
            "SELECT properties FROM nodes WHERE node_type='ManagementStatement' AND ticker=? ORDER BY created_at",
            (ticker,),
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _extract_pdf_text(ticker: str, max_pages: int = 50) -> str:
    """Try to extract text from concall PDFs for the ticker. Returns '' if none found."""
    dirs_to_check = [
        CONCALL_DIR / ticker,
        REPO / "memory_store" / "annual_reports" / ticker,
    ]
    text_parts = []
    for d in dirs_to_check:
        if not d.exists():
            continue
        pdfs = sorted(d.glob("*.pdf"))
        for pdf_path in pdfs[:2]:  # max 2 PDFs per ticker
            try:
                import pymupdf  # noqa: PLC0415
                doc = pymupdf.open(str(pdf_path))
                for i in range(min(max_pages, len(doc))):
                    text_parts.append(doc[i].get_text())
                doc.close()
            except Exception:
                pass
    return "\n".join(text_parts)


def _sentences(text: str) -> list[str]:
    """Simple sentence splitter."""
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if len(s.strip()) > 20]


def _score_tone(text_lower: str) -> str:
    bull  = sum(text_lower.count(w) for w in BULLISH_TONE_WORDS)
    caut  = sum(text_lower.count(w) for w in CAUTIOUS_TONE_WORDS)
    defen = sum(text_lower.count(w) for w in DEFENSIVE_TONE_WORDS)
    total = bull + caut + defen or 1
    if bull / total >= 0.5:
        return "bullish"
    if defen / total >= 0.4:
        return "defensive"
    return "cautious"


def _extract_guidance(sentences: list[str]) -> list[str]:
    guidance = []
    for s in sentences:
        s_lower = s.lower()
        if any(re.search(p, s_lower) for p in GUIDANCE_PATTERNS[:2]):
            # Must also mention a numeric target or %
            if NUMBER_PATTERN.search(s):
                guidance.append(s[:300])
    return guidance[:10]


def _extract_numbers(text: str) -> list[str]:
    """Extract distinct number mentions."""
    matches = NUMBER_PATTERN.findall(text)
    seen = set()
    out = []
    for m in matches:
        m = m.strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out[:30]


def _top_themes(text_lower: str, top_n: int = 5) -> list[str]:
    scores = {}
    for theme, keywords in THEME_KEYWORDS.items():
        scores[theme] = sum(text_lower.count(kw) for kw in keywords)
    top = sorted(scores.items(), key=lambda x: -x[1])
    return [t for t, s in top if s > 0][:top_n]


def _filter_sentences(sentences: list[str], keywords: list[str], max_n: int = 10) -> list[str]:
    out = []
    for s in sentences:
        s_lower = s.lower()
        if any(kw in s_lower for kw in keywords):
            out.append(s[:300])
    return out[:max_n]


# ── Main Analyzer ─────────────────────────────────────────────────────────────

def analyze_ticker(ticker: str) -> dict | None:
    """
    Analyze concall data for a single ticker.
    Returns structured insight dict, or None if no data found.
    """
    statements = _fetch_statements(ticker)
    pdf_text   = _extract_pdf_text(ticker)
    sources_used = []

    if statements:
        sources_used.append("ManagementStatement_nodes")
    if pdf_text:
        sources_used.append("pdf_text")

    if not statements and not pdf_text:
        return None

    # Combine all text
    all_text = "\n".join(
        s.get("statement", "") for s in statements
        if s.get("statement")
    )
    if pdf_text:
        all_text = pdf_text + "\n" + all_text

    if not all_text.strip():
        return None

    text_lower = all_text.lower()
    sents      = _sentences(all_text)

    guidance        = _extract_guidance(sents)
    key_themes      = _top_themes(text_lower)
    tone            = _score_tone(text_lower)
    risks_flagged   = _filter_sentences(sents, RISK_KEYWORDS, max_n=5)
    positive_sigs   = _filter_sentences(sents, POSITIVE_KEYWORDS, max_n=5)
    specific_numbers = _extract_numbers(all_text)

    # Category breakdown from ManagementStatement nodes
    cat_counts: Counter = Counter(s.get("category", "unknown") for s in statements)

    return {
        "ticker":           ticker,
        "guidance":         guidance,
        "key_themes":       key_themes,
        "tone":             tone,
        "risks_flagged":    risks_flagged,
        "positive_signals": positive_sigs,
        "specific_numbers": specific_numbers,
        "statement_count":  len(statements),
        "category_breakdown": dict(cat_counts),
        "sources_used":     sources_used,
        "analyzed_at":      time.strftime("%Y-%m-%d"),
    }


def _write_node(ticker: str, insight: dict):
    """Write ConcallInsight node to graph.db."""
    from memory.graph_store import GraphStore
    gs   = GraphStore()
    name = f"{ticker}_ConcallInsight_{insight['analyzed_at']}"
    gs.upsert_node(
        node_type="ConcallInsight",
        name=name,
        ticker=ticker,
        properties=insight,
    )


# ── Class API ─────────────────────────────────────────────────────────────────

class ConcallAnalyzer:
    """Batch concall analyzer — processes top tickers from ManagementStatement nodes."""

    def _get_tickers(self, limit: int = 30) -> list[str]:
        with _conn() as c:
            rows = c.execute(
                "SELECT DISTINCT ticker FROM nodes WHERE node_type='ManagementStatement' "
                "AND ticker IS NOT NULL AND ticker != '' ORDER BY ticker"
            ).fetchall()
        tickers = [r[0] for r in rows]
        # Exclude SME/NSE Emerge: typically flagged in company index; apply simple length filter
        tickers = [t for t in tickers if t and len(t) <= 15]
        return tickers[:limit]

    def analyze(self, ticker: str) -> dict:
        try:
            insight = analyze_ticker(ticker)
            if insight is None:
                return {"ticker": ticker, "status": "no_data"}
            _write_node(ticker, insight)
            return {"ticker": ticker, "status": "ok", "tone": insight.get("tone"), "themes": insight.get("key_themes")}
        except Exception as e:
            return {"ticker": ticker, "status": "error", "error": str(e)}

    def run_all(self, tickers: list[str] | None = None, limit: int = 30) -> dict:
        """Run concall analysis on tickers. Returns summary."""
        t0 = time.time()
        from memory.agent_perf import record_run

        if tickers is None:
            tickers = self._get_tickers(limit)

        ok = skipped = errors = 0
        for ticker in tickers:
            r = self.analyze(ticker)
            if r["status"] == "ok":
                ok += 1
            elif r["status"] == "no_data":
                skipped += 1
            else:
                errors += 1

        dur = round(time.time() - t0, 2)
        try:
            record_run("concall_analyzer", ok, errors, dur, "analysis")
        except Exception:
            pass

        return {
            "ok":      ok,
            "skipped": skipped,
            "errors":  errors,
            "tickers": len(tickers),
            "duration_s": dur,
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Concall Analyzer")
    parser.add_argument("--ticker", help="Single ticker to analyze")
    parser.add_argument("--limit", type=int, default=30, help="Max tickers")
    args = parser.parse_args()

    analyzer = ConcallAnalyzer()

    if args.ticker:
        r = analyzer.analyze(args.ticker)
        print(json.dumps(r, indent=2, default=str))
        # Also print full insight
        insight = analyze_ticker(args.ticker)
        if insight:
            print("\n--- Insight ---")
            print(f"  Tone:     {insight['tone']}")
            print(f"  Themes:   {insight['key_themes']}")
            print(f"  Guidance: {insight['guidance'][:3]}")
            print(f"  Risks:    {insight['risks_flagged'][:2]}")
            print(f"  Positives:{insight['positive_signals'][:2]}")
            print(f"  Numbers:  {insight['specific_numbers'][:5]}")
    else:
        r = analyzer.run_all(limit=args.limit)
        print(json.dumps(r, indent=2, default=str))
