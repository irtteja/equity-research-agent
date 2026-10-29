"""
PDF download and text extraction for NSE corporate filings.
Uses nselib for downloading (handles NSE session/cookies), pymupdf for extraction.
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/irtteja/nselib"))

import tempfile
from pathlib import Path


def _extract_text_pymupdf(pdf_path: str | Path) -> str:
    import pymupdf
    doc = pymupdf.open(str(pdf_path))
    pages = []
    for page in doc:
        pages.append(page.get_text())
    doc.close()
    return "\n".join(pages)


def download_and_extract(url: str, symbol: str = None) -> dict:
    """
    Download a PDF from NSE archives and extract its text.

    Returns:
        {
            "text": <extracted text>,
            "pages": <page count>,
            "size_bytes": <file size>,
            "url": <original url>,
            "error": <only if failed>
        }
    """
    from nselib import NSE
    import re

    filename = url.rstrip("/").split("/")[-1] or "document.pdf"
    # Sanitise filename
    filename = re.sub(r'[^\w.\-]', '_', filename)
    if not filename.endswith(".pdf"):
        filename += ".pdf"

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            with NSE() as nse:
                pdf_path = nse.download_document(url, folder=tmpdir, filename=filename)
        except Exception as e:
            return {"error": f"download failed: {e}", "url": url}

        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            return {"error": "downloaded file missing", "url": url}

        size_bytes = pdf_path.stat().st_size
        if size_bytes == 0:
            return {"error": "empty file downloaded", "url": url}

        try:
            text = _extract_text_pymupdf(pdf_path)
        except Exception as e:
            return {"error": f"text extraction failed: {e}", "url": url, "size_bytes": size_bytes}

        import pymupdf
        doc = pymupdf.open(str(pdf_path))
        page_count = len(doc)
        doc.close()

    return {
        "text": text.strip(),
        "pages": page_count,
        "size_bytes": size_bytes,
        "url": url,
    }


def fetch_nse_announcements(symbol: str, days_back: int = 1) -> list[dict]:
    """
    Fetch recent NSE announcements for a symbol.
    Returns list of announcement dicts with 'pdf_url' field populated.
    """
    sys.path.insert(0, os.path.expanduser("~/irtteja/nselib"))
    from nselib import NSE

    with NSE() as nse:
        ann = nse.announcements(symbol=symbol)

    if not ann:
        return []

    from datetime import date, timedelta, datetime
    cutoff = date.today() - timedelta(days=days_back)

    results = []
    for a in ann:
        dt_str = a.get("an_dt", "") or a.get("date", "")
        # an_dt format: "29-Oct-2026 08:55:40" or "2026-10-29"
        try:
            if len(dt_str) >= 11 and dt_str[2] == "-":
                dt = datetime.strptime(dt_str[:11].strip(), "%d-%b-%Y").date()
            else:
                dt = datetime.strptime(dt_str[:10], "%Y-%m-%d").date()
            if dt < cutoff:
                continue
        except Exception:
            pass  # include if unparseable

        attach_url = a.get("attchmntFile", "")
        results.append({
            "symbol": symbol,
            "date": dt_str,
            "description": a.get("desc", "") or a.get("description", ""),
            "headline": a.get("attchmntText", "")[:200],
            "pdf_url": attach_url,
            "source": "NSE",
            "raw": a,
        })

    return results
