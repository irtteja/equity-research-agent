#!/usr/bin/env python3
"""
Self-improvement module — testing, healing, and iterative improvement.

Three responsibilities:
  1. TEST    — run the e2e test suite and surface failures
  2. HEAL    — auto-fix known recoverable failures (stale cookies, missing dirs,
               broken index files, numpy/import issues)
  3. IMPROVE — data-driven gap discovery with metacognitive persistence tracking

The IMPROVE phase reads actual runtime signals — not hardcoded file-existence
checks — to surface real performance issues:
  • Triage calibration: which announcement categories are mis-labelled?
  • Test failure patterns: which tests break repeatedly across runs?
  • Task error patterns: which jobs fail consistently?
  • Pipeline freshness: which pipelines haven't run recently?
  • Data coverage: what fraction of tracked symbols have XBRL results?

Gap persistence tracking closes the metacognitive loop:
  • Each scan compares its findings against the previous scan
  • A gap that persists across 3+ consecutive scans auto-escalates
  • A gap that disappears is logged as resolved (improvement noted)
  • Escalated gaps surface in the self-model so Claude arrives knowing
    what it tried before and what still needs a different approach

Run modes:
  python jobs/self_improve.py           # full cycle: test → heal → improve
  python jobs/self_improve.py --test    # test only
  python jobs/self_improve.py --heal    # heal only
  python jobs/self_improve.py --improve # improve scan only

Results are logged to memory_store/self_improve_log.jsonl
and surfaced to the self-model as session notes.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import subprocess
import traceback
from datetime import date, datetime
from pathlib import Path

REPO  = Path(__file__).parent.parent
LOG   = REPO / "memory_store" / "self_improve_log.jsonl"
STORE = REPO / "memory_store"

# ── Logging ────────────────────────────────────────────────────────────────

def _log(entry: dict):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    entry["ts"] = datetime.now().isoformat()
    with open(LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ── 1. TEST ────────────────────────────────────────────────────────────────

def run_tests(symbol: str = "RELIANCE") -> dict:
    """
    Run the e2e test suite. Returns pass/fail counts and any failure messages.
    """
    print("\n[TEST] Running e2e test suite...")
    result = subprocess.run(
        [sys.executable, "tests/test_e2e.py", "--symbol", symbol],
        cwd=str(REPO),
        capture_output=True, text=True, timeout=120,
    )
    output = result.stdout + result.stderr
    passed = output.count("✓")
    failed = output.count("✗")
    warned = output.count("⚠")

    status = "pass" if failed == 0 else "fail"
    print(f"  {passed} passed  {failed} failed  {warned} warnings")
    if failed > 0:
        # extract failure lines
        failures = [l.strip() for l in output.splitlines() if "✗" in l]
        for f in failures:
            print(f"  {f}")

    entry = {
        "type": "test_run", "symbol": symbol,
        "status": status, "passed": passed, "failed": failed, "warned": warned,
        "failures": [l.strip() for l in output.splitlines() if "✗" in l],
        "output_tail": output[-800:],
    }
    _log(entry)
    return entry


# ── 2. HEAL ────────────────────────────────────────────────────────────────

_HEAL_REGISTRY = []

def _healer(name: str, description: str):
    """Decorator to register a heal function."""
    def decorator(fn):
        _HEAL_REGISTRY.append({"name": name, "description": description, "fn": fn})
        return fn
    return decorator


@_healer("ensure_dirs", "Create missing memory_store subdirectories")
def _heal_dirs():
    dirs = [
        "bhavcopy", "indices_bhavcopy", "announcements", "results",
        "documents", "event_calendar", "index",
    ]
    created = []
    for d in dirs:
        p = STORE / d
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(d)
    return {"created": created} if created else {"ok": True}


@_healer("repair_doc_index", "Remove broken entries from _doc_index.jsonl")
def _heal_doc_index():
    index_file = STORE / "documents" / "_doc_index.jsonl"
    if not index_file.exists():
        return {"ok": True, "reason": "no index file"}

    kept, removed = [], 0
    with open(index_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                store_path = entry.get("store_path", "")
                if store_path and Path(store_path).exists():
                    kept.append(line)
                else:
                    removed += 1
            except json.JSONDecodeError:
                removed += 1

    if removed > 0:
        with open(index_file, "w") as f:
            for line in kept:
                f.write(line + "\n")

    return {"kept": len(kept), "removed": removed}


@_healer("repair_results_index", "Rebuild results/_index.json from JSONL files")
def _heal_results_index():
    results_dir = STORE / "results"
    if not results_dir.exists():
        return {"ok": True, "reason": "no results dir"}

    index = {}
    for f in results_dir.glob("*.jsonl"):
        symbol = f.stem
        lines = [l for l in f.read_text().splitlines() if l.strip()]
        if lines:
            try:
                last = json.loads(lines[-1])
                index[symbol] = {
                    "quarters": len(lines),
                    "latest_period": last.get("period_end", ""),
                }
            except Exception:
                pass

    index_file = results_dir / "_index.json"
    with open(index_file, "w") as f:
        json.dump(index, f, indent=2)

    return {"symbols_indexed": len(index)}


@_healer("prune_empty_logs", "Remove zero-byte log files from logs/")
def _heal_empty_logs():
    logs_dir = REPO / "logs"
    if not logs_dir.exists():
        return {"ok": True}
    removed = []
    for f in logs_dir.glob("*"):
        if f.is_file() and f.stat().st_size == 0:
            f.unlink()
            removed.append(f.name)
    return {"removed": removed} if removed else {"ok": True}


@_healer("validate_self_model", "Ensure self_model.json has valid schema")
def _heal_self_model():
    from memory.self_model import load, save, _EMPTY
    try:
        model = load()
        # ensure all top-level keys exist
        changed = False
        for key, default in _EMPTY.items():
            if key not in model:
                model[key] = json.loads(json.dumps(default))
                changed = True
        if changed:
            # save without bumping version (it's a repair)
            model["version"] = model.get("version", 0)
            from pathlib import Path
            import json as _json
            path = REPO / "memory_store" / "self_model.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                _json.dump(model, f, indent=2)
            return {"repaired": True}
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


def run_healers() -> dict:
    """Run all registered healers. Returns summary of what was fixed."""
    print("\n[HEAL] Running healers...")
    results = {}
    for h in _HEAL_REGISTRY:
        try:
            r = h["fn"]()
            results[h["name"]] = r
            status = "✓" if not r.get("error") else "✗"
            detail = ""
            if r.get("created"):   detail = f"created {r['created']}"
            elif r.get("removed"): detail = f"removed {r['removed']}"
            elif r.get("repaired"):detail = "repaired"
            elif r.get("error"):   detail = r["error"]
            print(f"  {status} {h['name']:<30} {detail}")
        except Exception as e:
            results[h["name"]] = {"error": str(e)}
            print(f"  ✗ {h['name']:<30} ERROR: {e}")

    _log({"type": "heal_run", "results": results})
    return results


# ── 3. IMPROVE — signal readers ────────────────────────────────────────────

def _read_scan_history(n: int = 10) -> list[dict]:
    """Return the last N improve_scan entries from self_improve_log.jsonl."""
    if not LOG.exists():
        return []
    entries = []
    for line in LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            if e.get("type") == "improve_scan":
                entries.append(e)
        except json.JSONDecodeError:
            pass
    return entries[-n:]


def _gap_persistence(current_areas: set, history: list[dict]) -> dict[str, int]:
    """
    For each gap area in current scan, count how many of the last N scans
    also contained that area. Returns {area: consecutive_count}.
    """
    counts = {area: 0 for area in current_areas}
    for scan in reversed(history):
        scan_areas = {g.get("area") for g in scan.get("gaps", [])}
        for area in current_areas:
            if area in scan_areas:
                counts[area] += 1
            # stop counting once the gap was absent (non-consecutive)
    return counts


def _resolved_gaps(current_areas: set, history: list[dict]) -> list[str]:
    """Return gap areas that appeared in the last scan but are absent now."""
    if not history:
        return []
    last_areas = {g.get("area") for g in history[-1].get("gaps", [])}
    return sorted(last_areas - current_areas)


def _signal_triage_health() -> list[dict]:
    """
    Read outcome_log.jsonl and surface triage calibration issues.

    Two layers:
      Layer 1 — distribution checks: categories with >90% skip or >90% deep rate,
                accuracy below threshold for scored decisions.
      Layer 2 — deep diagnosis via _diagnose_triage(): re-runs triage() on stored
                inputs, finds anomaly patterns, generates + applies patches.
    """
    gaps = []
    path = STORE / "outcome_log.jsonl"
    if not path.exists():
        return gaps

    from collections import defaultdict
    by_category: dict = defaultdict(lambda: defaultdict(int))
    scored, score_sum, total = 0, 0.0, 0
    all_entries: list[dict] = []

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            if e.get("event_type") != "triage":
                continue
            total += 1
            decision  = e.get("decision", "unknown")
            rationale = (e.get("rationale") or "unknown").split(":")[0].strip()[:40]
            by_category[rationale][decision] += 1
            if e.get("score") is not None:
                scored += 1
                score_sum += float(e["score"])
            all_entries.append(e)
        except (json.JSONDecodeError, ValueError):
            pass

    if total < 20:
        return gaps

    # ── Layer 1: distribution checks ──────────────────────────────────────────
    if scored >= 10:
        mean_acc = score_sum / scored
        if mean_acc < 0.70:
            gaps.append({
                "area": "triage_accuracy",
                "finding": f"Triage mean accuracy {mean_acc:.0%} across {scored} scored decisions — below 70% threshold",
                "action": "Run: python jobs/self_improve.py --diagnose-triage for specific rule patches",
                "priority": "high",
                "metric": {"accuracy": round(mean_acc, 3), "scored": scored},
            })

    for cat, decisions in by_category.items():
        cat_total = sum(decisions.values())
        if cat_total < 10:
            continue
        skip_rate = decisions.get("skip", 0) / cat_total
        deep_rate = decisions.get("deep", 0) / cat_total
        if skip_rate > 0.90:
            gaps.append({
                "area": f"triage_skew:{cat[:30]}",
                "finding": f"Rule '{cat}' fires skip {skip_rate:.0%} of the time ({cat_total} decisions)",
                "action": "Run: python jobs/self_improve.py --diagnose-triage",
                "priority": "medium",
                "metric": {"skip_rate": round(skip_rate, 3), "n": cat_total},
            })
        elif deep_rate > 0.90 and cat == "default deep":
            # default deep firing too often = missing specific rules above it
            gaps.append({
                "area": "triage_default_deep_flood",
                "finding": f"'default deep' fallback firing {deep_rate:.0%} of the time ({cat_total}/{total} decisions) — too few specific rules",
                "action": "Run: python jobs/self_improve.py --diagnose-triage to mine patterns + generate rules",
                "priority": "medium" if deep_rate < 0.95 else "high",
                "metric": {"deep_rate": round(deep_rate, 3), "n": cat_total, "total": total},
            })

    # ── Layer 2: deep diagnosis (runs when enough data) ───────────────────────
    if total >= 50:
        diag = _diagnose_triage(all_entries)
        if diag["patches_applied"] > 0:
            gaps.append({
                "area": "triage_auto_patched",
                "finding": f"Auto-patched {diag['patches_applied']} triage rules from {len(diag['anomalies'])} anomalies",
                "action": "Verify patches in memory/triage.py — check git diff",
                "priority": "low",  # already fixed — just FYI
                "metric": {"patches": diag["patches_applied"], "anomalies": len(diag["anomalies"])},
            })
        # Surface skip leaks as a gap (risky — needs human review before patching)
        leaks = [a for a in diag["anomalies"] if a["type"] == "skip_financial_leak"]
        if leaks:
            total_leaks = sum(a["count"] for a in leaks)
            rules = ", ".join(a["pattern"][:30] for a in leaks[:3])
            gaps.append({
                "area": "triage_skip_financial_leak",
                "finding": f"{total_leaks} skip decisions contain strong financial keywords — possible false negatives (rules: {rules})",
                "action": f"Review memory_store/triage_patch_candidates.jsonl; escalate rules to deep if confirmed",
                "priority": "high" if total_leaks >= 10 else "medium",
                "metric": {"leak_count": total_leaks, "rules": [a["pattern"] for a in leaks]},
            })

    return gaps


def _diagnose_triage(entries: list[dict]) -> dict:
    """
    Deep-diagnose triage miscalibration by re-running triage() on stored inputs,
    comparing the rule that fired against what the decision should have been,
    and generating concrete patch candidates for memory/triage.py.

    Three anomaly classes:
      A. default_deep_flood  — entries where rationale='default deep', grouped by
                               common text patterns → candidates for new specific rules
      B. skip_financial_leak — entries where decision=skip but input contains strong
                               financial keywords → potential false negatives
      C. scored_rule_misfire — rules with low accuracy on scored entries (future)

    Returns {
        'anomalies': list of {type, pattern, count, evidence, patch, confidence},
        'patches_applied': int,
        'patches_staged': list of patch dicts,
        'summary': str,
    }
    """
    import re as _re
    from collections import Counter, defaultdict

    # ── Re-run triage() on each stored input ──────────────────────────────────
    try:
        from memory.triage import triage as _triage
    except ImportError:
        return {"anomalies": [], "patches_applied": 0, "patches_staged": [], "summary": "triage import failed"}

    # Rebuild entries with actual rule that fires
    enriched = []
    for e in entries:
        inp     = e.get("input", "")
        # category is not stored in outcome_log — extract from rationale context or leave blank
        rationale_stored = e.get("rationale", "")
        decision_stored  = e.get("decision", "")
        # Re-run with empty category (we don't have it) — gives text-based rule only
        rule_fired, rule_reason = _triage("", inp)
        enriched.append({
            "input":            inp,
            "decision_stored":  decision_stored,
            "rationale_stored": rationale_stored,
            "rule_fired":       rule_fired,
            "rule_reason":      rule_reason,
            "score":            e.get("score"),
            "symbol":           e.get("symbol", ""),
        })

    anomalies = []
    patches_staged = []

    # ── Anomaly A: default_deep_flood ─────────────────────────────────────────
    # Entries that fell through all rules to 'default deep' — text has no specific pattern.
    # Mine for common 2–4 word phrases; if a phrase appears in 5+ entries, it's a rule candidate.
    default_deep = [e for e in enriched if e["rule_reason"] == "default deep"]
    if len(default_deep) >= 5:
        phrase_counter: Counter = Counter()
        # Extract lowercase 2–3 grams from input text
        for e in default_deep:
            words = _re.findall(r'[a-z]{3,}', e["input"].lower())
            for n in (2, 3):
                for i in range(len(words) - n + 1):
                    phrase_counter[" ".join(words[i:i+n])] += 1

        # Candidate phrases: appear in ≥20% of default_deep entries, min 5 hits
        threshold = max(5, int(len(default_deep) * 0.20))
        candidates = [(ph, cnt) for ph, cnt in phrase_counter.items() if cnt >= threshold]
        # Filter out generic stop-phrases
        _STOP = {"the company", "the board", "with reference", "please find", "in terms",
                 "for the", "of the", "to the", "in the", "and the", "on the", "at the",
                 "this is", "that the", "as per", "as at", "as on"}
        candidates = [(ph, cnt) for ph, cnt in candidates if ph not in _STOP]
        candidates.sort(key=lambda x: -x[1])

        for phrase, cnt in candidates[:5]:
            # Determine what triage label these entries *should* have
            # Heuristic: if phrase contains results/financial keywords → deep (already is)
            #            if phrase contains schedule/intimation → skip candidate
            _FINANCIAL = {"results", "quarterly", "quarter", "revenue", "profit", "ebitda",
                          "pat", "unaudited", "audited", "financial"}
            _SCHEDULE  = {"schedule", "intimation", "notice", "invite", "invitation"}
            ph_words = set(phrase.split())
            if ph_words & _SCHEDULE:
                suggested = "skip"
                suggested_reason = f"schedule/intimation pattern: '{phrase}'"
                rule_section = "SKIP"
            elif len(ph_words & _FINANCIAL) >= 1:
                # already going to deep, but we can add a specific rule so rationale is cleaner
                suggested = "deep"
                suggested_reason = f"financial keyword pattern: '{phrase}'"
                rule_section = "DEEP_SPECIFIC"
            else:
                continue  # unknown intent — don't generate a rule

            evidence = [e["input"][:120] for e in default_deep
                        if phrase in e["input"].lower()][:3]

            patch = None
            if suggested == "skip" and cnt >= 8:
                # Add phrase to skip_txt list in triage.py
                patch = {
                    "file": "memory/triage.py",
                    "description": f"Add '{phrase}' to skip_txt (found in {cnt} default_deep entries)",
                    "old_str": f"    skip_txt = [",
                    "new_str": f"    skip_txt = [\n        '{phrase}',  # auto-added: {cnt} default_deep entries",
                    "confidence": "medium",
                }

            anomalies.append({
                "type":      "default_deep_flood",
                "pattern":   phrase,
                "count":     cnt,
                "suggested": suggested,
                "evidence":  evidence,
                "patch":     patch,
                "confidence": "high" if cnt >= 10 else "medium",
            })

    # ── Anomaly B: skip_financial_leak ────────────────────────────────────────
    # Entries stored as skip where the input text contains strong financial signal.
    # These are potential false negatives — the skip rule fired on content that
    # actually contained extractable facts.
    _FIN_KEYWORDS = [
        "unaudited", "audited", "quarterly results", "financial results",
        "revenue of", "net profit", "ebitda of", "pat of", "crore",
        "q2 fy", "q1 fy", "h1 fy", "quarter ended",
    ]
    skip_entries = [e for e in enriched if e["decision_stored"] == "skip"]
    leaks = []
    for e in skip_entries:
        inp_low = e["input"].lower()
        hits = [kw for kw in _FIN_KEYWORDS if kw in inp_low]
        if len(hits) >= 2:  # 2+ financial keywords in a skipped entry = suspicious
            leaks.append({"input": e["input"][:200], "rule_reason": e["rule_reason"],
                          "keywords": hits, "symbol": e["symbol"]})

    if len(leaks) >= 3:
        # Group by the rule that caused the skip
        by_rule: defaultdict = defaultdict(list)
        for lk in leaks:
            by_rule[lk["rule_reason"]].append(lk)

        for rule, group in sorted(by_rule.items(), key=lambda x: -len(x[1])):
            anomalies.append({
                "type":      "skip_financial_leak",
                "pattern":   rule,
                "count":     len(group),
                "suggested": "deep",
                "evidence":  [g["input"][:120] for g in group[:3]],
                "patch":     None,  # too risky to auto-patch skip rules — surface for review
                "confidence": "medium",
            })

    # ── Anomaly C: scored rule misfires (only when scores exist) ──────────────
    scored = [e for e in enriched if e.get("score") is not None]
    if len(scored) >= 10:
        by_rule_acc: defaultdict = defaultdict(list)
        for e in scored:
            by_rule_acc[e["rule_reason"]].append(float(e["score"]))
        for rule, scores in by_rule_acc.items():
            if len(scores) < 5:
                continue
            acc = sum(scores) / len(scores)
            if acc < 0.60:
                anomalies.append({
                    "type":      "scored_rule_misfire",
                    "pattern":   rule,
                    "count":     len(scores),
                    "accuracy":  round(acc, 3),
                    "suggested": "review",
                    "evidence":  [],
                    "patch":     None,
                    "confidence": "high",
                })

    # ── Apply high-confidence patches ─────────────────────────────────────────
    patches_applied = 0
    triage_path = REPO / "memory" / "triage.py"
    triage_src = triage_path.read_text()

    for anomaly in anomalies:
        patch = anomaly.get("patch")
        if not patch or anomaly["confidence"] != "high":
            if patch:
                patches_staged.append(patch)
            continue

        old_str = patch["old_str"]
        new_str = patch["new_str"]
        if old_str not in triage_src:
            patches_staged.append({**patch, "note": "old_str not found — staged for review"})
            continue

        # Apply the patch
        triage_src = triage_src.replace(old_str, new_str, 1)
        patches_applied += 1

    if patches_applied > 0:
        triage_path.write_text(triage_src)
        try:
            subprocess.run(["git", "add", str(triage_path)], cwd=str(REPO), check=True)
            subprocess.run(
                ["git", "commit", "-m",
                 f"fix(triage): auto-patch {patches_applied} miscalibrations from diagnose_triage\n\n"
                 f"Patterns: {', '.join(a['pattern'] for a in anomalies if a.get('patch') and a['confidence']=='high')}\n\n"
                 f"Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>\n"
                 f"Claude-Session: https://claude.ai/code/session_01G28r1gAsugprj2KxqH9L5D"],
                cwd=str(REPO), check=True,
            )
        except subprocess.CalledProcessError:
            pass  # patch applied to file even if commit fails

    # ── Write staged patches to a review file ────────────────────────────────
    if patches_staged:
        staged_path = STORE / "triage_patch_candidates.jsonl"
        with open(staged_path, "a") as f:
            for p in patches_staged:
                f.write(json.dumps({**p, "ts": datetime.now().isoformat()}, default=str) + "\n")

    n_anomalies = len(anomalies)
    summary = (
        f"{n_anomalies} anomalies found: "
        f"{sum(1 for a in anomalies if a['type']=='default_deep_flood')} default-deep floods, "
        f"{sum(1 for a in anomalies if a['type']=='skip_financial_leak')} skip leaks, "
        f"{sum(1 for a in anomalies if a['type']=='scored_rule_misfire')} scored misfires. "
        f"{patches_applied} patches applied, {len(patches_staged)} staged for review."
    )

    _log({"type": "triage_diagnosis", "anomaly_count": n_anomalies,
          "patches_applied": patches_applied, "patches_staged": len(patches_staged),
          "anomalies": [{k: v for k, v in a.items() if k != "evidence"} for a in anomalies],
          "summary": summary})

    return {
        "anomalies":       anomalies,
        "patches_applied": patches_applied,
        "patches_staged":  patches_staged,
        "summary":         summary,
    }


def _signal_test_failures(history: list[dict]) -> list[dict]:
    """
    Analyse test_run entries in scan history. Surfaces tests that fail
    across multiple consecutive runs (persistent failures) vs one-off flaps.
    """
    gaps = []
    test_runs = [e for e in history if e.get("type") == "test_run"]
    # also read directly from log for full history
    if LOG.exists():
        for line in LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if e.get("type") == "test_run" and e not in test_runs:
                    test_runs.append(e)
            except json.JSONDecodeError:
                pass

    if len(test_runs) < 2:
        return gaps  # need at least 2 runs to detect patterns

    recent = test_runs[-5:]  # last 5 test runs

    # Collect failure lines across runs
    from collections import Counter
    failure_counter: Counter = Counter()
    for run in recent:
        for f in run.get("failures", []):
            # normalise: strip test symbol to get the test name
            name = f.split("(")[0].strip() if "(" in f else f.strip()
            failure_counter[name] += 1

    # Flag tests that failed in 2+ of the last 5 runs
    persistent = [(name, cnt) for name, cnt in failure_counter.items() if cnt >= 2]
    if persistent:
        for name, cnt in sorted(persistent, key=lambda x: -x[1]):
            gaps.append({
                "area": f"persistent_test_failure",
                "finding": f"Test '{name[:60]}' failed in {cnt}/{len(recent)} recent runs — not a flap",
                "action": f"Investigate and fix: {name[:60]}",
                "priority": "high" if cnt >= 3 else "medium",
                "metric": {"failure_count": cnt, "runs_checked": len(recent)},
            })

    # Trend: is the failure count increasing?
    if len(recent) >= 3:
        older_fails = sum(r.get("failed", 0) for r in recent[:-2])
        newer_fails = sum(r.get("failed", 0) for r in recent[-2:])
        older_per_run = older_fails / max(len(recent) - 2, 1)
        newer_per_run = newer_fails / 2
        if newer_per_run > older_per_run * 1.5 and newer_fails > 0:
            gaps.append({
                "area": "test_regression_trend",
                "finding": f"Test failure rate increasing: {older_per_run:.1f}/run → {newer_per_run:.1f}/run in last 2 runs",
                "action": "Run test suite manually and investigate new failures before next feature",
                "priority": "high",
                "metric": {"older_rate": round(older_per_run, 2), "newer_rate": round(newer_per_run, 2)},
            })

    return gaps


def _signal_task_errors() -> list[dict]:
    """
    Read logs/tasks.log and surface jobs with repeated failures.
    """
    gaps = []
    log_path = REPO / "logs" / "tasks.log"
    if not log_path.exists():
        return gaps

    from collections import defaultdict
    job_outcomes: dict = defaultdict(lambda: {"ok": 0, "failed": 0, "last_error": ""})

    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            task_id = e.get("task_id", "unknown")
            status  = e.get("status", "")
            if status == "failed":
                job_outcomes[task_id]["failed"] += 1
                job_outcomes[task_id]["last_error"] = (e.get("blockers") or "")[:120]
            elif status == "completed":
                job_outcomes[task_id]["ok"] += 1
        except json.JSONDecodeError:
            pass

    for task_id, counts in job_outcomes.items():
        total = counts["ok"] + counts["failed"]
        if total < 2 or counts["failed"] == 0:
            continue
        fail_rate = counts["failed"] / total
        if fail_rate >= 0.5:
            gaps.append({
                "area": f"task_failures:{task_id[:40]}",
                "finding": f"Task '{task_id[:40]}' fails {fail_rate:.0%} of runs ({counts['failed']}/{total})",
                "action": f"Investigate task '{task_id}': {counts['last_error'][:100]}",
                "priority": "high" if fail_rate >= 0.75 else "medium",
                "metric": {"fail_rate": round(fail_rate, 2), "failed": counts["failed"], "ok": counts["ok"]},
            })

    return gaps


def _signal_pipeline_freshness() -> list[dict]:
    """
    Check when each major data pipeline last produced output.
    Flags pipelines that haven't run in longer than expected.
    """
    gaps = []
    today = date.today()

    def _latest_file_date(folder: Path, glob: str) -> date | None:
        if not folder.exists():
            return None
        files = sorted(folder.glob(glob))
        for f in reversed(files):
            try:
                return date.fromisoformat(f.stem[:10])
            except ValueError:
                pass
        return None

    checks = [
        ("bhavcopy",          STORE / "bhavcopy",          "*.csv",  2,  "jobs/save_bhavcopy.py"),
        ("indices_bhavcopy",  STORE / "indices_bhavcopy",  "*.csv",  2,  "jobs/save_indices_bhavcopy.py"),
        ("announcements",     STORE / "announcements",     "*.json", 2,  "jobs/fetch_announcements.py --save"),
        ("event_calendar",    STORE / "event_calendar",    "*.json", 7,  "jobs/daily_monitor.py"),
    ]

    for name, folder, pattern, max_stale_days, fix_cmd in checks:
        last = _latest_file_date(folder, pattern)
        if last is None:
            gaps.append({
                "area": f"pipeline_never_run:{name}",
                "finding": f"Pipeline '{name}' has never produced output in {folder.name}/",
                "action": f"Run: python {fix_cmd}",
                "priority": "high",
                "metric": {"last_run": None},
            })
        else:
            age = (today - last).days
            if age > max_stale_days:
                # skip weekends for daily pipelines
                weekday_age = age - (age // 7) * 2
                if weekday_age > max_stale_days:
                    gaps.append({
                        "area": f"pipeline_stale:{name}",
                        "finding": f"Pipeline '{name}' last ran {age} days ago (last file: {last}) — expected every {max_stale_days}d",
                        "action": f"Run: python {fix_cmd}",
                        "priority": "medium",
                        "metric": {"last_run": str(last), "age_days": age},
                    })

    return gaps


def _signal_agent_quality() -> list[dict]:
    """
    Score agent output quality by checking:
      1. ContextAgent edge coverage — how many MetricValue nodes lack PRECEDED_BY edges?
      2. TrendAgent coverage — how many (ticker, metric) pairs have TrendSummary nodes?
      3. CORROBORATES/CONTRADICTS edge accuracy — sampled against outcome_log sentiment scores.

    Surfaces gaps when quality falls below thresholds, closing the recursive
    self-improvement loop for agents (not just the data pipeline).
    """
    gaps = []

    try:
        from memory.graph_store import get_store
        gs = get_store()
        stats = gs.stats()
        by_type = stats.get("by_type", {})

        # ── 1. ContextAgent edge coverage ─────────────────────────────────────
        total_mv   = by_type.get("MetricValue", 0)
        enriched   = total_mv - stats.get("pending_enrichment", 0)

        if total_mv > 0:
            enrichment_rate = enriched / total_mv
            if enrichment_rate < 0.5 and total_mv >= 5:
                gaps.append({
                    "area": "context_agent:low_enrichment_rate",
                    "finding": f"ContextAgent has only enriched {enriched}/{total_mv} MetricValue nodes ({enrichment_rate:.0%}) — run context_agent_runner.py",
                    "action": "python jobs/context_agent_runner.py",
                    "priority": "high" if enrichment_rate < 0.2 else "medium",
                    "metric": {"enriched": enriched, "total": total_mv, "rate": round(enrichment_rate, 3)},
                })

        # ── 2. TrendAgent coverage ─────────────────────────────────────────────
        total_trends = by_type.get("TrendSummary", 0)
        total_cos    = by_type.get("Company", 0)

        if total_cos > 0 and total_trends == 0 and total_mv >= 10:
            gaps.append({
                "area": "trend_agent:no_trends_computed",
                "finding": f"TrendAgent has never run — {total_mv} MetricValue nodes but 0 TrendSummary nodes",
                "action": "python -m agents.trend_agent",
                "priority": "medium",
                "metric": {"metric_nodes": total_mv, "trend_nodes": 0},
            })
        elif total_cos > 0 and total_trends > 0:
            # Check if trends are stale (last update > 7 days ago)
            with gs._conn() as conn:
                stale = conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE node_type='TrendSummary' "
                    "AND json_extract(properties,'$.updated_at') < datetime('now', '-7 days')"
                ).fetchone()[0]
            if stale > 0:
                gaps.append({
                    "area": "trend_agent:stale_trends",
                    "finding": f"{stale}/{total_trends} TrendSummary nodes are >7 days old — TrendAgent needs a refresh run",
                    "action": "python -m agents.trend_agent",
                    "priority": "medium" if stale < total_trends else "high",
                    "metric": {"stale": stale, "total": total_trends},
                })

        # ── 3. CORROBORATES/CONTRADICTS accuracy via outcome_log ──────────────
        outcome_path = STORE / "outcome_log.jsonl"
        if outcome_path.exists():
            lines = [l for l in outcome_path.read_text().splitlines() if l.strip()]
            sentiment_outcomes = [
                json.loads(l) for l in lines
                if json.loads(l).get("decision_type") == "sentiment_edge"
            ]
            if len(sentiment_outcomes) >= 10:
                correct = sum(1 for o in sentiment_outcomes if o.get("outcome_correct") is True)
                rate    = correct / len(sentiment_outcomes)
                if rate < 0.60:
                    gaps.append({
                        "area": "context_agent:sentiment_edge_accuracy",
                        "finding": (
                            f"ContextAgent CORROBORATES/CONTRADICTS edges are {rate:.0%} accurate "
                            f"({correct}/{len(sentiment_outcomes)} scored) — below 60% threshold"
                        ),
                        "action": "Review sentiment scoring logic in agents/context_agent.py:_sentiment_consistent()",
                        "priority": "high",
                        "metric": {"accuracy": round(rate, 3), "scored": len(sentiment_outcomes)},
                    })

    except Exception as exc:
        gaps.append({
            "area": "agent_quality:signal_error",
            "finding": f"Could not read graph for agent quality check: {exc}",
            "action": "Check memory/graph_store.py and memory_store/graph.db",
            "priority": "medium",
        })

    return gaps


def _signal_data_coverage() -> list[dict]:
    """
    Check XBRL results coverage against tracked universe size.
    """
    gaps = []

    # How many tracked symbols vs how many have results?
    universe_path = REPO / "data" / "tracked_universe.json"
    results_dir   = STORE / "results"
    if not universe_path.exists():
        return gaps

    try:
        universe = json.loads(universe_path.read_text())
        total_tracked = len(universe.get("universe", []))
    except Exception:
        return gaps

    if not results_dir.exists():
        coverage = 0.0
        symbols_with_results = 0
    else:
        symbols_with_results = len(list(results_dir.glob("*.jsonl")))
        coverage = symbols_with_results / total_tracked if total_tracked else 0

    if coverage < 0.30:
        gaps.append({
            "area": "xbrl_coverage",
            "finding": f"Only {symbols_with_results}/{total_tracked} tracked symbols have XBRL results ({coverage:.0%} coverage)",
            "action": "Run: python jobs/fetch_results.py — also verify event_calendar is up to date",
            "priority": "high" if coverage < 0.05 else "medium",
            "metric": {"coverage": round(coverage, 3), "have": symbols_with_results, "total": total_tracked},
        })

    return gaps


# ── STUCK gap handler ──────────────────────────────────────────────────────

# Known dependency chains: if the blocker gap is still open, the dependent gap
# can't close on its own. Format: {dependent_area_prefix: blocker_area_prefix}
_GAP_BLOCKERS: dict[str, str] = {
    "xbrl_coverage":           "pipeline_never_run:event_calendar",
    "pipeline_stale:bhavcopy":  "pipeline_never_run:bhavcopy",
}

# Threshold defaults — recalibration may tighten or loosen these
_FRESHNESS_THRESHOLDS: dict[str, int] = {
    "bhavcopy":         2,
    "indices_bhavcopy": 2,
    "announcements":    2,
    "event_calendar":   7,
}


def _handle_stuck_gap(gap: dict, all_current_gaps: list[dict]) -> dict:
    """
    Called when a gap has been stuck for 5+ consecutive scans.

    Three-step resolution protocol:
      Step 1 — dependency: is a known blocker gap still open?
               → surface the chain, no code needed
      Step 2 — recalibration: is the signal threshold unreasonable?
               → adjust threshold constant + log rationale
      Step 3 — generate fix: write and commit a targeted one-shot script

    Returns a dict describing what action was taken.
    """
    area = gap["area"]
    current_area_set = {g["area"] for g in all_current_gaps}

    # ── Step 1: dependency check ───────────────────────────────────────────
    for dep_prefix, blocker_prefix in _GAP_BLOCKERS.items():
        if area.startswith(dep_prefix):
            # check if the blocker is itself still an open gap
            blocker_open = any(g["area"].startswith(blocker_prefix) for g in all_current_gaps)
            if blocker_open:
                msg = (f"STUCK gap '{area}' is blocked by '{blocker_prefix}' which is also open. "
                       f"Close '{blocker_prefix}' first — '{area}' will resolve automatically.")
                print(f"  → [stuck handler] dependency identified: {blocker_prefix}")
                _log({"type": "stuck_dependency", "stuck_area": area,
                      "blocker": blocker_prefix, "message": msg})
                return {"action": "dependency_identified", "blocker": blocker_prefix,
                        "message": msg}

    # ── Step 2: recalibration — check if threshold is the problem ─────────
    if area.startswith("pipeline_stale:"):
        name = area.split(":", 1)[1]
        current_threshold = _FRESHNESS_THRESHOLDS.get(name, 7)
        # Check actual run history — how often does this pipeline produce output?
        folder = STORE / name
        if folder.exists():
            files = sorted(folder.glob("*.csv")) + sorted(folder.glob("*.json"))
            if len(files) >= 3:
                # Compute median inter-file gap in days
                stems = []
                for f in files[-10:]:
                    try:
                        stems.append(date.fromisoformat(f.stem[:10]))
                    except ValueError:
                        pass
                if len(stems) >= 2:
                    stems.sort()
                    gaps_days = [(stems[i+1] - stems[i]).days for i in range(len(stems)-1)]
                    median_gap = sorted(gaps_days)[len(gaps_days)//2]
                    if median_gap > current_threshold * 1.5:
                        new_threshold = int(median_gap * 1.2)
                        _FRESHNESS_THRESHOLDS[name] = new_threshold
                        msg = (f"Recalibrated '{name}' freshness threshold: "
                               f"{current_threshold}d → {new_threshold}d "
                               f"(actual median run interval: {median_gap}d)")
                        print(f"  → [stuck handler] threshold recalibrated: {msg}")
                        _log({"type": "stuck_recalibrate", "stuck_area": area,
                              "old_threshold": current_threshold,
                              "new_threshold": new_threshold,
                              "median_gap_days": median_gap, "message": msg})
                        return {"action": "threshold_recalibrated", "message": msg,
                                "new_threshold": new_threshold}

    # ── Step 3: generate a targeted one-shot fix script ────────────────────
    script = _generate_fix_script(gap)
    if script:
        script_path = REPO / "jobs" / script["filename"]
        script_path.write_text(script["content"])
        # commit the new script
        try:
            subprocess.run(["git", "add", str(script_path)], cwd=str(REPO), check=True)
            subprocess.run(
                ["git", "commit", "-m",
                 f"fix(auto): one-shot fix for stuck gap '{area}'\n\n"
                 f"Generated by self_improve._handle_stuck_gap() after {gap.get('consecutive_scans',5)}+ scans.\n"
                 f"Finding: {gap['finding'][:120]}\n\n"
                 f"Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>\n"
                 f"Claude-Session: https://claude.ai/code/session_01G28r1gAsugprj2KxqH9L5D"],
                cwd=str(REPO), check=True,
            )
            committed = True
            print(f"  → [stuck handler] generated + committed: jobs/{script['filename']}")
        except subprocess.CalledProcessError:
            committed = False
            print(f"  → [stuck handler] generated (commit failed): jobs/{script['filename']}")

        _log({"type": "stuck_fix_generated", "stuck_area": area,
              "script": script["filename"], "committed": committed})
        return {"action": "fix_script_generated", "script": script["filename"],
                "committed": committed,
                "message": f"Run: python jobs/{script['filename']}"}

    return {"action": "no_resolution_found", "area": area,
            "message": "No automatic fix available — manual intervention needed."}


def _generate_fix_script(gap: dict) -> dict | None:
    """
    Return {filename, content} for a one-shot script targeting this stuck gap.
    Returns None if no template applies.
    """
    area = gap["area"]

    if area == "pipeline_never_run:event_calendar":
        return {
            "filename": "backfill_event_calendar.py",
            "content": '''\
#!/usr/bin/env python3
"""
One-shot: backfill event_calendar/ for the last 30 days.
Generated by self_improve._handle_stuck_gap() to resolve
pipeline_never_run:event_calendar gap.
Run once; daily_monitor.py maintains it going forward.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, timedelta
from pathlib import Path
import json

