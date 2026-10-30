"""
Self-model layer — the agent's accumulated knowledge about itself.

The agent writes observations after each run. Those observations are injected
as context at the start of every future session, so the agent arrives already
knowing its tendencies, blind spots, and what it's been watching.

Storage: memory_store/self_model.json (git-backed, human-readable)
Injection: call as_context() to get a formatted block for session prompts.

Schema:
  patterns.confirmed   — seen 3+ times, high confidence
  patterns.uncertain   — 1-2 data points, needs more evidence
  calibration.works    — signals that have been reliable
  calibration.gaps     — known blind spots
  watch_list           — specific tickers/themes currently being tracked
  sector_notes         — per-sector accumulated observations
  session_log          — last 30 brief per-session notes
"""
import json
from datetime import date
from pathlib import Path

_MODEL_FILE = Path(__file__).parent.parent / "memory_store" / "self_model.json"

_EMPTY = {
    "last_updated":  None,
    "version":       0,
    "patterns": {
        "confirmed":  [],
        "uncertain":  [],
        "rejected":   [],
    },
    "calibration": {
        "works": [],
        "gaps":  [],
    },
    "watch_list":    [],
    "sector_notes":  {},
    "session_log":   [],
}


# ── I/O ────────────────────────────────────────────────────────────────────

def load() -> dict:
    if not _MODEL_FILE.exists():
        return json.loads(json.dumps(_EMPTY))  # deep copy
    with open(_MODEL_FILE) as f:
        return json.load(f)


def save(model: dict):
    _MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    model["last_updated"] = date.today().isoformat()
    model["version"]      = model.get("version", 0) + 1
    with open(_MODEL_FILE, "w") as f:
        json.dump(model, f, indent=2, ensure_ascii=False)


# ── Update helpers ─────────────────────────────────────────────────────────

def apply_reflection(reflection: dict) -> dict:
    """
    Merge Claude's structured reflection output into the self-model.

    reflection schema (Claude outputs this after each run):
    {
      "new_confirmed":  ["pattern text", ...],
      "new_uncertain":  ["pattern text", ...],
      "promote":        ["pattern text to move uncertain→confirmed"],
      "reject":         [{"pattern": "...", "reason": "..."}],
      "works":          ["signal that proved reliable today"],
      "gaps":           ["blind spot identified today"],
      "watch_add":      [{"ticker": "X", "theme": "...", "note": "..."}],
      "watch_remove":   ["ticker or theme to stop watching"],
      "sector_notes":   {"SECTOR": "observation"},
      "session_note":   "one-line summary of today's session",
    }

    Returns the updated model.
    """
    model = load()
    today = date.today().isoformat()

    pats = model["patterns"]
    cal  = model["calibration"]

    # new confirmed patterns
    existing_confirmed = {p["pattern"] for p in pats["confirmed"]}
    for p in reflection.get("new_confirmed", []):
        if p not in existing_confirmed:
            pats["confirmed"].append({"pattern": p, "first_seen": today, "last_seen": today, "count": 1})
        else:
            for item in pats["confirmed"]:
                if item["pattern"] == p:
                    item["count"] = item.get("count", 1) + 1
                    item["last_seen"] = today

    # new uncertain patterns
    existing_uncertain = {p["pattern"] for p in pats["uncertain"]}
    for p in reflection.get("new_uncertain", []):
        if p not in existing_uncertain and p not in existing_confirmed:
            pats["uncertain"].append({"pattern": p, "first_seen": today, "last_seen": today, "count": 1})
        elif p in existing_uncertain:
            for item in pats["uncertain"]:
                if item["pattern"] == p:
                    item["count"] = item.get("count", 1) + 1
                    item["last_seen"] = today

    # promote uncertain → confirmed
    promote_set = set(reflection.get("promote", []))
    if promote_set:
        still_uncertain = []
        for item in pats["uncertain"]:
            if item["pattern"] in promote_set:
                item["last_seen"] = today
                pats["confirmed"].append(item)
            else:
                still_uncertain.append(item)
        pats["uncertain"] = still_uncertain

    # reject patterns
    reject_set = {r["pattern"] if isinstance(r, dict) else r for r in reflection.get("reject", [])}
    if reject_set:
        pats["uncertain"] = [p for p in pats["uncertain"] if p["pattern"] not in reject_set]
        pats["confirmed"] = [p for p in pats["confirmed"] if p["pattern"] not in reject_set]
        for r in reflection.get("reject", []):
            pat  = r["pattern"] if isinstance(r, dict) else r
            why  = r.get("reason", "") if isinstance(r, dict) else ""
            pats["rejected"].append({"pattern": pat, "reason": why, "rejected_on": today})

    # calibration
    existing_works = {w["signal"] for w in cal["works"]}
    for sig in reflection.get("works", []):
        if sig not in existing_works:
            cal["works"].append({"signal": sig, "added": today})

    existing_gaps = {g["gap"] for g in cal["gaps"]}
    for gap in reflection.get("gaps", []):
        if gap not in existing_gaps:
            cal["gaps"].append({"gap": gap, "added": today})

    # watch list
    remove_set = set(reflection.get("watch_remove", []))
    model["watch_list"] = [
        w for w in model["watch_list"]
        if w.get("ticker") not in remove_set and w.get("theme") not in remove_set
    ]
    existing_watch = {w.get("ticker") for w in model["watch_list"]}
    for w in reflection.get("watch_add", []):
        if w.get("ticker") not in existing_watch:
            w["added"] = today
            model["watch_list"].append(w)

    # sector notes
    for sector, note in reflection.get("sector_notes", {}).items():
        if sector not in model["sector_notes"]:
            model["sector_notes"][sector] = []
        model["sector_notes"][sector].append({"note": note, "date": today})
        model["sector_notes"][sector] = model["sector_notes"][sector][-10:]  # keep last 10

    # session log (keep last 30)
    note = reflection.get("session_note", "")
    if note:
        model["session_log"].append({"date": today, "note": note})
        model["session_log"] = model["session_log"][-30:]

    save(model)
    return model


