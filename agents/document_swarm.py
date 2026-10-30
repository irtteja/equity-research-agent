#!/usr/bin/env python3
"""
Document Ingestion Swarm
========================
Feed it any directory (or list of files) and it dispatches parallel workers
to extract, classify, and store every document.

Supported formats: PDF, XLSX/XLS/CSV, DOCX/DOC, TXT, MD, HTML

Usage:
  python agents/document_swarm.py /path/to/documents/
  python agents/document_swarm.py file1.pdf file2.xlsx --symbol RELIANCE
  python agents/document_swarm.py /inbox/ --workers 16 --verbose
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.doc_store import save_document, summary as store_summary

# format router
_FORMAT_MAP = {
    "pdf":  "agents.workers.pdf_worker",
    "xlsx": "agents.workers.excel_worker",
    "xls":  "agents.workers.excel_worker",
    "xlsm": "agents.workers.excel_worker",
    "csv":  "agents.workers.excel_worker",
    "docx": "agents.workers.docx_worker",
    "doc":  "agents.workers.docx_worker",
    "txt":  "agents.workers.text_worker",
    "md":   "agents.workers.text_worker",
    "html": "agents.workers.text_worker",
    "htm":  "agents.workers.text_worker",
}

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _collect_files(paths: list[str]) -> list[Path]:
    files = []
    for p in paths:
        pp = Path(p)
        if pp.is_file():
            files.append(pp)
        elif pp.is_dir():
            for f in pp.rglob("*"):
                if f.is_file() and not any(d in _SKIP_DIRS for d in f.parts):
                    files.append(f)
    return files


def _load_known_symbols() -> set:
    """Load NSE symbol list from memory_store if available."""
    try:
        from nselib import NSE
        # use cached constituent list if available
        index_path = Path(__file__).parent.parent / "memory_store" / "symbols.json"
        if index_path.exists():
            with open(index_path) as f:
                return set(json.load(f))
        # fallback: fetch NIFTY 500
        with NSE() as nse:
            stocks = nse.fetch_index_constituents("NIFTY 500")
        syms = {s.get("Symbol", "").strip().upper() for s in stocks if s.get("Symbol")}
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(index_path, "w") as f:
            json.dump(sorted(syms), f)
        return syms
    except Exception:
        return set()


def _process_one(
    file_path: Path,
    known_symbols: set,
    override_symbol: str | None = None,
) -> dict:
    """Single worker: extract → classify → save. Returns result dict."""
    ext = file_path.suffix.lstrip(".").lower()
    module_name = _FORMAT_MAP.get(ext)

    if not module_name:
        return {
            "file": str(file_path),
            "status": "skipped",
            "reason": f"unsupported extension: .{ext}",
        }

    try:
        import importlib
        mod = importlib.import_module(module_name)
        extract_result = mod.extract(str(file_path))
        text = extract_result.get("text", "")
    except Exception as e:
        return {"file": str(file_path), "status": "error", "reason": f"extract failed: {e}"}

    if not text or len(text.strip()) < 20:
        return {"file": str(file_path), "status": "error", "reason": "no text extracted"}

    from agents.workers.classifier import classify
    classify_result = classify(str(file_path), text, known_symbols)

    if override_symbol:
        classify_result["symbol"] = override_symbol.upper()
        classify_result["confidence"] = "high"

    save_result = save_document(
        file_path=str(file_path),
        text=text,
        extract_meta=extract_result,
        classify_meta=classify_result,
    )

    return {
        "file":      str(file_path),
        "status":    save_result["status"],
        "doc_id":    save_result.get("doc_id"),
        "symbol":    classify_result.get("symbol"),
        "doc_type":  classify_result.get("doc_type"),
        "period":    classify_result.get("period"),
        "confidence": classify_result.get("confidence"),
        "chars":     len(text),
        "pages":     extract_result.get("pages"),
        "method":    extract_result.get("method"),
    }


def run(
    paths: list[str],
    workers: int = 8,
    override_symbol: str | None = None,
    verbose: bool = False,
) -> dict:
    files = _collect_files(paths)
    supported = [f for f in files if f.suffix.lstrip(".").lower() in _FORMAT_MAP]
    skipped_ext = [f for f in files if f.suffix.lstrip(".").lower() not in _FORMAT_MAP]

    print(f"\n{'='*60}")
    print(f"Document Swarm — {len(supported)} files to process ({len(skipped_ext)} skipped by extension)")
    print(f"Workers: {workers}  |  Symbol override: {override_symbol or 'auto-detect'}")
    print(f"{'='*60}\n")

    if not supported:
        print("Nothing to process.")
        return {"processed": 0, "saved": 0, "duplicates": 0, "errors": 0, "results": []}

    print("Loading known NSE symbols...")
    known_symbols = _load_known_symbols()
    print(f"  {len(known_symbols)} symbols loaded\n")

    t0 = time.time()
    results = []
    saved = dupes = errors = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_process_one, f, known_symbols, override_symbol): f
            for f in supported
        }
        for i, future in enumerate(as_completed(futures), 1):
            res = future.result()
            results.append(res)
            s = res["status"]
            if s == "saved":      saved += 1
            elif s == "duplicate": dupes += 1
            else:                  errors += 1

            if verbose or s == "error":
                sym   = res.get("symbol") or "?"
                dtype = res.get("doc_type") or "?"
                conf  = res.get("confidence") or "?"
                chars = res.get("chars") or 0
                print(f"  [{i:3d}/{len(supported)}] {s.upper():9s} {sym:15s} {dtype:15s} {conf:6s} {chars:7,d}ch  {Path(res['file']).name}")

    elapsed = time.time() - t0
    stats = store_summary()

    print(f"\n{'='*60}")
    print(f"Done in {elapsed:.1f}s")
    print(f"  Saved: {saved}  |  Duplicates: {dupes}  |  Errors: {errors}")
    print(f"  Store total: {stats['total']} docs across {stats['symbols']} symbols")
    print(f"  By type: {stats['by_type']}")
    print(f"{'='*60}\n")

    return {
        "processed":  len(supported),
        "saved":      saved,
        "duplicates": dupes,
        "errors":     errors,
        "elapsed_s":  round(elapsed, 1),
        "results":    results,
        "store":      stats,
    }


def main():
    parser = argparse.ArgumentParser(description="Document Ingestion Swarm")
    parser.add_argument("paths", nargs="+", help="Files or directories to ingest")
    parser.add_argument("--workers", type=int, default=8, help="Parallel workers (default 8)")
    parser.add_argument("--symbol", default=None, help="Override symbol for all files")
    parser.add_argument("--verbose", action="store_true", help="Print every file result")
    args = parser.parse_args()

    result = run(
        paths=args.paths,
        workers=args.workers,
        override_symbol=args.symbol,
        verbose=args.verbose,
    )

    # print errors in detail
    errs = [r for r in result["results"] if r["status"] == "error"]
    if errs:
        print(f"\n{len(errs)} errors:")
        for e in errs:
            print(f"  {e['file']}: {e.get('reason')}")


if __name__ == "__main__":
    main()