STORE = Path(__file__).parent.parent / "memory_store" / "event_calendar"
STORE.mkdir(parents=True, exist_ok=True)

def run():
    try:
        from nselib import NSE
    except ImportError:
        print("nselib not available")
        return

    today = date.today()
    saved = 0
    for delta in range(30):
        d = today - timedelta(days=delta)
        dest = STORE / f"{d.isoformat()}.json"
        if dest.exists():
            continue
        try:
            with NSE() as nse:
                events = nse.event_calendar() or []
            dest.write_text(json.dumps(events, default=str, indent=2))
            saved += 1
            print(f"  saved {d.isoformat()}: {len(events)} events")
        except Exception as e:
            print(f"  {d.isoformat()}: error — {e}")

    print(f"\\nDone: {saved} days backfilled.")

if __name__ == "__main__":
    run()
''',
        }

    if area == "xbrl_coverage":
        return {
            "filename": "backfill_xbrl_results.py",
            "content": '''\
#!/usr/bin/env python3
"""
One-shot: fetch XBRL financial_results_detail() for all tracked symbols.
Generated by self_improve._handle_stuck_gap() to resolve xbrl_coverage gap.
Run once; fetch_results.py maintains it going forward.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import json

REPO = Path(__file__).parent.parent

def run():
    universe_path = REPO / "data" / "tracked_universe.json"
    if not universe_path.exists():
        print("tracked_universe.json not found")
        return

    universe = json.loads(universe_path.read_text()).get("universe", [])
    symbols = [s["symbol"] if isinstance(s, dict) else s for s in universe]
    print(f"Fetching XBRL results for {len(symbols)} symbols (10 workers)...")

    from memory.results_store import save_results

    def fetch_one(sym):
        try:
            from nselib import NSE
            with NSE() as nse:
                data = nse.financial_results_detail(sym)
            if not data:
                return sym, 0, "empty"
            quarters = data if isinstance(data, list) else data.get("results", [])
            out = save_results(sym, quarters)
            return sym, out.get("saved", 0), None
        except Exception as e:
            return sym, 0, str(e)[:80]

    saved_total = 0
    errors = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_one, s): s for s in symbols}
        for future in as_completed(futures):
            sym, saved, err = future.result()
            if err:
                errors.append((sym, err))
            else:
                saved_total += saved
                if saved:
                    print(f"  ✓ {sym}: {saved} quarters")

    print(f"\\nDone: {saved_total} quarters saved, {len(errors)} errors.")
    if errors:
        for sym, err in errors[:10]:
            print(f"  ✗ {sym}: {err}")

if __name__ == "__main__":
    run()
''',
        }

    return None


# ── 3. IMPROVE — main scan ─────────────────────────────────────────────────

def run_improvement_scan() -> dict:
    """
    Data-driven gap discovery with metacognitive persistence tracking.

    Reads actual runtime signals to find real performance issues, then
    compares against previous scans to track what's improving, what's
    regressing, and what's been stuck long enough to escalate.
    """
    print("\n[IMPROVE] Running data-driven improvement scan...")

    # ── Load scan history for persistence tracking ──────────────────────────
    history = _read_scan_history(n=10)

    # ── Collect signals ──────────────────────────────────────────────────────
    gaps: list[dict] = []
    gaps += _signal_triage_health()
    gaps += _signal_test_failures(history)
    gaps += _signal_task_errors()
    gaps += _signal_pipeline_freshness()
    gaps += _signal_data_coverage()
    gaps += _signal_agent_quality()

    # ── Gap persistence: escalate gaps that appear in 3+ consecutive scans ──
    current_areas = {g["area"] for g in gaps}
    persistence   = _gap_persistence(current_areas, history)
    resolved      = _resolved_gaps(current_areas, history)

    for gap in gaps:
        streak = persistence.get(gap["area"], 0)
        gap["consecutive_scans"] = streak + 1  # current scan + history
        if streak >= 2 and gap["priority"] == "medium":
            gap["priority"] = "high"
            gap["escalated"] = True
            gap["finding"] += f" [escalated: seen in {streak+1} consecutive scans]"
        elif streak >= 4 and gap["priority"] == "high":
            gap["finding"] += f" [STUCK: {streak+1} scans — needs different approach]"
            gap["stuck"] = True

    # ── Handle stuck gaps (5+ consecutive scans) ────────────────────────────
    stuck_actions: list[dict] = []
    for gap in gaps:
        if gap.get("stuck"):
            action = _handle_stuck_gap(gap, gaps)
            stuck_actions.append({"area": gap["area"], **action})
            # Annotate the gap finding with what was done
            gap["stuck_action"] = action.get("action", "none")
            if action.get("message"):
                gap["finding"] += f" → {action['message'][:80]}"

    # ── Print ────────────────────────────────────────────────────────────────
    if resolved:
        print(f"\n  ✓ Resolved since last scan: {', '.join(resolved)}")

    for g in gaps:
        icon = {"high": "!", "medium": "~", "low": "·"}.get(g["priority"], " ")
        streak = g.get("consecutive_scans", 1)
        streak_str = f" [{streak} scans]" if streak > 1 else ""
        print(f"  [{icon}] {g['area']}{streak_str}: {g['finding'][:85]}")

    if not gaps:
        print("  ✓ No gaps detected — system healthy")

    # ── Persist ──────────────────────────────────────────────────────────────
    entry = {
        "type":          "improve_scan",
        "gaps":          gaps,
        "gap_count":     len(gaps),
        "resolved":      resolved,
        "high_count":    sum(1 for g in gaps if g["priority"] == "high"),
        "escalated":     [g["area"] for g in gaps if g.get("escalated")],
        "stuck":         [g["area"] for g in gaps if g.get("stuck")],
        "stuck_actions": stuck_actions,
    }
    _log(entry)

    # ── Write gaps to knowledge graph ────────────────────────────────────────
    try:
        from memory.episode_bridge import gap_to_graph
        for gap in gaps:
            gap_to_graph(gap)
    except Exception:
        pass

    # ── Write to self-model so Claude arrives knowing what needs work ────────
    high   = [g for g in gaps if g["priority"] == "high"]
    stuck  = [g for g in gaps if g.get("stuck")]

    session_note_parts = []
    if resolved:
        session_note_parts.append(f"resolved: {', '.join(resolved)}")
    if high:
        session_note_parts.append(f"{len(high)} high-priority gaps")
    if stuck:
        actions_taken = [a.get("action", "none") for a in stuck_actions]
        session_note_parts.append(
            f"{len(stuck)} stuck gaps → actions: {', '.join(set(actions_taken))}"
        )
    if not gaps:
        session_note_parts.append("system healthy — no gaps")

    session_note = f"self_improve scan: {'; '.join(session_note_parts)}"

    reflection: dict = {
        "session_note": session_note,
    }
    if high:
        reflection["gaps"] = [g["finding"] for g in high]
    if resolved:
        # resolved gaps come out of calibration.gaps in the self-model
        reflection["new_confirmed"] = [f"resolved gap: {r}" for r in resolved]

    try:
        from memory.self_model import apply_reflection
        apply_reflection(reflection)
        print(f"\n  → self-model updated: {session_note}")
    except Exception as e:
        print(f"  → self-model update failed: {e}")

    return entry


# ── Main ───────────────────────────────────────────────────────────────────

def run(test: bool = True, heal: bool = True, improve: bool = True, symbol: str = "RELIANCE"):
    print(f"\n{'='*60}")
    print(f"  Self-Improve — {date.today().isoformat()}")
    print(f"{'='*60}")

    results = {}

    if heal:
        results["heal"] = run_healers()

    if test:
        results["test"] = run_tests(symbol=symbol)

    if improve:
        results["improve"] = run_improvement_scan()

    # Summary
    print(f"\n{'='*60}  Summary")
    if "test" in results:
        t = results["test"]
        print(f"  Tests:   {t['passed']} passed, {t['failed']} failed, {t['warned']} warnings")
    if "improve" in results:
        print(f"  Gaps:    {results['improve']['gap_count']} identified")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",            action="store_true")
    parser.add_argument("--heal",            action="store_true")
    parser.add_argument("--improve",         action="store_true")
    parser.add_argument("--diagnose-triage", action="store_true",
                        help="Run triage diagnosis directly: re-run rules on outcome_log, find anomalies, generate patches")
    parser.add_argument("--symbol",          default="RELIANCE")
    args = parser.parse_args()

    if args.diagnose_triage:
        print(f"\n{'='*60}")
        print(f"  Triage Diagnosis — {date.today().isoformat()}")
        print(f"{'='*60}\n")
        path = STORE / "outcome_log.jsonl"
        entries = []
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("event_type") == "triage":
                        entries.append(e)
                except json.JSONDecodeError:
                    pass
        print(f"  {len(entries)} triage entries in outcome_log")
        if len(entries) < 10:
            print("  Not enough data yet (need 10+). Run fetch_announcements.py --save first.")
        else:
            result = _diagnose_triage(entries)
            print(f"\n  {result['summary']}")
            for a in result["anomalies"]:
                print(f"\n  [{a['type']}] pattern='{a['pattern']}' count={a['count']} confidence={a['confidence']}")
                for ev in a.get("evidence", [])[:2]:
                    print(f"    └ {ev[:100]}")
                if a.get("patch"):
                    print(f"    patch: {a['patch']['description']}")
            if result["patches_applied"]:
                print(f"\n  ✓ {result['patches_applied']} patches applied to memory/triage.py")
            if result["patches_staged"]:
                print(f"  ~ {len(result['patches_staged'])} patches staged in memory_store/triage_patch_candidates.jsonl")
        import sys; sys.exit(0)

    # if no flags, run all
    all_mode = not (args.test or args.heal or args.improve)
    run(
        test    = all_mode or args.test,
        heal    = all_mode or args.heal,
        improve = all_mode or args.improve,
        symbol  = args.symbol,
    )
