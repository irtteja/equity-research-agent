"""
annual_report_analyzer.py — Extracts structured intelligence from annual report PDFs.

Inputs:
  - PDF files in memory_store/annual_reports/<TICKER>/ (most recent PDF used)

Outputs:
  - AnnualReportInsight node per ticker written to graph.db with fields:
      business_description, revenue_segments, growth_drivers, key_risks,
      capital_allocation, operational_metrics, pdf_path, pages_analyzed

No API calls. No anthropic.Anthropic(). All analysis is keyword/regex/heuristic-based.

Called by: jobs/orchestrator.py (Phase 2 — Analysis)
CLI: python agents/annual_report_analyzer.py [--ticker TICKER] [--limit N]
"""
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

REPO    = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

DB_PATH    = REPO / "memory_store" / "graph.db"
AR_DIR     = REPO / "memory_store" / "annual_reports"


# ── Section markers (heuristics to find MD&A and risk sections) ───────────────

MDA_MARKERS = [
    "management discussion and analysis",
    "management's discussion",
    "discussion and analysis",
    "mda",
    "md&a",
    "directors' report",
    "board's report",
    "management report",
]

RISK_MARKERS = [
    "risk factors",
    "key risks",
    "principal risks",
    "risk management",
    "material risks",
]

CAPEX_KEYWORDS    = ['capex', 'capital expenditure', 'capacity', 'plant', 'investment', 'expand', 'moderniz', 'brownfield', 'greenfield']
DIVIDEND_KEYWORDS = ['dividend', 'buyback', 'buy-back', 'share repurchase', 'return to shareholders']
ACQUISITION_KEYWORDS = ['acquisition', 'merger', 'joint venture', 'subsidiary', 'stake']
SEGMENT_PATTERNS  = [
    r'(\d+(?:\.\d+)?)\s*%\s*(?:of\s+)?(?:revenue|sales|income)',
    r'(?:revenue|sales|income)\s+(?:of\s+)?(?:rs\.?\s*|₹\s*)?(\d[\d,]*(?:\.\d+)?)\s*(?:cr|crore|mn|million)',
    r'segment\s+[\w\s]+(?:revenue|sales|contribution)',
]
NUMBER_WITH_UNIT = re.compile(
    r'(?:rs\.?\s*|inr\s*|₹\s*)?\d[\d,]*(?:\.\d+)?\s*(?:crore|cr|lakhs?|mn|million|billion|bn|%)',
    re.IGNORECASE,
)
KPI_PATTERNS = [
    r'\b(\d[\d,]*)\s+(stores?|outlets?|branches?|centres?|plants?|units?)',
    r'\b(\d[\d,]*(?:\.\d+)?)\s*(mt|mmt|mte|kwh|mw|gwh|tonne|litre|kl)',
    r'\b(\d[\d,]*)\s+(subscribers?|customers?|users?|members?)',
    r'utilisation\s+(?:rate\s+)?(?:of\s+)?(\d+(?:\.\d+)?)\s*%',
    r'capacity\s+(?:utilisation|utilization)\s+(?:at\s+)?(\d+(?:\.\d+)?)\s*%',
]
GROWTH_DRIVER_KEYWORDS = [
    'driven by', 'growth driven', 'growth in', 'demand from', 'increase in',
    'supported by', 'aided by', 'contributed by', 'powered by', 'led by',
    'opportunity', 'tailwind', 'favorable', 'positive', 'strong demand',
    'new products', 'new markets', 'exports', 'premiumization', 'premiumisation',
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _get_latest_pdf(ticker: str) -> Path | None:
    """Return the most recent PDF for a ticker, or None."""
    ticker_dir = AR_DIR / ticker
    if not ticker_dir.exists():
        return None
    pdfs = sorted(ticker_dir.glob("*.pdf"), reverse=True)
    return pdfs[0] if pdfs else None


def _extract_pages(pdf_path: Path, max_pages: int = 50) -> str:
    """Extract text from first max_pages of a PDF."""
    try:
        import pymupdf  # noqa: PLC0415
        doc   = pymupdf.open(str(pdf_path))
        pages = min(max_pages, len(doc))
        text  = "\n".join(doc[i].get_text() for i in range(pages))
        doc.close()
        return text
    except Exception:
        return ""


def _extract_mda_section(pdf_path: Path, max_section_chars: int = 20_000) -> str:
    """
    Scan entire PDF to locate MD&A section. Returns the section text (up to
    max_section_chars). Falls back to first 50 pages if not found.
    Scans in 10-page windows to avoid loading the whole PDF into RAM.
    """
    try:
        import pymupdf  # noqa: PLC0415
        doc   = pymupdf.open(str(pdf_path))
        total = len(doc)

        # Phase 1: quick scan for MD&A by page, in batches of 10
        for start in range(0, total, 10):
            end   = min(start + 10, total)
            chunk = "\n".join(doc[i].get_text() for i in range(start, end))
            chunk_lower = chunk.lower()
            for marker in MDA_MARKERS:
                if marker in chunk_lower:
                    # Found the MD&A region — extract from here
                    idx = chunk_lower.find(marker)
                    tail = chunk[idx:]
                    # Continue reading a few more pages
                    for j in range(end, min(end + 20, total)):
                        tail += doc[j].get_text()
                    doc.close()
                    return tail[:max_section_chars]

        # Fallback: return first 50 pages
        text = "\n".join(doc[i].get_text() for i in range(min(50, total)))
        doc.close()
        return text[:max_section_chars]
    except Exception:
        return ""


def _find_section(text_lower: str, markers: list[str], max_offset: int = 50_000) -> int:
    """Return index of first marker match, within max_offset chars. -1 if not found."""
    for marker in markers:
        idx = text_lower.find(marker)
        if 0 <= idx <= max_offset:
            return idx
    return -1


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if len(s.strip()) > 20]