# ── Context injection ──────────────────────────────────────────────────────

def as_context(max_patterns: int = 10, max_watch: int = 10) -> str:
    """
    Return a formatted block for injection into any Claude session.
    Shows confirmed patterns, calibration, current watch list, recent session notes.
    """
    model = load()
    if not model.get("last_updated"):
        return ""

    lines = [
        f"╔══ AGENT SELF-MODEL (v{model['version']}, updated {model['last_updated']}) ══╗",
        "",
    ]

    # confirmed patterns
    confirmed = model["patterns"]["confirmed"][-max_patterns:]
    if confirmed:
        lines.append("Confirmed patterns (seen repeatedly):")
        for p in confirmed:
            cnt = p.get("count", 1)
            lines.append(f"  • [{cnt}x] {p['pattern']}")
        lines.append("")

    # uncertain patterns
    uncertain = model["patterns"]["uncertain"][-5:]
    if uncertain:
        lines.append("Uncertain patterns (needs more data):")
        for p in uncertain:
            lines.append(f"  ? {p['pattern']}")
        lines.append("")

    # calibration
    works = model["calibration"]["works"]
    gaps  = model["calibration"]["gaps"]
    if works or gaps:
        lines.append("Calibration:")
        for w in works[-5:]:
            lines.append(f"  ✓ {w['signal']}")
        for g in gaps[-5:]:
            lines.append(f"  ✗ {g['gap']}")
        lines.append("")

    # watch list
    watch = model["watch_list"][-max_watch:]
    if watch:
        lines.append("Currently watching:")
        for w in watch:
            ticker = w.get("ticker", "")
            theme  = w.get("theme", "")
            note   = w.get("note", "")
            lines.append(f"  → {ticker:12s} {theme} — {note}"[:100])
        lines.append("")

    # recent session notes
    recent = model["session_log"][-3:]
    if recent:
        lines.append("Recent sessions:")
        for s in recent:
            lines.append(f"  [{s['date']}] {s['note']}")
        lines.append("")

    lines.append("╚══ END SELF-MODEL ══╝")
    return "\n".join(lines)


# ── Reflection prompt ──────────────────────────────────────────────────────

def build_reflection_prompt(session_results: dict) -> str:
    """
    Build the prompt Claude reads at end of a daily run to produce a reflection.
    session_results: summary dict from daily_monitor.run().
    """
    current = as_context()

    facts_count   = session_results.get("ingestion", {}).get("ready_for_analysis", 0)
    deep_count    = session_results.get("ingestion", {}).get("rule_deep", 0)
    macro_summary = session_results.get("macro_summary", "")
    fii_dii       = session_results.get("fii_dii", {})
    errors        = [k for k, v in session_results.items() if isinstance(v, dict) and "error" in v]

    return f"""
[DAILY REFLECTION — {date.today().isoformat()}]

Today's session processed {deep_count} deep announcements, {facts_count} items total.
Errors in: {', '.join(errors) if errors else 'none'}.

Macro context:
{macro_summary[:500] if macro_summary else '(not available)'}

FII/DII: {str(fii_dii)[:200]}

Current self-model:
{current if current else '(no prior model — first run)'}

Based on today's analysis, output a JSON reflection:
{{
  "new_confirmed":  ["pattern you saw confirmed again today"],
  "new_uncertain":  ["new pattern you noticed but only once"],
  "promote":        ["pattern from uncertain that was confirmed again"],
  "reject":         [{{"pattern": "...", "reason": "..."}}],
  "works":          ["signal type that proved reliable today"],
  "gaps":           ["blind spot or miss you noticed today"],
  "watch_add":      [{{"ticker": "X", "theme": "...", "note": "one line"}}],
  "watch_remove":   ["ticker or theme no longer relevant"],
  "sector_notes":   {{"SECTOR": "observation"}},
  "session_note":   "one-line summary of today"
}}

Rules:
- Only add a pattern if you genuinely observed it today — not speculation
- Output valid JSON only, no markdown fences
- If nothing notable: output {{"session_note": "quiet session — no notable patterns"}}
"""
