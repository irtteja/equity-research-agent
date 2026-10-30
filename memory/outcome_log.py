"""
Outcome log — record every triage decision and prediction with a timestamp
so they can be reconciled against ground truth later.

Phase 1 of the self-improvement roadmap: log decisions now.
Phase 2 (future): score them once outcomes are known.
Phase 3 (future): use labeled data to fine-tune triage heuristics.

Storage: memory_store/outcome_log.jsonl (one JSON object per line)
Schema per entry:
    ts          ISO timestamp of the decision
    event_type  "triage" | "prediction" | "extraction" | "signal"
    symbol      NSE ticker (if applicable)
    input       short description of what was analyzed
    decision    the label/outcome assigned ("deep" | "skim" | "skip" | price target | etc.)
    confidence  "high" | "medium" | "low"
    rationale   one-line reason
    source      which job/module made the decision
    ground_truth  null until reconciled; filled later as actual outcome becomes known
    scored_at   null until reconciled
    score       null until reconciled (float 0–1, 1 = correct)
"""
import json
import os
from datetime import datetime
from pathlib import Path

_STORE = Path(__file__).parent.parent / "memory_store" / "outcome_log.jsonl"


def log_outcome(
    event_type: str,
    decision: str,
    source: str,
    symbol: str = "",
    input_desc: str = "",
    confidence: str = "medium",
    rationale: str = "",
) -> dict:
    """
    Append one decision to the outcome log.

    event_type: "triage" | "prediction" | "extraction" | "signal"
    decision:   the label assigned ("deep", "skim", "skip", a price target, etc.)
    source:     module that made the decision, e.g. "jobs/triage.py"
    symbol:     NSE ticker, empty if not symbol-specific
    input_desc: brief description of the input (headline, announcement summary, etc.)
    confidence: "high" | "medium" | "low"
    rationale:  one-line reason for the decision

    Returns the entry written.
    """
    entry = {
        "ts":           datetime.now().isoformat(),
        "event_type":   event_type,
        "symbol":       symbol.upper() if symbol else "",
        "input":        input_desc[:300],
        "decision":     decision,
        "confidence":   confidence,
        "rationale":    rationale[:300],
        "source":       source,
        "ground_truth": None,
        "scored_at":    None,
        "score":        None,
    }
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    with open(_STORE, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def reconcile(symbol: str, event_type: str, ground_truth: str, score: float) -> int:
    """
    Fill in ground_truth and score for all unscored entries matching symbol + event_type.
    Returns number of entries updated.
    """
    if not _STORE.exists():
        return 0
    lines = _STORE.read_text().splitlines()
    updated = 0
    out = []
    now = datetime.now().isoformat()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if (entry.get("symbol") == symbol.upper()
                    and entry.get("event_type") == event_type
                    and entry.get("score") is None):
                entry["ground_truth"] = ground_truth
                entry["scored_at"]    = now
                entry["score"]        = score
                updated += 1
            out.append(json.dumps(entry, default=str))
        except json.JSONDecodeError:
            out.append(line)
    _STORE.write_text("\n".join(out) + "\n")
    return updated


def summary(symbol: str = "", event_type: str = "") -> dict:
    """
    Summarise outcome log: total entries, scored count, mean score, by event_type breakdown.
    Optional filter by symbol or event_type.
    """
    if not _STORE.exists():
        return {"total": 0, "scored": 0, "mean_score": None, "by_type": {}}
    entries = []
    for line in _STORE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            if symbol and e.get("symbol") != symbol.upper():
                continue
            if event_type and e.get("event_type") != event_type:
                continue
            entries.append(e)
        except json.JSONDecodeError:
            pass

    scored = [e for e in entries if e.get("score") is not None]
    by_type: dict = {}
    for e in entries:
        t = e.get("event_type", "unknown")
        by_type.setdefault(t, {"total": 0, "scored": 0, "scores": []})
        by_type[t]["total"] += 1
        if e.get("score") is not None:
            by_type[t]["scored"] += 1
            by_type[t]["scores"].append(e["score"])

    for t in by_type:
        sc = by_type[t].pop("scores", [])
        by_type[t]["mean_score"] = round(sum(sc) / len(sc), 3) if sc else None

    return {
        "total":      len(entries),
        "scored":     len(scored),
        "mean_score": round(sum(e["score"] for e in scored) / len(scored), 3) if scored else None,
        "by_type":    by_type,
    }
