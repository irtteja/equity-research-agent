"""
DocumentAgent — extracts management statements from concall transcripts and
annual reports, pushes ManagementStatement nodes into the knowledge graph.

Reads:
  memory_store/documents/<TICKER>/transcript/*.json
  memory_store/documents/<TICKER>/annual_report/*.json

Writes:
  graph nodes: ManagementStatement
  graph edges: CORROBORATES / CONTRADICTS → linked TrendSummary nodes
  memory_store/document_agent_progress.json — checkpoint for resumability

No LLM calls. Pure rule-based sentence extraction by keyword matching.
"""
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

# ── Keyword sets for extraction ───────────────────────────────────────────────

GUIDANCE_KW = {
    "expect", "guidance", "outlook", "target", "anticipate", "forecast",
    "aim to", "plan to", "will achieve", "projecting", "project", "intend",
    "looking at", "going forward", "next quarter", "next year", "fy2", "h2",
    "confident of", "on track to", "guide",
}
RISK_KW = {
    "headwind", "risk", "challenge", "pressure", "concern", "uncertainty",
    "slowdown", "impact", "adverse", "difficult", "weakness", "softness",
    "decline", "macro", "volatile", "cautious", "cautiously", "below",
}
POSITIVE_KW = {
    "strong", "momentum", "growth", "margin expansion", "demand", "pipeline",
    "confident", "outperform", "record", "highest", "best", "robust",
    "accelerat", "ramp", "win", "deal", "order book", "market share",
    "healthy", "significant", "broad-based", "improve",
}
NEGATIVE_SENTIMENT_KW = {
    "decline", "fall", "drop", "lower", "weak", "loss", "concern",
    "pressure", "headwind", "slowdown", "difficult", "below", "miss",
    "negative", "challenge", "risk", "adversely",
}


def _contains_any(text_lower: str, keywords: set) -> bool:
    return any(kw in text_lower for kw in keywords)


def _classify_sentiment(text: str) -> str:
    tl = text.lower()
    pos = sum(1 for kw in POSITIVE_KW if kw in tl)
    neg = sum(1 for kw in NEGATIVE_SENTIMENT_KW if kw in tl)
    if pos > neg:
        return "positive"
    elif neg > pos:
        return "negative"
    return "neutral"


_BOILERPLATE_KW = {
    "stock exchange", "bse limited", "nse limited", "sebi", "listing obligation",
    "pursuant to regulation", "please find enclosed", "company secretary",
    "registered office", "corporate identity", "cin:", "tel ", "fax ",
    "e-mail ", "website www", "dear sir", "yours faithfully", "digitally signed",
    "all participant lines", "conference is being recorded", "touchtone phone",
    "webcast through our website", "financial statements", "quarterly fact sheet",
    "available on our website", "press releases are also", "archive including",
    "please note",
}


def _is_boilerplate(text: str) -> bool:
    tl = text.lower()
    return any(kw in tl for kw in _BOILERPLATE_KW)


def _split_sentences(text: str, skip_chars: int = 1500) -> list[str]:
    # Skip document header (address, filing preamble, legal disclaimer)
    body = text[skip_chars:] if len(text) > skip_chars + 500 else text
    raw = re.split(r'(?<=[.!?])\s+(?=[A-Z])', body)
    sentences = []
    for chunk in raw:
        chunk = re.sub(r'\s+', ' ', chunk).strip()
        # Require minimum length and reject boilerplate
        if len(chunk) >= 80 and not _is_boilerplate(chunk):
            sentences.append(chunk)
    return sentences


def _extract_statements(text: str, max_per_category: int = 3) -> list[dict]:
    """
    Extract categorized management statements from document text.
    Returns list of {category, sentiment, statement} dicts.
    """
    sentences = _split_sentences(text)
    results = []
    counts = {"guidance": 0, "risk": 0, "positive": 0}

    for sent in sentences:
        tl = sent.lower()
        cat = None

        if counts["guidance"] < max_per_category and _contains_any(tl, GUIDANCE_KW):
            cat = "guidance"
        elif counts["risk"] < max_per_category and _contains_any(tl, RISK_KW):
            cat = "risk"
        elif counts["positive"] < max_per_category and _contains_any(tl, POSITIVE_KW):
            cat = "positive"

        if cat:
            sentiment = _classify_sentiment(sent)
            results.append({
                "category":  cat,
                "sentiment": sentiment,
                "statement": sent[:500],
            })
            counts[cat] += 1

        if all(v >= max_per_category for v in counts.values()):
            break

    return results


