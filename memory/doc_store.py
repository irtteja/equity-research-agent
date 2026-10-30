"""
General document storage layer.
Stores any ingested document (annual report, transcript, results PDF, etc.)
as structured JSON with extracted text, metadata, and 2000-char chunks.

Layout:
  memory_store/documents/{SYMBOL}/{doc_type}/{YYYY-MM-DD}_{slug}.json
  memory_store/documents/_unclassified/{YYYY-MM-DD}_{slug}.json
  memory_store/documents/_doc_index.jsonl  ← append-only master index
"""
import hashlib
import json
import re
import os
from datetime import date
from pathlib import Path

_STORE_ROOT = Path(__file__).parent.parent / "memory_store" / "documents"
_INDEX_FILE = _STORE_ROOT / "_doc_index.jsonl"
_CHUNK_SIZE = 2000  # chars per chunk


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "_", text.strip())
    return text[:40]


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _chunk(text: str) -> list[str]:
    return [text[i : i + _CHUNK_SIZE] for i in range(0, len(text), _CHUNK_SIZE)]


def save_document(
    file_path: str,
    text: str,
    extract_meta: dict,   # from worker: {pages, method, ...}
    classify_meta: dict,  # from classifier: {symbol, doc_type, period, confidence}
    source_name: str | None = None,
) -> dict:
    """
    Persist document. Idempotent by content hash.
    Returns {status, doc_id, store_path}.
    """
    _STORE_ROOT.mkdir(parents=True, exist_ok=True)

    content_hash = _content_hash(text)

    # check duplicate
    if _is_duplicate(content_hash):
        return {"status": "duplicate", "doc_id": content_hash}

    symbol   = (classify_meta.get("symbol") or "").upper()
    doc_type = classify_meta.get("doc_type") or "misc"
    period   = classify_meta.get("period") or ""

    folder_sym = symbol if symbol else "_unclassified"
    store_dir = _STORE_ROOT / folder_sym / doc_type
    store_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    base_name = source_name or Path(file_path).stem
    filename = f"{today}_{_slug(base_name)}_{content_hash}.json"
    store_path = store_dir / filename

    doc = {
        "doc_id":       content_hash,
        "source_file":  str(file_path),
        "source_name":  source_name or Path(file_path).name,
        "ingested_at":  today,
        "symbol":       symbol or None,
        "doc_type":     doc_type,
        "period":       period or None,
        "confidence":   classify_meta.get("confidence", "low"),
        "pages":        extract_meta.get("pages"),
        "extract_method": extract_meta.get("method"),
        "char_count":   len(text),
        "text":         text,
        "chunks":       _chunk(text),
    }

    with open(store_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    _append_index({
        "doc_id":      content_hash,
        "symbol":      symbol or None,
        "doc_type":    doc_type,
        "period":      period or None,
        "source_name": doc["source_name"],
        "ingested_at": today,
        "store_path":  str(store_path),
        "char_count":  len(text),
        "confidence":  classify_meta.get("confidence", "low"),
    })

    return {"status": "saved", "doc_id": content_hash, "store_path": str(store_path)}


def _is_duplicate(content_hash: str) -> bool:
    if not _INDEX_FILE.exists():
        return False
    with open(_INDEX_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("doc_id") == content_hash:
                    return True
            except Exception:
                pass
    return False


def _append_index(record: dict):
    _INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_INDEX_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def search_index(
    symbol: str | None = None,
    doc_type: str | None = None,
    period: str | None = None,
) -> list[dict]:
    """Return index records matching filters."""
    if not _INDEX_FILE.exists():
        return []
    results = []
    with open(_INDEX_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if symbol and (rec.get("symbol") or "").upper() != symbol.upper():
                continue
            if doc_type and rec.get("doc_type") != doc_type:
                continue
            if period and period.lower() not in (rec.get("period") or "").lower():
                continue
            results.append(rec)
    return results


def load_document(doc_id: str) -> dict | None:
    """Load full document JSON by doc_id."""
    if not _INDEX_FILE.exists():
        return None
    store_path = None
    with open(_INDEX_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("doc_id") == doc_id:
                    store_path = rec.get("store_path")
                    break
            except Exception:
                pass
    if not store_path or not Path(store_path).exists():
        return None
    with open(store_path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_symbols() -> list[str]:
    """All symbols that have at least one document."""
    syms = set()
    if not _INDEX_FILE.exists():
        return []
    with open(_INDEX_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("symbol"):
                    syms.add(rec["symbol"])
            except Exception:
                pass
    return sorted(syms)


def summary() -> dict:
    """Quick stats on the document store."""
    if not _INDEX_FILE.exists():
        return {"total": 0, "symbols": 0, "by_type": {}}
    total = 0
    syms = set()
    by_type: dict[str, int] = {}
    with open(_INDEX_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                total += 1
                if rec.get("symbol"):
                    syms.add(rec["symbol"])
                t = rec.get("doc_type", "misc")
                by_type[t] = by_type.get(t, 0) + 1
            except Exception:
                pass
    return {"total": total, "symbols": len(syms), "by_type": by_type}