def _filter_sentences(sents: list[str], keywords: list[str], max_n: int = 5) -> list[str]:
    out = []
    for s in sents:
        s_lower = s.lower()
        if any(kw in s_lower for kw in keywords):
            out.append(s[:350])
    return out[:max_n]


def _extract_numbers(text: str) -> list[str]:
    matches = NUMBER_WITH_UNIT.findall(text)
    seen = set()
    out  = []
    for m in matches:
        m = m.strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out[:20]


def _extract_kpis(text: str) -> list[str]:
    kpis = []
    for pat in KPI_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            kpis.append(m.group(0).strip()[:100])
    return list(dict.fromkeys(kpis))[:10]


def _extract_segments(text: str) -> list[str]:
    segs = []
    for pat in SEGMENT_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            segs.append(m.group(0).strip()[:150])
    return list(dict.fromkeys(segs))[:8]


def _build_business_description(sents: list[str], ticker: str) -> str:
    """
    Heuristic: first 2 sentences from MD&A section that mention the company
    or business activity. Falls back to first 2 non-empty sentences.
    """
    business_words = ['company', 'business', 'manufacture', 'provide', 'offer', 'engaged', 'operates', 'leader', 'products', 'services', 'industry']
    candidates = [s for s in sents[:40] if any(w in s.lower() for w in business_words)]
    if candidates:
        return " ".join(candidates[:2])
    return " ".join(sents[:2]) if sents else ""


# ── Main Analyzer ─────────────────────────────────────────────────────────────

def analyze_ticker(ticker: str) -> dict | None:
    """
    Analyze annual report for a single ticker.
    Returns structured insight dict, or None if no PDF found.
    """
    pdf_path = _get_latest_pdf(ticker)
    if pdf_path is None:
        return None

    full_text   = _extract_pages(pdf_path, max_pages=50)
    if not full_text.strip():
        return None

    text_lower  = full_text.lower()
    all_sents   = _sentences(full_text)

    # Locate MD&A section — use smart scanner for large PDFs
    mda_text = _extract_mda_section(pdf_path)
    if not mda_text:
        mda_text = full_text[:15_000]
    # Strip the section header itself to get to the content
    mda_lower = mda_text.lower()
    mda_start_in_mda = _find_section(mda_lower, MDA_MARKERS, max_offset=len(mda_lower))
    if mda_start_in_mda > -1:
        mda_text  = mda_text[mda_start_in_mda:]
        mda_lower = mda_text.lower()
    mda_sents = _sentences(mda_text)

    # Locate risk section within MD&A text or fall back to full text
    risk_start = _find_section(mda_lower, RISK_MARKERS, max_offset=len(mda_lower))
    if risk_start > -1:
        risk_text  = mda_text[risk_start: risk_start + 8_000]
        risk_sents = _sentences(risk_text)
    else:
        risk_start_full = _find_section(text_lower, RISK_MARKERS)
        if risk_start_full > -1:
            risk_text  = full_text[risk_start_full: risk_start_full + 8_000]
            risk_sents = _sentences(risk_text)
        else:
            risk_sents = []

    # 1. Business description (2 sentences from MD&A)
    business_desc = _build_business_description(mda_sents, ticker)

    # 2. Revenue segments
    revenue_segments = _extract_segments(mda_text)

    # 3. Growth drivers
    growth_drivers = _filter_sentences(mda_sents, GROWTH_DRIVER_KEYWORDS, max_n=5)

    # 4. Key risks (from risk section or entire text)
    if risk_sents:
        key_risks = [s[:300] for s in risk_sents if len(s) > 30][:5]
    else:
        risk_keywords = ['risk', 'challenge', 'headwind', 'uncertainty', 'adverse', 'regulatory']
        key_risks = _filter_sentences(all_sents, risk_keywords, max_n=5)

    # 5. Capital allocation
    capex_mentions     = _filter_sentences(mda_sents, CAPEX_KEYWORDS,    max_n=3)
    dividend_mentions  = _filter_sentences(mda_sents, DIVIDEND_KEYWORDS, max_n=2)
    acq_mentions       = _filter_sentences(mda_sents, ACQUISITION_KEYWORDS, max_n=2)
    capital_allocation = {
        "capex":        capex_mentions,
        "dividends":    dividend_mentions,
        "acquisitions": acq_mentions,
    }

    # 6. Operational metrics / KPIs
    operational_metrics = _extract_kpis(mda_text)

    # 7. Specific numbers from MD&A
    specific_numbers = _extract_numbers(mda_text)

    return {
        "ticker":               ticker,
        "pdf_path":             str(pdf_path.relative_to(REPO)),
        "pages_analyzed":       min(50, len(_extract_pages.__doc__ or "")),  # rough
        "business_description": business_desc[:400],
        "revenue_segments":     revenue_segments,
        "growth_drivers":       growth_drivers,
        "key_risks":            key_risks,
        "capital_allocation":   capital_allocation,
        "operational_metrics":  operational_metrics,
        "specific_numbers":     specific_numbers,
        "analyzed_at":          time.strftime("%Y-%m-%d"),
    }