class DocumentAgent:
    """
    Reads concall transcripts and annual reports, extracts management
    statements, pushes ManagementStatement nodes into the knowledge graph.
    """

    def __init__(self):
        from memory.graph_store import get_store
        self.gs = get_store()
        self._progress_file = REPO / "memory_store" / "document_agent_progress.json"
        self._progress = self._load_progress()

    # ── Public interface ──────────────────────────────────────────────────────

    def process_all(self, limit: int = 0) -> dict:
        doc_dir = REPO / "memory_store" / "documents"
        if not doc_dir.exists():
            return {"tickers": 0, "documents": 0, "statements": 0}

        tickers = sorted(d.name for d in doc_dir.iterdir() if d.is_dir())
        total_docs = total_stmts = processed_tickers = 0

        for ticker in tickers:
            if limit and processed_tickers >= limit:
                break
            if ticker in self._progress.get("done", {}):
                continue

            docs, stmts = self._process_ticker(ticker, doc_dir / ticker)
            total_docs  += docs
            total_stmts += stmts
            processed_tickers += 1
            self._progress.setdefault("done", {})[ticker] = {
                "docs": docs, "stmts": stmts,
                "at": datetime.now().isoformat(),
            }
            self._save_progress()

        return {
            "tickers":    processed_tickers,
            "documents":  total_docs,
            "statements": total_stmts,
        }

    # ── Per-ticker processing ────────────────────────────────────────────────

    def _process_ticker(self, ticker: str, ticker_dir: Path) -> tuple[int, int]:
        docs_processed = stmts_written = 0

        # Collect all document files (transcripts + annual reports)
        sources = [
            (f, "concall")
            for f in (ticker_dir / "transcript").glob("*.json")
            if (ticker_dir / "transcript").exists()
        ] + [
            (f, "annual_report")
            for f in (ticker_dir / "annual_report").glob("*.json")
            if (ticker_dir / "annual_report").exists()
        ]

        # Load existing TrendSummary nodes for this ticker for edge linking
        trend_nodes = self._get_trend_nodes(ticker)

        for fpath, source_type in sources:
            try:
                data = json.loads(fpath.read_text())
                text = data.get("text", "")
                if not text or len(text) < 200:
                    continue

                doc_date = data.get("ingested_at", data.get("period", str(date.today())))
                doc_title = data.get("source_name", fpath.name)[:120]
                doc_id   = data.get("doc_id", fpath.stem)

                statements = _extract_statements(text, max_per_category=3)
                for stmt in statements:
                    node_id = f"MGMT_{ticker}_{doc_id}_{abs(hash(stmt['statement'])) % 10**8:08d}"
                    self.gs.add_node(
                        node_id   = node_id,
                        node_type = "ManagementStatement",
                        name      = f"{ticker} {stmt['category']} ({doc_date})",
                        ticker    = ticker,
                        properties = {
                            "ticker":         ticker,
                            "date":           str(doc_date),
                            "statement":      stmt["statement"],
                            "category":       stmt["category"],
                            "sentiment":      stmt["sentiment"],
                            "source":         source_type,
                            "document_title": doc_title,
                        },
                    )
                    # Link to trend nodes
                    for trend_id, trend_dir in trend_nodes:
                        if stmt["sentiment"] == "positive" and trend_dir == "improving":
                            self.gs.add_edge("CORROBORATES", node_id, trend_id,
                                             {"basis": "positive_sentiment_x_improving_trend"})
                        elif stmt["sentiment"] == "negative" and trend_dir == "improving":
                            self.gs.add_edge("CONTRADICTS", node_id, trend_id,
                                             {"basis": "negative_sentiment_x_improving_trend"})
                    stmts_written += len(statements)

                docs_processed += 1
                break  # one doc per ticker in first pass (most recent transcript)

            except Exception:
                continue

        return docs_processed, stmts_written

    # ── Graph helpers ────────────────────────────────────────────────────────

    def _get_trend_nodes(self, ticker: str) -> list[tuple[str, str]]:
        """Return [(node_id, direction)] for TrendSummary nodes of this ticker."""
        with self.gs._conn() as conn:
            rows = conn.execute(
                "SELECT id, properties FROM nodes WHERE node_type='TrendSummary' AND ticker=?",
                (ticker.upper(),),
            ).fetchall()
        result = []
        for row in rows:
            try:
                props = json.loads(row["properties"]) if isinstance(row["properties"], str) else row["properties"]
                result.append((row["id"], props.get("direction", "stable")))
            except Exception:
                pass
        return result

    # ── Progress checkpoint ──────────────────────────────────────────────────

    def _load_progress(self) -> dict:
        try:
            return json.loads(self._progress_file.read_text())
        except Exception:
            return {"done": {}}

    def _save_progress(self):
        self._progress_file.write_text(json.dumps(self._progress, indent=2, default=str))
