"""
Announcement ingestion pipeline — data preparation only.

Claude (running in-session) does the extraction. This module handles:
  1. Hard-ignore headlines that are always noise
  2. attchmntText pre-filter via memory.triage (skip/light/deep)
  3. PDF download + text extraction for non-skip items
  4. Saving Claude's extraction output to memory_store/

No API calls here. Claude reads the prepared text in-session and calls
save_extraction() with its results.
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
_HARD_IGNORE = [
    r"trading window",
    r"closure of trading window",
    r"shareholding pattern.*reg.*31",
    r"compliance certificate",
    r"statement of investor complaints",
    r"reconciliation of share capital",
    r"intimation of record date.*only",
    r"submission of .*certificate",
    r"board meeting.*to consider.*date",
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


# ─── Extraction prompt (reference for Claude in-session) ─────────────────────
# Claude reads this template and the prepared text, then extracts facts directly.
# No API call — Claude IS the session.

EXTRACTION_PROMPT_TEMPLATE = """You are an equity memory agent for {ticker}.

Company profile:
{profile_json}

Document metadata:
- Category: {category}
- attchmntText: {attchmnt_text}
- Date: {ann_date}
- Pre-triage strategy: {strategy} ({triage_reason})

Document text:
---
{text}
---

Extract ALL atomic facts as a JSON object:
{{
  "triage": "light|deep|ignore",
  "reason": "<one line if ignore>",
  "summary": "<2 sentences>",
  "facts": [
    {{
      "fact_id": "{ticker}-<METRIC>-<PERIOD>-<seq>",
      "raw": {{
        "company": "{ticker}",
        "ticker": "{ticker}",
        "doc_type": "<financial_results|order_win|appointment|dividend|acquisition|...>",
        "doc_name": "<description truncated to 60 chars>",
        "date_accessed": "{today}",
        "source": "<where in doc>",
        "period": "<e.g. Q2-FY27 or null>",
        "period_date": "<YYYY-MM-DD or null>",
        "metric": "<revenue|pat|ebitda|order_value|dividend_per_share|...>",
        "value": <number or null>,
        "unit": "<crore|percent|rs|count|mw|...>",
        "speaker": "<name or null>",
        "speaker_role": "<role or null>",
        "context": "<verbatim excerpt>"
      }},
      "derived": {{
        "previous_value": null,
        "delta": null,
        "delta_direction": "improving|deteriorating|stable|null",
        "anomaly": false,
        "anomaly_note": null,
        "first_appearance": true,
        "contradiction": false,
        "event_links": []
      }},
      "tone": {{"sentiment": "positive|negative|neutral", "tone_shift": null}},
      "rationale": {{
        "significance": "<why this fact matters>",
        "vs_expectations": "<beat/miss/in-line/unknown>",
        "key_takeaway": "<one sentence>",
        "watch_for": "<what to track next>"
      }},
      "confidence": {{
        "source_type": "management_stated|disclosed|calculated|estimated",
        "current_assessment": "high|medium|low",
        "reasoning": "<one line>",
        "last_assessed": "{today}",
        "assessed_by": "claude-ingestion"
      }},
      "event_links": [],
      "extended": {{}}
    }}
  ],
  "events": []
}}

