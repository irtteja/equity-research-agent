"""
Announcement ingestion pipeline.

Flow:
  1. Fetch NSE announcements for a symbol (last N days)
  2. Hard-ignore headlines that are always noise
  3. For everything else: download PDF + extract text
  4. Claude triages based on actual content (ignore / light / deep)
  5. Claude extracts atomic facts (light or deep)
  6. Batch save to memory_store/
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.expanduser("~/irtteja/nselib"))

import json
import re
from datetime import date
from pathlib import Path

from memory.triage import triage as attchmnt_triage


# ─── Hard-ignore patterns (never worth parsing) ───────────────────────────────
# These are routine compliance filings with zero analytical value.
_HARD_IGNORE = [
    r"trading window",
    r"closure of trading window",
    r"shareholding pattern.*reg.*31",
    r"compliance certificate",
    r"statement of investor complaints",
    r"reconciliation of share capital",
    r"intimation of record date.*only",
    r"submission of .*certificate",
    r"board meeting.*to consider.*date",  # just setting the date, not results
    r"loss of share certificate",
    r"transfer.*physical.*shares",
    r"demat.*remat",
    r"change in registrar",
    r"appointment of compliance officer",
    r"intimation.*address change",
    r"intimation.*bank account",
]
_HARD_IGNORE_RE = [re.compile(p, re.IGNORECASE) for p in _HARD_IGNORE]


def _is_hard_ignore(headline: str) -> bool:
    h = (headline or "").strip()
    return any(p.search(h) for p in _HARD_IGNORE_RE)


# ─── Claude extraction prompt ─────────────────────────────────────────────────

_EXTRACTION_PROMPT = """You are an equity memory agent for {ticker}.

Company profile loaded:
{profile_json}

Document metadata:
- Description: {description}
- Date: {ann_date}
- Source: NSE filing

Document text:
---
{text}
---

Instructions:
1. TRIAGE: Classify this document as one of:
   - "ignore"  → routine compliance, no analytical value
   - "light"   → some useful metadata but no material numbers
   - "deep"    → financial results, guidance, material event, analyst call

2. If "ignore": output only {"triage": "ignore", "reason": "<one line>"}

3. If "light" or "deep": extract ALL atomic facts as a JSON array.
   Each fact must follow this schema exactly:
   {{
     "fact_id": "<TICKER>-<METRIC>-<PERIOD>-<seq>",
     "raw": {{
       "company": "{ticker}",
       "ticker": "{ticker}",
       "doc_type": "<name it yourself>",
       "doc_name": "<description truncated to 60 chars>",
       "date_accessed": "{today}",
       "source": "<where in doc>",
       "period": "<e.g. Q2-2027 or null>",
       "period_date": "<YYYY-MM-DD or null>",
       "metric": "<name it yourself>",
       "value": <number or null>,
       "unit": "<percent / crore / million / count / etc or null>",
       "speaker": "<name or null>",
       "speaker_role": "<role or null>",
       "context": "<verbatim or near-verbatim excerpt>"
     }},
     "derived": {{
       "previous_value": <from profile or null>,
       "delta": <calculated or null>,
       "delta_direction": "improving/deteriorating/stable/null",
       "anomaly": <true/false>,
       "anomaly_note": "<or null>",
       "first_appearance": <true if metric never seen before>,
       "contradiction": false,
       "event_links": []
     }},
     "tone": {{
       "sentiment": "positive/negative/neutral",
       "tone_shift": null
     }},
     "missing": null,
     "revision": {{"revision_of": null, "revision_type": null, "revision_delta": null}},
     "confidence": {{
       "source_type": "management_stated/disclosed/calculated/estimated",
       "current_assessment": "high/medium/low",
       "reasoning": "<one line>",
       "last_assessed": "{today}",
       "assessed_by": "claude-ingestion",
       "trigger": null
     }},
     "event_links": [],
     "extended": {{}}
   }}