def _write_node(ticker: str, insight: dict):
    """Write AnnualReportInsight node to graph.db."""
    from memory.graph_store import GraphStore
    gs   = GraphStore()
    name = f"{ticker}_AnnualReportInsight_{insight['analyzed_at']}"
    gs.upsert_node(
        node_type="AnnualReportInsight",
        name=name,
        ticker=ticker,
        properties=insight,
    )


# ── Class API ─────────────────────────────────────────────────────────────────

class AnnualReportAnalyzer:
    """Batch annual report analyzer — processes tickers with AR PDFs available."""

    def _get_tickers(self, limit: int = 20) -> list[str]:
        """Return tickers that have annual report PDFs."""
        if not AR_DIR.exists():
            return []
        tickers = [d.name for d in AR_DIR.iterdir() if d.is_dir() and list(d.glob("*.pdf"))]
        # Exclude SME-looking tickers (heuristic: all-digits or very short)
        tickers = [t for t in tickers if t and len(t) >= 2 and not t.isdigit()]
        tickers.sort()
        return tickers[:limit]

    def analyze(self, ticker: str) -> dict:
        try:
            insight = analyze_ticker(ticker)
            if insight is None:
                return {"ticker": ticker, "status": "no_pdf"}
            _write_node(ticker, insight)
            return {
                "ticker":  ticker,
                "status":  "ok",
                "drivers": len(insight.get("growth_drivers", [])),
                "risks":   len(insight.get("key_risks", [])),
            }
        except Exception as e:
            return {"ticker": ticker, "status": "error", "error": str(e)}

    def run_all(self, tickers: list[str] | None = None, limit: int = 20) -> dict:
        """Run annual report analysis on tickers. Returns summary."""
        t0 = time.time()
        from memory.agent_perf import record_run

        if tickers is None:
            tickers = self._get_tickers(limit)

        ok = skipped = errors = 0
        for ticker in tickers:
            r = self.analyze(ticker)
            if r["status"] == "ok":
                ok += 1
            elif r["status"] == "no_pdf":
                skipped += 1
            else:
                errors += 1

        dur = round(time.time() - t0, 2)
        try:
            record_run("annual_report_analyzer", ok, errors, dur, "analysis")
        except Exception:
            pass

        return {
            "ok":       ok,
            "skipped":  skipped,
            "errors":   errors,
            "tickers":  len(tickers),
            "duration_s": dur,
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Annual Report Analyzer")
    parser.add_argument("--ticker", help="Single ticker to analyze")
    parser.add_argument("--limit",  type=int, default=20, help="Max tickers")
    args = parser.parse_args()

    analyzer = AnnualReportAnalyzer()

    if args.ticker:
        r = analyzer.analyze(args.ticker)
        print(json.dumps(r, indent=2, default=str))
        insight = analyze_ticker(args.ticker)
        if insight:
            print("\n--- Insight ---")
            print(f"  Business:   {insight['business_description'][:150]}")
            print(f"  Segments:   {insight['revenue_segments'][:3]}")
            print(f"  Growth:     {insight['growth_drivers'][:2]}")
            print(f"  Risks:      {insight['key_risks'][:2]}")
            print(f"  CapAlloc:   capex={len(insight['capital_allocation'].get('capex',[]))} mentions")
            print(f"  KPIs:       {insight['operational_metrics'][:3]}")
            print(f"  Numbers:    {insight['specific_numbers'][:5]}")
    else:
        r = analyzer.run_all(limit=args.limit)
        print(json.dumps(r, indent=2, default=str))
