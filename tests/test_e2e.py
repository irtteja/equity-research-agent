#!/usr/bin/env python3
"""
End-to-end pipeline test.
Uses real data already in memory_store — no live API calls required.

Tests each layer in sequence:
  1. Recall      — query prices + results + insights for a symbol
  2. Ingestion   — build extraction prompt from a real announcement
  3. Self-model  — apply a mock reflection, verify context injection
  4. Doc swarm   — ingest a test document, verify storage + retrieval

Run: python tests/test_e2e.py
     python tests/test_e2e.py --symbol TCS --verbose
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

REPO = Path(__file__).parent.parent
STORE = REPO / "memory_store"

PASS = "✓"
FAIL = "✗"
WARN = "⚠"


def _hdr(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _ok(msg):   print(f"  {PASS} {msg}")
def _fail(msg): print(f"  {FAIL} {msg}")
def _warn(msg): print(f"  {WARN} {msg}")


# ── Layer 1: Recall ────────────────────────────────────────────────────────

def test_recall(symbol: str, verbose: bool = False):
    _hdr(f"Layer 1: Recall — {symbol}")
    from memory.recall import get_price_series, get_results, get_announcements, get_insights, company_snapshot

    # prices
    p = get_price_series(symbol, days=30)
    if p["data"]:
        _ok(f"Prices: {len(p['data'])} trading days, latest close ₹{p['data'][-1]['close']:,.2f}")
        if verbose:
            print(p["text"])
    else:
        _warn(f"No price data for {symbol}")

    # results
    r = get_results(symbol, n=4)
    if r["data"]:
        _ok(f"Results: {len(r['data'])} quarters stored")
        if verbose:
            print(r["text"])
    else:
        _warn(f"No XBRL results for {symbol} (expected if fetch_results hasn't run yet)")

    # announcements
    a = get_announcements(symbol, days=30)
    if a["data"]:
        _ok(f"Announcements: {len(a['data'])} found in last 30d")
        if verbose:
            print(a["text"][:600])
    else:
        _warn(f"No processed announcements for {symbol}")

    # insights
    i = get_insights(symbol)
    if i["data"]:
        _ok(f"Insights: {len(i['data'])} stored")
    else:
        _warn(f"No insights yet for {symbol} (expected on first run)")

    # full snapshot
    snap = company_snapshot(symbol)
    sections = snap["text"].count("─")
    _ok(f"company_snapshot assembled ({len(snap['text'])} chars, {sections} sections)")
    return snap


# ── Layer 2: Ingestion ─────────────────────────────────────────────────────

def test_ingestion(symbol: str, verbose: bool = False):
    _hdr(f"Layer 2: Ingestion — {symbol}")
    from memory.ingestion import prepare_announcement, _build_company_context, _get_self_model_context

    # load a real announcement from memory_store
    ann_files = sorted((STORE / "announcements").glob("*.json"), reverse=True)
    if not ann_files:
        _fail("No announcement files in memory_store/announcements/")
        return None

    ann_file = ann_files[0]
    with open(ann_file) as f:
        daily = json.load(f)

    anns = daily.get("announcements", {}).get(symbol, [])
    if not anns:
        _warn(f"No announcements for {symbol} in {ann_file.name} — using RELIANCE fallback")
        symbol = list(daily.get("announcements", {}).keys())[0]
        anns   = daily["announcements"][symbol]

    ann = anns[0]
    _ok(f"Using real announcement: {ann.get('attchmntText','')[:80]}...")

    # test company context build
    ctx = _build_company_context(symbol)
    if "No prior data" in ctx or "recall unavailable" in ctx:
        _warn(f"Company context: {ctx[:80]}")
    else:
        _ok(f"Company context built ({len(ctx)} chars)")

    # test self-model context
    sm = _get_self_model_context()
    if sm:
        _ok(f"Self-model context injected ({len(sm)} chars)")
    else:
        _warn("Self-model context empty (will populate after first reflection)")

    # prepare the announcement (this downloads the PDF — may fail from cloud on some URLs)
    _ok(f"Running prepare_announcement for {symbol}...")
    try:
        profile = {}
        try:
            from memory.store import get_profile
            profile = get_profile(symbol) or {}
        except Exception:
            pass

        item = prepare_announcement(symbol, ann, profile)
        status = item["status"]

        if status == "ready":
            _ok(f"Status: ready — {item['pdf_pages']} pages, {len(item['text'])} chars extracted")
            if verbose:
                print("\n--- Extraction prompt (first 1200 chars) ---")
                print(item["prompt"][:1200])
                print("---")
        elif status == "skip":
            _warn(f"Status: skip — {item.get('reason','')}")
        elif status == "ignore":
            _warn(f"Status: ignore — {item.get('reason','')}")
        elif status == "light":
            _warn(f"Status: light — {item.get('reason','')}")
        else:
            _fail(f"Status: {status} — {item.get('reason','')}")

        return item
    except Exception as e:
        _fail(f"prepare_announcement raised: {e}")
        return None


# ── Layer 3: Self-model ────────────────────────────────────────────────────

def test_self_model():
    _hdr("Layer 3: Self-model")
    from memory.self_model import apply_reflection, as_context, load

    # apply a test reflection
    reflection = {
        "new_uncertain": [f"TEST PATTERN — auto-generated by e2e test"],
        "works":         ["E2E test signal"],
        "watch_add":     [{"ticker": "TEST", "theme": "e2e test", "note": "remove after test"}],
        "session_note":  "E2E test run",
    }
    model = apply_reflection(reflection)
    _ok(f"apply_reflection succeeded — model v{model['version']}")

    ctx = as_context()
    if "TEST PATTERN" in ctx:
        _ok("Pattern visible in as_context() output")
    else:
        _fail("Pattern not found in as_context() output")

    if verbose_global:
        print(ctx)

    # clean up test artifacts
    model2 = load()
    model2["patterns"]["uncertain"] = [
        p for p in model2["patterns"]["uncertain"] if "TEST PATTERN" not in p["pattern"]
    ]
    model2["watch_list"] = [w for w in model2["watch_list"] if w.get("ticker") != "TEST"]
    model2["calibration"]["works"] = [
        w for w in model2["calibration"]["works"] if w["signal"] != "E2E test signal"
    ]
    from memory.self_model import save
    save(model2)
    _ok("Test artifacts cleaned up")
    return model2


# ── Layer 4: Document swarm ────────────────────────────────────────────────

def test_doc_swarm(symbol: str):
    _hdr(f"Layer 4: Document Swarm — {symbol}")
    import tempfile, os
    from agents.document_swarm import run as swarm_run
    from memory.doc_store import search_index, load_document, summary as store_summary

    # write a temp test file
    test_content = f"""
{symbol} Annual Report FY2026
NSE: {symbol}

