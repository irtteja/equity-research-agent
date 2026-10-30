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
    Flags: categories that are always skipped (>90%) or always deep (>90%),
    and accuracy below threshold for scored decisions.
    """
    gaps = []
    path = STORE / "outcome_log.jsonl"
    if not path.exists():
        return gaps

    from collections import defaultdict
    by_category: dict = defaultdict(lambda: defaultdict(int))
    scored, score_sum, total = 0, 0.0, 0

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            if e.get("event_type") != "triage":
                continue
            total += 1
            # source field often contains "category" info via input text
            decision = e.get("decision", "unknown")
            # use first word of rationale as a rough category label
            rationale = (e.get("rationale") or "unknown").split(":")[0].strip()[:40]
            by_category[rationale][decision] += 1
            if e.get("score") is not None:
                scored += 1
                score_sum += float(e["score"])
        except (json.JSONDecodeError, ValueError):
            pass

    if total < 20:
        # not enough data to draw conclusions yet
        return gaps

    # Check accuracy on scored decisions
    if scored >= 10:
        mean_acc = score_sum / scored
        if mean_acc < 0.70:
            gaps.append({
                "area": "triage_accuracy",
                "finding": f"Triage mean accuracy {mean_acc:.0%} across {scored} scored decisions — below 70% threshold",
                "action": "Review triage.py rules for categories with lowest accuracy; check outcome_log for patterns",
                "priority": "high",
                "metric": {"accuracy": round(mean_acc, 3), "scored": scored},
            })

    # Check per-category skew
    for cat, decisions in by_category.items():
        cat_total = sum(decisions.values())
        if cat_total < 10:
            continue
        skip_rate = decisions.get("skip", 0) / cat_total
        deep_rate = decisions.get("deep", 0) / cat_total
        if skip_rate > 0.90:
            gaps.append({
                "area": f"triage_skew:{cat[:30]}",
                "finding": f"Category '{cat}' is skipped {skip_rate:.0%} of the time ({cat_total} decisions) — possible over-filtering",
                "action": f"Review triage.py rule for '{cat}'; check if valuable signals are being missed",
                "priority": "medium",
                "metric": {"skip_rate": round(skip_rate, 3), "n": cat_total},
            })
        elif deep_rate > 0.90:
            gaps.append({
                "area": f"triage_deep_flood:{cat[:30]}",
                "finding": f"Category '{cat}' triggers deep {deep_rate:.0%} of the time — may be over-processing noise",
                "action": f"Review triage.py rule for '{cat}'; tighten conditions if category includes noise",
                "priority": "low",
                "metric": {"deep_rate": round(deep_rate, 3), "n": cat_total},
            })

    return gaps


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

    # ── Print ────────────────────────────────────────────────────────────────
    if resolved:
        print(f"\n  ✓ Resolved since last scan: {', '.join(resolved)}")

    for g in gaps:
        icon = {"high": "!", "medium": "~", "low": "·"}.get(g["priority"], " ")
        streak = g.get("consecutive_scans", 1)
        streak_str = f" [{streak} scans]" if streak > 1 else ""
        print(f"  [{icon}] {g['area']}{streak_str}: {g['finding'][:75]}")

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
    }
    _log(entry)

    # ── Write to self-model so Claude arrives knowing what needs work ────────
    high   = [g for g in gaps if g["priority"] == "high"]
    stuck  = [g for g in gaps if g.get("stuck")]

    session_note_parts = []
    if resolved:
        session_note_parts.append(f"resolved: {', '.join(resolved)}")
    if high:
        session_note_parts.append(f"{len(high)} high-priority gaps")
    if stuck:
        session_note_parts.append(f"{len(stuck)} stuck gaps need new approach")
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
    parser.add_argument("--test",    action="store_true")
    parser.add_argument("--heal",    action="store_true")
    parser.add_argument("--improve", action="store_true")
    parser.add_argument("--symbol",  default="RELIANCE")
    args = parser.parse_args()

    # if no flags, run all
    all_mode = not (args.test or args.heal or args.improve)
    run(
        test    = all_mode or args.test,
        heal    = all_mode or args.heal,
        improve = all_mode or args.improve,
        symbol  = args.symbol,
    )
