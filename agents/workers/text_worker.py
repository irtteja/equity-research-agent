"""Plain text / HTML / Markdown extraction worker."""
import re


def extract(file_path: str) -> dict:
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    ext = file_path.rsplit(".", 1)[-1].lower()
    if ext in ("html", "htm"):
        text = _strip_html(raw)
        method = "html-strip"
    else:
        text = raw
        method = "plaintext"
    return {"text": text, "pages": 1, "method": method}


def _strip_html(html: str) -> str:
    # remove scripts/styles first
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    # replace block tags with newlines
    html = re.sub(r"<(br|p|div|h[1-6]|li|tr)[^>]*>", "\n", html, flags=re.I)
    # strip remaining tags
    html = re.sub(r"<[^>]+>", "", html)
    # collapse whitespace
    lines = [l.strip() for l in html.splitlines() if l.strip()]
    return "\n".join(lines)
