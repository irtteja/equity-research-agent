#!/usr/bin/env python3
"""
Self-improvement module — testing, healing, and iterative improvement.

Three responsibilities:
  1. TEST    — run the e2e test suite and surface failures
  2. HEAL    — auto-fix known recoverable failures (stale cookies, missing dirs,
               broken index files, numpy/import issues)
  3. IMPROVE — scan for gaps and log them as improvement candidates

Run modes:
  python jobs/self_improve.py           # full cycle: test → heal → improve
  python jobs/self_improve.py --test    # test only
  python jobs/self_improve.py --heal    # heal only
  python jobs/self_improve.py --improve # improve scan only

Results are logged to memory_store/self_improve_log.jsonl
and surfaced to the self-model as session notes.

Claude reads this module's output and acts on IMPROVE suggestions
in subsequent sessions — adding tools, fixing pipelines, tightening triage.
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


# ── 3. IMPROVE ─────────────────────────────────────────────────────────────

def run_improvement_scan() -> dict:
    """
    Scan the system for gaps and log improvement candidates.
    Claude reads these and acts on them in subsequent sessions.
    """
    print("\n[IMPROVE] Scanning for improvement opportunities...")
    gaps = []

    # Check: do we have XBRL results for any tracked companies?
    results_dir = STORE / "results"
    result_symbols = list(results_dir.glob("*.jsonl")) if results_dir.exists() else []
    if not result_symbols:
        gaps.append({
            "area": "results_pipeline",
            "finding": "No XBRL results stored yet — fetch_results.py may not have run",
            "action": "Run: python jobs/fetch_results.py",
            "priority": "high",
        })

    # Check: self-model populated?
    sm_file = STORE / "self_model.json"
    if sm_file.exists():
        import json as _json
        sm = _json.loads(sm_file.read_text())
        if not sm.get("last_updated"):
            gaps.append({
                "area": "self_model",
                "finding": "Self-model exists but has never been updated via apply_reflection()",
                "action": "Run daily_monitor.py end-to-end and apply the reflection output",
                "priority": "high",
            })
        confirmed = len(sm.get("patterns", {}).get("confirmed", []))
        if confirmed < 3:
            gaps.append({
                "area": "self_model",
                "finding": f"Only {confirmed} confirmed patterns — model is still thin",
                "action": "Continue daily runs; patterns accumulate automatically",
                "priority": "low",
            })

    # Check: outcome logging exists?
    outcome_log = STORE / "outcome_log.jsonl"
    if not outcome_log.exists():
        gaps.append({
            "area": "outcome_logging",
            "finding": "No outcome_log.jsonl — triage decisions are not being tracked for ground-truth reconciliation",
            "action": "Build memory/outcome_log.py to log every triage decision with timestamp",
            "priority": "medium",
        })

    # Check: any documents ingested?
    doc_index = STORE / "documents" / "_doc_index.jsonl"
    doc_count = 0
    if doc_index.exists():
        doc_count = sum(1 for l in doc_index.read_text().splitlines() if l.strip())
    if doc_count < 5:
        gaps.append({
            "area": "document_swarm",
            "finding": f"Only {doc_count} documents ingested — annual reports and transcripts not yet loaded",
            "action": "Feed annual reports via: python agents/document_swarm.py /path/to/docs/",
            "priority": "medium",
        })

    # Check: scheduled tasks enabled?
    gaps.append({
        "area": "scheduled_tasks",
        "finding": "14 scheduled tasks still disabled — system not running live",
        "action": "Enable scheduled tasks when ready to go live",
        "priority": "high",
    })

    # Check: BSE integration
    gaps.append({
        "area": "bse_integration",
        "finding": "Announcement pipeline is NSE-only; BSE filings not tracked",
        "action": "Build jobs/fetch_bse_announcements.py mirroring fetch_announcements.py",
        "priority": "medium",
    })

    # Check: recall_tool in MCP
    # (we just built it — this gap should resolve itself after next restart)

    # Print gaps
    for g in gaps:
        pri_color = {"high": "!", "medium": "~", "low": "·"}.get(g["priority"], " ")
        print(f"  [{pri_color}] {g['area']}: {g['finding'][:80]}")

    entry = {"type": "improve_scan", "gaps": gaps, "gap_count": len(gaps)}
    _log(entry)

    # Push high-priority gaps into self-model as session notes
    high = [g for g in gaps if g["priority"] == "high"]
    if high:
        try:
            from memory.self_model import apply_reflection
            apply_reflection({
                "gaps": [g["finding"] for g in high],
                "session_note": f"self_improve scan: {len(gaps)} gaps found, {len(high)} high priority",
            })
            print(f"\n  → {len(high)} high-priority gaps written to self-model")
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
