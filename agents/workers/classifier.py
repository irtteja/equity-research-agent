"""
Rule-based document classifier.
Detects: symbol, doc_type, period from filename + first 2000 chars of text.
No LLM calls — fast, deterministic.
"""
import re
from pathlib import Path

# doc_type keyword maps (order matters — first match wins)
_DOC_TYPE_RULES = [
    ("transcript",     re.compile(r"concall|conference.?call|earnings.?call|investor.?call|transcript", re.I)),
    ("annual_report",  re.compile(r"annual.?report|AR\s*20\d\d|year.?end.?report", re.I)),
    ("results",        re.compile(r"quarterly.?result|financial.?result|q[1-4].?result|half.?year|unaudited", re.I)),
    ("investor_pres",  re.compile(r"investor.?present|analyst.?day|capital.?market.?day", re.I)),
    ("regulatory",     re.compile(r"sebi|stock.?exchange|bse|nse|listing|compliance|disclosure", re.I)),
    ("news",           re.compile(r"press.?release|media.?release|news", re.I)),
]

# period patterns
_PERIOD_PATTERNS = [
    re.compile(r"(Q[1-4])\s*FY?\s*(\d{2,4})", re.I),           # Q1 FY26, Q2FY2025
    re.compile(r"FY\s*(\d{2,4})"),                               # FY2025, FY25
    re.compile(r"(H[12])\s*FY?\s*(\d{2,4})", re.I),             # H1 FY26
    re.compile(r"(\d{4})-(\d{2,4})"),                            # 2024-25
    re.compile(r"\b(20\d\d)\b"),                                  # bare year
]


def classify(file_path: str, text: str, known_symbols: set | None = None) -> dict:
    """
    Returns {symbol, doc_type, period, confidence}.
    confidence: 'high' | 'medium' | 'low'
    """
    name = Path(file_path).stem
    probe = (name + " " + text[:2000]).lower()
    full_probe = name + " " + text[:2000]

    doc_type = _detect_doc_type(probe)
    period   = _detect_period(full_probe)
    symbol, sym_conf = _detect_symbol(name, full_probe, known_symbols or set())

    if symbol and doc_type != "misc":
        confidence = "high"
    elif symbol or doc_type != "misc":
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "symbol":     symbol,
        "doc_type":   doc_type,
        "period":     period,
        "confidence": confidence,
    }


def _detect_doc_type(probe: str) -> str:
    for dtype, pattern in _DOC_TYPE_RULES:
        if pattern.search(probe):
            return dtype
    return "misc"


def _detect_period(text: str) -> str | None:
    for pat in _PERIOD_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0).strip()
    return None


def _detect_symbol(filename: str, text: str, known_symbols: set) -> tuple[str | None, str]:
    """Try to find an NSE symbol. Returns (symbol_or_None, confidence)."""
    # 1. Filename might be the symbol or contain it: RELIANCE_AR2025.pdf
    fname_upper = filename.upper().replace("-", "_")
    parts = re.split(r"[\s_\-\.]", fname_upper)
    for part in parts:
        if part and part in known_symbols:
            return part, "high"

    # 2. Look for "NSE: SYMBOL" or "BSE: 500325" style in text
    m = re.search(r"NSE\s*[:/]\s*([A-Z]{2,20})", text, re.I)
    if m:
        sym = m.group(1).upper()
        if not known_symbols or sym in known_symbols:
            return sym, "high"

    m = re.search(r"Symbol\s*[:/]\s*([A-Z]{2,20})", text, re.I)
    if m:
        sym = m.group(1).upper()
        if not known_symbols or sym in known_symbols:
            return sym, "medium"

    # 3. Scan first 500 chars for any known symbol mention (whole word)
    if known_symbols:
        head = text[:500].upper()
        for sym in known_symbols:
            if re.search(r"\b" + re.escape(sym) + r"\b", head):
                return sym, "medium"

    return None, "low"