Rules:
- raw.context must be a verbatim or near-verbatim quote, never a paraphrase
- Never skip numbers — every quantitative statement is a fact
- Flag anomalies (unusual values, sharp changes, first appearances, missing metrics)
- If a metric was tracked before (in profile) but is now absent, add a separate "missing" fact
- Attribute every statement to its speaker if identifiable
- Create new event objects if you spot a cross-company or macro pattern
- Output a single JSON object: {{"triage": "light|deep", "facts": [...], "events": [...], "summary": "<2 sentences>"}}
- No markdown, no code fences, just valid JSON
"""


def ingest_announcement(
    ticker: str,
    ann: dict,
    profile: dict,
    anthropic_client=None,
) -> dict:
    """
    Ingest a single announcement dict (from fetch_nse_announcements).
    Returns: { "triage": ..., "facts_saved": N, "summary": ..., "error": ... }
    """
    from tools.pdf_parser import download_and_extract
    from memory.store import save_facts, save_triage_log, save_event, update_company_index

    headline = ann.get("headline", "") or ann.get("description", "")
    pdf_url = ann.get("pdf_url", "")
    ann_date = ann.get("date", date.today().isoformat())

    # Hard-ignore check (no PDF download needed)
    if _is_hard_ignore(headline):
        save_triage_log(ticker, [{
            "date": ann_date,
            "decision": "ignore",
            "reason": "hard_ignore_pattern",
            "headline": headline[:150],
            "pdf_url": pdf_url,
        }])
        return {"triage": "ignore", "reason": "hard_ignore_pattern", "facts_saved": 0}

    # attchmntText pre-filter — skip provably-noise items before PDF download
    attchmnt_text = ann.get("attchmntText", "") or ann.get("text", "") or ""
    category = ann.get("desc", "") or ann.get("category", "")
    pre_strategy, pre_reason = attchmnt_triage(category, attchmnt_text)
    if pre_strategy == "skip":
        save_triage_log(ticker, [{
            "date": ann_date,
            "decision": "skip",
            "reason": f"attchmnt_triage: {pre_reason}",
            "headline": headline[:150],
            "pdf_url": pdf_url,
        }])
        return {"triage": "skip", "reason": pre_reason, "facts_saved": 0}

    # Download and extract PDF
    if not pdf_url:
        save_triage_log(ticker, [{
            "date": ann_date,
            "decision": "ignore",
            "reason": "no_pdf_attachment",
            "headline": headline[:150],
        }])
        return {"triage": "ignore", "reason": "no_pdf_attachment", "facts_saved": 0}

    pdf_result = download_and_extract(pdf_url, symbol=ticker)
    if "error" in pdf_result:
        save_triage_log(ticker, [{
            "date": ann_date,
            "decision": "error",
            "reason": f"pdf_download_failed: {pdf_result['error']}",
            "headline": headline[:150],
            "pdf_url": pdf_url,
        }])
        return {"triage": "error", "error": pdf_result["error"], "facts_saved": 0}

    text = pdf_result.get("text", "")
    if len(text) < 50:
        save_triage_log(ticker, [{
            "date": ann_date,
            "decision": "ignore",
            "reason": "pdf_empty_or_image_only",
            "headline": headline[:150],
            "pdf_url": pdf_url,
        }])
        return {"triage": "ignore", "reason": "pdf_empty_or_image_only", "facts_saved": 0}

    # Build Claude prompt
    prompt = _EXTRACTION_PROMPT.format(
        ticker=ticker,
        profile_json=json.dumps(profile, indent=2, default=str)[:2000],
        description=(ann.get("description") or headline)[:200],
        ann_date=ann_date[:10],
        text=text[:30000],
        today=date.today().isoformat(),
    )

    # Call Claude
    if anthropic_client is None:
        import anthropic
        anthropic_client = anthropic.Anthropic()

    try:
        message = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_response = message.content[0].text
    except Exception as e:
        return {"triage": "error", "error": f"claude_api_failed: {e}", "facts_saved": 0}

    # Parse response
    try:
        # Strip any accidental markdown fences
        clean = re.sub(r'^```json\s*|\s*```$', '', raw_response.strip(), flags=re.MULTILINE)
        result = json.loads(clean)
    except json.JSONDecodeError as e:
        return {
            "triage": "error",
            "error": f"json_parse_failed: {e}",
            "raw_response": raw_response[:500],
            "facts_saved": 0,
        }

    triage = result.get("triage", "ignore")
    facts = result.get("facts", [])
    events = result.get("events", [])
    summary = result.get("summary", "")

    # Log triage
    save_triage_log(ticker, [{
        "date": ann_date,
        "decision": triage,
        "reason": result.get("reason", ""),
        "headline": headline[:150],
        "pdf_url": pdf_url,
        "facts_extracted": len(facts),
        "summary": summary,
    }])

    if triage == "ignore":
        return {"triage": "ignore", "reason": result.get("reason", ""), "facts_saved": 0}

    # Save facts
    saved = 0
    if facts:
        save_result = save_facts(ticker, facts)
        saved = save_result.get("saved", 0)
        update_company_index(ticker, {"last_filing": ann_date[:10], "last_ingested": date.today().isoformat()})

    # Save new events
    for ev in events:
        if ev.get("event_id"):
            save_event(ev)

    return {
        "triage": triage,
        "facts_saved": saved,
        "events_created": len(events),
        "summary": summary,
        "pdf_pages": pdf_result.get("pages", 0),
        "text_chars": len(text),
    }


def ingest_company(ticker: str, days_back: int = 1, anthropic_client=None) -> dict:
    """
    Ingest all recent announcements for a company.
    Returns summary of what was processed.
    """
    from tools.pdf_parser import fetch_nse_announcements
    from memory.store import get_profile

    profile = get_profile(ticker)
    announcements = fetch_nse_announcements(ticker, days_back=days_back)

    if not announcements:
        return {"ticker": ticker, "announcements": 0, "facts_saved": 0, "message": "no new announcements"}

    results = []
    total_facts = 0
    for ann in announcements:
        r = ingest_announcement(ticker, ann, profile, anthropic_client=anthropic_client)
        results.append(r)
        total_facts += r.get("facts_saved", 0)

    return {
        "ticker": ticker,
        "announcements_processed": len(announcements),
        "facts_saved": total_facts,
        "breakdown": results,
    }
