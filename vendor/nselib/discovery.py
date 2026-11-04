"""
NSE endpoint discovery — two complementary strategies:

1. Browser-based (Playwright):  visit pages, intercept live network requests.
   Finds endpoints that fire on page load. Requires local/residential IP.
   Entry: run(update, verbose)

2. Static JS analysis (cloud-safe):  fetch page HTML → find JS files →
   read the JS → trace API URL construction in download/export handlers.
   Finds endpoints hidden behind user interactions (button clicks, date pickers).
   Works from any IP since it reads JS source, not live API calls.
   Entry: scan_page_js(page_url, session) → list[dict]

Usage:
    python -m nselib.discovery               # browser-based: print new endpoints
    python -m nselib.discovery --update      # browser-based: write to endpoints.json
    python -m nselib.discovery --verbose     # browser-based: show all captured
    python -m nselib.discovery --scan <url>  # static JS scan of one page
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Stealth JS — injected before every page load
# Patches away the most common Playwright/Chromium fingerprints
# that Akamai / NSE bot detection checks for.
# ---------------------------------------------------------------------------
_STEALTH_JS = """
// 1. Remove navigator.webdriver
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// 2. Add realistic plugin list
Object.defineProperty(navigator, 'plugins', {
  get: () => {
    const arr = [
      {name: 'Chrome PDF Plugin',     filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
      {name: 'Chrome PDF Viewer',     filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
      {name: 'Native Client',         filename: 'internal-nacl-plugin', description: ''},
    ];
    arr.__proto__ = PluginArray.prototype;
    return arr;
  }
});

// 3. Realistic language list (India-first)
Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en-GB', 'en']});

// 4. Ensure Chrome runtime object exists (headless omits it)
if (!window.chrome) window.chrome = {runtime: {}};

// 5. Patch permissions API — headless returns 'denied' for notifications
const origQuery = window.Permissions && window.Permissions.prototype.query;
if (origQuery) {
  window.Permissions.prototype.query = function(params) {
    if (params.name === 'notifications') {
      return Promise.resolve({state: Notification.permission});
    }
    return origQuery.call(this, params);
  };
}

// 6. Realistic screen dimensions
Object.defineProperty(screen, 'width',       {get: () => 1920});
Object.defineProperty(screen, 'height',      {get: () => 1080});
Object.defineProperty(screen, 'availWidth',  {get: () => 1920});
Object.defineProperty(screen, 'availHeight', {get: () => 1040});
Object.defineProperty(window, 'outerWidth',  {get: () => 1920});
Object.defineProperty(window, 'outerHeight', {get: () => 1080});

// 7. Mask automation flags on chrome.app
if (window.chrome && !window.chrome.app) {
  window.chrome.app = {
    isInstalled: false,
    getDetails:  function() { return null; },
    getIsInstalled: function() { return false; },
    runningState:   function() { return 'cannot_run'; },
  };
}
"""

# NSE pages to visit — each triggers a different set of API calls
_NSE_PAGES = [
    ("homepage",          "https://www.nseindia.com/"),
    ("equity market",     "https://www.nseindia.com/market-data/live-equity-market"),
    ("corporate actions", "https://www.nseindia.com/companies-listing/corporate-filings-actions"),
    ("board meetings",    "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings"),
    ("fin results",       "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"),
    ("announcements",     "https://www.nseindia.com/companies-listing/corporate-filings-announcements"),
    ("option chain",      "https://www.nseindia.com/option-chain"),
    ("derivatives",       "https://www.nseindia.com/market-data/equity-derivatives"),
    ("52 week",           "https://www.nseindia.com/market-data/52-week-high-low-equity"),
    ("indices",           "https://www.nseindia.com/market-data/live-market-indices"),
    ("holidays",          "https://www.nseindia.com/national-stock-exchange/holiday-calendar"),
]

_NSE_API_RE = re.compile(r"https://www\.nseindia\.com/api/(.+?)(?:\?|$)")
_CATALOGUE   = Path(__file__).parent / "endpoints.json"


def _load_catalogue() -> dict:
    if _CATALOGUE.exists():
        return json.loads(_CATALOGUE.read_text())
    return {"version": "1.0", "endpoints": {}}


def _save_catalogue(cat: dict) -> None:
    _CATALOGUE.write_text(json.dumps(cat, indent=2))


def _random_delay(lo: float = 1.5, hi: float = 4.0) -> None:
    time.sleep(random.uniform(lo, hi))


async def _discover(update: bool = False, verbose: bool = False) -> list[dict]:
    from playwright.async_api import async_playwright

    catalogue  = _load_catalogue()
    known      = set(catalogue.get("endpoints", {}).keys())
    captured:  dict[str, dict] = {}   # path → {params, sample_url}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path="/opt/pw-browsers/chromium",
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",   # key flag
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-web-security",
                "--window-size=1920,1080",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={
                "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
            },
        )

        # inject stealth before every navigation
        await context.add_init_script(_STEALTH_JS)

        page = await context.new_page()

        def _on_response(response):
            m = _NSE_API_RE.match(response.url)
            if not m:
                return
            path   = m.group(1).rstrip("/")
            parsed = urlparse(response.url)
            params = list(parse_qs(parsed.query).keys())
            if path not in captured:
                captured[path] = {"params": params, "sample_url": response.url, "status": response.status}

        page.on("response", _on_response)

        # --- visit each NSE page naturally ---
        for label, url in _NSE_PAGES:
            try:
                print(f"  → visiting {label} …", flush=True)
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                # small random wait to let lazy-loaded requests fire
                await asyncio.sleep(random.uniform(1.5, 3.5))
                # gentle random scroll — triggers more API calls on some pages
                await page.evaluate("window.scrollBy(0, Math.floor(Math.random()*600+200))")
                await asyncio.sleep(random.uniform(0.8, 1.8))
            except Exception as exc:
                print(f"     warning: {label} failed — {exc}", flush=True)

        await browser.close()

    # --- analyse results ---
    new_endpoints = []
    for path, meta in sorted(captured.items()):
        if verbose:
            status = "NEW  " if path not in known else "known"
            print(f"  [{status}] {path}")
        if path not in known:
            new_endpoints.append({"path": path, **meta})

    if update and new_endpoints:
        for ep in new_endpoints:
            path = ep["path"]
            catalogue["endpoints"][path] = {
                "purpose":       "DISCOVERED — needs description",
                "params":        ep["params"],
                "returns":       [],
                "nselib_method": None,
                "module":        None,
                "notes":         f"Auto-discovered. Sample URL: {ep['sample_url']}",
            }
        _save_catalogue(catalogue)
        print(f"\n  ✓ {len(new_endpoints)} new endpoint(s) added to endpoints.json")

    return new_endpoints


def scan_page_js(page_url: str, session=None, verbose: bool = False) -> list[dict]:
    """
    Static JS analysis — cloud-safe endpoint discovery.

    Fetches the page HTML, finds every JS file it loads, reads each one,
    and searches for API URL construction patterns (window.open, fetch,
    $.ajax, params.append, csv=true, etc.).

    Returns a list of dicts:
        {
          "js_file":   "/dist/js/sections/corporate-filings.js",
          "endpoint":  "corporate-announcements",
          "params":    ["index", "from_date", "to_date", "csv"],
          "context":   "...raw JS snippet around the match...",
          "note":      "CSV download — use from_date/to_date not from/to",
        }

    Works from cloud (no browser, no Akamai restriction) because it reads
    JS source files, not live API responses.

    Example:
        from nselib import NSE
        from nselib.discovery import scan_page_js

        with NSE() as nse:
            hits = scan_page_js(
                "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
                session=nse._session._s,
                verbose=True,
            )
    """
    import requests as _req

    s = session or _req.Session()

    # ── 1. Fetch page HTML ────────────────────────────────────────────────
    r = s.get(page_url, timeout=15)
    r.raise_for_status()
    html = r.text

    # ── 2. Find all JS file references ───────────────────────────────────
    js_srcs = re.findall(r'src=["\']([^"\']+\.js[^"\']*)["\']', html)
    base = "https://www.nseindia.com"
    js_urls = [(src, base + src if src.startswith("/") else src) for src in js_srcs]

    if verbose:
        print(f"Page: {page_url}")
        print(f"Found {len(js_urls)} JS files")

    # ── 3. Patterns that indicate an API endpoint being built ─────────────
    # Each pattern: (label, regex capturing up to 600 chars of context)
    _API_PATTERNS = [
        ("window.open",    re.compile(r".{0,300}window\.open\(['\"]?/api/.{0,300}", re.S)),
        ("csv=true",       re.compile(r".{0,300}csv.{0,5}true.{0,300}",             re.S)),
        ("from_date",      re.compile(r".{0,300}from_date.{0,300}",                 re.S)),
        ("params.append",  re.compile(r".{0,300}params\.append.{0,500}",            re.S)),
        ("fetch /api/",    re.compile(r".{0,200}fetch\(['\"]?/api/.{0,200}",        re.S)),
        ("$.ajax /api/",   re.compile(r".{0,200}\$\.(?:ajax|get|post)\(['\"]?/api/.{0,200}", re.S)),
        ("location.href",  re.compile(r".{0,200}location\.href.{0,5}/api/.{0,200}", re.S)),
    ]

    _EP_RE  = re.compile(r"/api/([a-zA-Z0-9_-]+)")
    _PRM_RE = re.compile(r'params\.append\(["\']([^"\']+)["\']')

    results: list[dict] = []
    seen_contexts: set[str] = set()

    for src, url in js_urls:
        try:
            jr = s.get(url, timeout=10)
            if jr.status_code != 200:
                continue
            js = jr.text
        except Exception:
            continue

        for label, pattern in _API_PATTERNS:
            for m in pattern.finditer(js):
                snippet = m.group(0)
                # deduplicate near-identical snippets
                key = snippet[:80]
                if key in seen_contexts:
                    continue
                seen_contexts.add(key)

                # extract endpoint name
                ep_match = _EP_RE.search(snippet)
                endpoint = ep_match.group(1) if ep_match else None

                # extract param names
                params = _PRM_RE.findall(snippet)

                hit = {
                    "js_file":  src,
                    "trigger":  label,
                    "endpoint": endpoint,
                    "params":   params,
                    "context":  snippet.strip()[:400],
                }
                results.append(hit)

                if verbose:
                    print(f"\n  [{label}] in {src}")
                    if endpoint:
                        print(f"    endpoint : /api/{endpoint}")
                    if params:
                        print(f"    params   : {params}")
                    print(f"    snippet  : {snippet.strip()[:120]} ...")

    if verbose:
        print(f"\nTotal hits: {len(results)}")

    return results


def run(update: bool = False, verbose: bool = False) -> list[dict]:
    """Synchronous entry point."""
    return asyncio.run(_discover(update=update, verbose=verbose))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="NSE endpoint discovery agent")
    ap.add_argument("--update",  action="store_true", help="Write new endpoints into endpoints.json")
    ap.add_argument("--verbose", action="store_true", help="Print all captured endpoints, not just new ones")
    ap.add_argument("--scan",    metavar="URL",        help="Static JS scan of one NSE page (cloud-safe)")
    args = ap.parse_args()

    if args.scan:
        print(f"Static JS scan: {args.scan}\n")
        hits = scan_page_js(args.scan, verbose=True)
        print(f"\n{len(hits)} hit(s) found.")
    else:
        print("Starting NSE endpoint discovery …\n")
        found = run(update=args.update, verbose=args.verbose)

        if found:
            print(f"\n{len(found)} NEW endpoint(s) found:\n")
            for ep in found:
                print(f"  {ep['path']}")
                print(f"    params : {ep['params']}")
                print(f"    status : {ep['status']}")
                print()
        else:
            print("\nNo new endpoints found — catalogue is up to date.")