Financial Highlights Q4 FY2026:
Revenue from Operations: ₹95,000 crore (up 12% YoY)
EBITDA: ₹18,500 crore (margin 19.5%)
PAT: ₹11,200 crore (up 8% YoY)

Management Commentary:
CEO stated: "We expect sustained growth driven by strong order book of ₹2.1 lakh crore."
CFO guided for 14-16% revenue growth in FY2027.
"""

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt",
        prefix=f"{symbol}_AR2026_",
        delete=False, dir="/tmp"
    ) as f:
        f.write(test_content)
        tmp_path = f.name

    try:
        result = swarm_run(
            paths=[tmp_path],
            workers=1,
            override_symbol=symbol,
            verbose=True,
        )
        if result["saved"] == 1:
            _ok(f"Document saved to store")
        elif result["duplicates"] == 1:
            _ok(f"Document already in store (duplicate — idempotent)")
        else:
            _fail(f"Unexpected result: {result}")

        # verify retrieval
        recs = search_index(symbol=symbol, doc_type="annual_report")
        if recs:
            _ok(f"search_index found {len(recs)} annual_report doc(s) for {symbol}")
            doc = load_document(recs[-1]["doc_id"])
            if doc and "Revenue" in doc.get("text", ""):
                _ok(f"load_document returned full text ({len(doc['text'])} chars)")
            else:
                _warn("Document text missing expected content")
        else:
            _warn(f"No annual_report docs found for {symbol} in index")

        stats = store_summary()
        _ok(f"Store summary: {stats['total']} docs, {stats['symbols']} symbols, types: {stats['by_type']}")

    finally:
        os.unlink(tmp_path)


# ── Main ───────────────────────────────────────────────────────────────────

verbose_global = False

def main():
    global verbose_global
    parser = argparse.ArgumentParser(description="E2E pipeline test")
    parser.add_argument("--symbol",  default="RELIANCE", help="Symbol to test with")
    parser.add_argument("--verbose", action="store_true", help="Print full output at each layer")
    parser.add_argument("--layer",   type=int, default=0, help="Run only this layer (1-4); 0=all")
    args = parser.parse_args()

    verbose_global = args.verbose
    sym = args.symbol.upper()

    print(f"\nEquity Research Agent — E2E Test")
    print(f"Symbol: {sym}  |  Verbose: {args.verbose}")

    results = {}

    if args.layer in (0, 1):
        results["recall"]    = test_recall(sym, verbose=args.verbose)
    if args.layer in (0, 2):
        results["ingestion"] = test_ingestion(sym, verbose=args.verbose)
    if args.layer in (0, 3):
        results["self_model"] = test_self_model()
    if args.layer in (0, 4):
        test_doc_swarm(sym)

    print(f"\n{'='*60}")
    print("  Summary")
    print(f"{'='*60}")
    for layer, result in results.items():
        status = "ok" if result else "warn"
        print(f"  {PASS if result else WARN} {layer}")
    print()


if __name__ == "__main__":
    main()