Rules:
- context must be verbatim or near-verbatim, never a paraphrase
- Every number is a fact — don't skip any quantitative statement
- If triage=ignore, output only {{"triage":"ignore","reason":"..."}}
- Output valid JSON only, no markdown fences
"""


# ─── Prepare ─────────────────────────────────────────────────────────────────

def prepare_announcement(
    ticker: str,
    ann: dict,
    profile: dict,
    force_deep: bool = False,
) -> dict:
    """
    Prepare a single announcement for Claude to analyze in-session.

    Returns a dict with:
      status = "ready"  → text extracted, prompt built, Claude should analyze
      status = "skip"   → attchmnt_triage said skip, no PDF needed
      status = "light"  → key fact in attchmntText, light extraction only
      status = "ignore" → hard-ignore pattern matched
      status = "error"  → PDF download or extraction failed

    force_deep=True: bypass triage and download PDF regardless (used for Claude escalations).
    """
    from tools.pdf_parser import download_and_extract

    headline    = ann.get("headline", "") or ann.get("description", "")
    pdf_url     = ann.get("pdf_url", "") or ann.get("attchmntFile", "")
    ann_date    = ann.get("date", "") or ann.get("sort_date", date.today().isoformat())
    attchmnt    = ann.get("attchmntText", "") or ann.get("text", "") or ""
    category    = ann.get("desc", "") or ann.get("category", "")

    # 1. Hard-ignore (never bypassed — genuinely zero value)
    if not force_deep and _is_hard_ignore(headline):
        return {
            "status": "ignore", "ticker": ticker, "ann_date": ann_date[:10],
            "reason": "hard_ignore_pattern", "headline": headline[:150],
        }

    # 2. attchmntText pre-filter (bypassed when force_deep=True)
    strategy, triage_reason = attchmnt_triage(category, attchmnt)
    if not force_deep:
        if strategy == "skip":
            return {
                "status": "skip", "ticker": ticker, "ann_date": ann_date[:10],
                "strategy": "skip", "reason": triage_reason,
                "category": category, "attchmnt_text": attchmnt[:200],
                "headline": headline[:150],
                "ann": ann,  # preserved so Claude can escalate with force_deep
            }
        if strategy == "light":
            return {
                "status": "light", "ticker": ticker, "ann_date": ann_date[:10],
                "strategy": "light", "reason": triage_reason,
                "category": category, "attchmnt_text": attchmnt[:200],
                "headline": headline[:150],
                "ann": ann,  # preserved so Claude can escalate with force_deep
            }
    else:
        triage_reason = f"claude_escalated → was {strategy}: {triage_reason}"
        strategy = "deep"

    # 3. Download PDF
    if not pdf_url:
        return {
            "status": "ignore", "ticker": ticker, "ann_date": ann_date[:10],
            "reason": "no_pdf_attachment", "headline": headline[:150],
        }

    pdf_result = download_and_extract(pdf_url, symbol=ticker)
    if "error" in pdf_result:
        return {
            "status": "error", "ticker": ticker, "ann_date": ann_date[:10],
            "reason": f"pdf_download_failed: {pdf_result['error']}",
            "headline": headline[:150], "pdf_url": pdf_url,
        }

    text = pdf_result.get("text", "")
    # OCR was attempted but failed (tesseract not installed or crashed)
    if len(text) < 50 and pdf_result.get("ocr_error"):
        return {
            "status": "error", "ticker": ticker, "ann_date": ann_date[:10],
            "reason": f"scanned_pdf_ocr_failed: {pdf_result['ocr_error']}", "headline": headline[:150],
        }
    if len(text) < 50:
        return {
            "status": "ignore", "ticker": ticker, "ann_date": ann_date[:10],
            "reason": "pdf_empty_or_image_only", "headline": headline[:150],
        }

    # 4. Build extraction prompt for Claude to use in-session
    profile_json = json.dumps(profile, indent=2, default=str)[:2000]
    prompt = EXTRACTION_PROMPT_TEMPLATE.format(
        ticker=ticker,
        profile_json=profile_json,
        category=category,
        attchmnt_text=attchmnt[:200],
        ann_date=ann_date[:10],
        strategy=strategy,
        triage_reason=triage_reason,
        text=text[:30000],
        today=date.today().isoformat(),
    )

    return {
        "status": "ready",
        "ticker": ticker,
        "ann_date": ann_date[:10],
        "strategy": strategy,
        "triage_reason": triage_reason,
        "category": category,
        "attchmnt_text": attchmnt[:200],
        "headline": headline[:150],
        "pdf_url": pdf_url,
        "text": text[:30000],
        "pdf_pages": pdf_result.get("pages", 0),
        "prompt": prompt,
    }


# ─── Escalation helper (called in-session by Claude after dynamic review) ─────

def escalate_items(skip_light_list: list[dict], indices: list[int], reason: str = "") -> list[dict]:
    """
    Escalate skip/light items to deep by downloading their PDFs.

    Claude calls this after reading the CLAUDE_DYNAMIC_REVIEW block:
      from memory.ingestion import escalate_items
      new_deep = escalate_items(_skip_light_combined, [3, 17, 42], "material order/results content")

    Returns list of ready-status dicts (same format as prepare_announcement status=ready).
    """
    escalated = []
    for idx in indices:
        if idx < 0 or idx >= len(skip_light_list):
            print(f"  [escalate] index {idx} out of range, skipping")
            continue
        item = skip_light_list[idx]
        ticker  = item["ticker"]
        ann     = item.get("ann", {})
        if not ann:
            print(f"  [escalate] {ticker} idx={idx}: no raw ann dict preserved, cannot re-prep")
            continue

        from memory.store import get_profile
        profile = get_profile(ticker)
        result  = prepare_announcement(ticker, ann, profile, force_deep=True)
        if result["status"] == "ready":
            print(f"  [escalate] {ticker} idx={idx} → deep (escalation: {reason})")
        else:
            print(f"  [escalate] {ticker} idx={idx} → {result['status']}: {result.get('reason','')}")
        escalated.append(result)
    return escalated


# ─── Save ─────────────────────────────────────────────────────────────────────

def save_extraction(ticker: str, prep: dict, claude_output: dict) -> dict:
    """
    Save Claude's extraction results to memory store.

    claude_output: the dict Claude returned after analyzing prep["text"].
    Expected keys: triage, facts (list), events (list), summary.
    """
    from memory.store import save_facts, save_triage_log, save_event, update_company_index

    triage   = claude_output.get("triage", "ignore")
    facts    = claude_output.get("facts", [])
    events   = claude_output.get("events", [])
    summary  = claude_output.get("summary", "")
    ann_date = prep.get("ann_date", date.today().isoformat())

    save_triage_log(ticker, [{
        "date": ann_date,
        "decision": triage,
        "reason": claude_output.get("reason", prep.get("triage_reason", "")),
        "headline": prep.get("headline", "")[:150],
        "pdf_url": prep.get("pdf_url", ""),
        "facts_extracted": len(facts),
        "summary": summary,
    }])

    if triage == "ignore":
        return {"triage": "ignore", "facts_saved": 0}

    saved = 0
    if facts:
        result = save_facts(ticker, facts)
        saved  = result.get("saved", 0)
        update_company_index(ticker, {
            "last_filing": ann_date,
            "last_ingested": date.today().isoformat(),
        })

    for ev in events:
        if ev.get("event_id"):
            save_event(ev)

    return {
        "triage": triage,
        "facts_saved": saved,
        "events_created": len(events),
        "summary": summary,
    }


# ─── Company-level helpers ────────────────────────────────────────────────────

def prepare_company(ticker: str, days_back: int = 1) -> list[dict]:
    """
    Prepare all recent announcements for a company.
    Returns list of prep dicts (status=ready|skip|ignore|error).
    Claude iterates these, calls save_extraction() for each ready item.
    """
    from tools.pdf_parser import fetch_nse_announcements
    from memory.store import get_profile

    profile       = get_profile(ticker)
    announcements = fetch_nse_announcements(ticker, days_back=days_back)

    if not announcements:
        return []

    return [prepare_announcement(ticker, ann, profile) for ann in announcements]


# ─── Backwards compatibility alias ───────────────────────────────────────────
# Old callers that used ingest_company() now get prepare_company().
# No anthropic_client param — Claude analyzes the returned prep dicts in-session.

def ingest_company(ticker: str, days_back: int = 1, anthropic_client=None) -> dict:
    """Deprecated wrapper — returns prep list. Ignores anthropic_client."""
    items = prepare_company(ticker, days_back=days_back)
    ready = sum(1 for x in items if x["status"] == "ready")
    skip  = sum(1 for x in items if x["status"] == "skip")
    ignore = sum(1 for x in items if x["status"] in ("ignore", "error"))
    return {
        "ticker": ticker,
        "announcements_processed": len(items),
        "ready_for_analysis": ready,
        "skipped": skip,
        "ignored": ignore,
        "facts_saved": 0,  # Claude hasn't analyzed yet
        "prep_items": items,
    }
