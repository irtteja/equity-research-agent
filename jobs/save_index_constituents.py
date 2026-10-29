#!/usr/bin/env python3
"""
Download NSE index constituent lists and build a symbol→indices reverse lookup.

Saves:
  data/index_constituents/{INDEX_NAME}.json   — constituent list per index
  data/symbol_to_indices.json                 — reverse lookup: symbol → [index, ...]

Skips indices in omitted categories (factor/strategy, ESG, leverage/inverse,
bonds/fixed-income, volatility, SME, Tata-branded). Any new index added by NSE
that doesn't match the skip rules is included automatically.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.expanduser("~/irtteja/nselib"))

import json
import re
from pathlib import Path
from datetime import date

REPO_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = REPO_DIR / "data" / "index_constituents"
UNIVERSE_PATH = REPO_DIR / "data" / "tracked_universe.json"
SYMBOL_MAP_PATH = REPO_DIR / "data" / "symbol_to_indices.json"

# Patterns that identify indices to SKIP.
# Checked against the index name (upper-cased).
_SKIP_PATTERNS = [
    # Bonds / Fixed Income
    r"^BHARATBOND",
    r"^NIFTY GS ",
    # (INDIA VIX kept — volatility reference index)
    # SME
    r"^NIFTY SME EMERGE$",
    # Leverage / Inverse
    r"\bPR [12]X (INV|LEV)\b",
    r"\bTR [12]X (INV|LEV)\b",
    r"\bNIFTY50 USD\b",
    # ESG / Shariah
    r"\bESG\b",
    r"\bSHARIAH\b",
    # Factor / Strategy
    r"\bALPHA\b",
    r"\bLOWVOL\b",
    r"\bHIGHBETA\b",
    r"\bQUALT[YI]\b",
    r"\bQLTY\b",
    r"\b(VALUE|MOMENTM|MOMNTM)\b",
    r"\bDIV OPPS\b",
    r"\bDIV POINT\b",
    r"\bAQL\b",
    r"\bTMMQ\b",
    r"\bGROWSECT\b",
    r"\bFPI \d",
    r"\bCORP MAATR\b",
    r"\bTOP (10|15|20) EW\b",
    r"\bEQL WGT\b",
    r"\b500 EW\b",
    r"\bLMS EQL\b",
    r"\bLIQ 15\b",
    r"\bFLEXICAP\b",
    r"\bALPHALOWVOL\b",
    r"\bQLTY LV\b",
    # Tata-branded / niche composites
    r"\bTATA 25\b",
    r"\bSML250 Q50\b",
    r"^NIFTYMS400\b",
    r"^NIFTYSML250MQ\b",
    r"\bMQ \d",
    r"\bMQVLV\b",
    r"\b500 QLTY\b",
    r"\bM150 QLTY\b",
    r"\bM150 MOMNTM\b",
    r"\bMS400\b",
]

_SKIP_RE = [re.compile(p, re.IGNORECASE) for p in _SKIP_PATTERNS]


def should_skip(index_name: str) -> bool:
    for rx in _SKIP_RE:
        if rx.search(index_name):
            return True
    return False


def fetch_constituents(nse, index_name: str) -> list[dict]:
    """Return list of constituent dicts for the given index."""
    try:
        stocks = nse.list_equity_stocks_by_index(index_name)
        if stocks and isinstance(stocks, list):
            return stocks
    except Exception:
        pass
    # fallback
    try:
        data = nse.index_constituents(index_name)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "stocks", "constituents"):
                if key in data and isinstance(data[key], list):
                    return data[key]
    except Exception:
        pass
    return []


def run(force: bool = False) -> dict:
    from nselib import NSE

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with NSE() as nse:
        index_list_data = nse.list_indices()

    all_indices = [d["indexSymbol"] for d in index_list_data.get("data", [])]
    included = [n for n in all_indices if not should_skip(n)]
    skipped = [n for n in all_indices if should_skip(n)]

    print(f"Total indices: {len(all_indices)}  |  included: {len(included)}  |  skipped: {len(skipped)}")

    symbol_to_indices: dict[str, list[str]] = {}
    saved, errors, already_exists = [], [], []

    with NSE() as nse:
        for idx_name in included:
            safe_name = re.sub(r'[^\w\s-]', '', idx_name).strip().replace(' ', '_')
            out_path = OUT_DIR / f"{safe_name}.json"

            if out_path.exists() and not force:
                already_exists.append(idx_name)
                # Still load for reverse lookup
                with open(out_path) as f:
                    existing = json.load(f)
                for row in existing.get("constituents", []):
                    sym = row.get("symbol") or row.get("TckrSymb") or row.get("symbolName", "")
                    if sym:
                        symbol_to_indices.setdefault(sym, [])
                        if idx_name not in symbol_to_indices[sym]:
                            symbol_to_indices[sym].append(idx_name)
                continue

            constituents = fetch_constituents(nse, idx_name)
            if not constituents:
                print(f"  WARN: no data for {idx_name}")
                errors.append(idx_name)
                continue

            # Normalise: keep symbol + isin at minimum
            rows = []
            for c in constituents:
                row = {}
                for sym_key in ("symbol", "TckrSymb", "symbolName", "Symbol"):
                    if c.get(sym_key):
                        row["symbol"] = str(c[sym_key]).strip()
                        break
                for isin_key in ("isin", "ISIN", "isinCode"):
                    if c.get(isin_key):
                        row["isin"] = str(c[isin_key]).strip()
                        break
                if row.get("symbol"):
                    rows.append(row)
                    symbol_to_indices.setdefault(row["symbol"], [])
                    if idx_name not in symbol_to_indices[row["symbol"]]:
                        symbol_to_indices[row["symbol"]].append(idx_name)

            payload = {
                "index": idx_name,
                "as_of": date.today().isoformat(),
                "count": len(rows),
                "constituents": rows,
            }
            with open(out_path, "w") as f:
                json.dump(payload, f, indent=2)
            saved.append(idx_name)
            print(f"  {idx_name}: {len(rows)} constituents")

    # Write reverse lookup
    with open(SYMBOL_MAP_PATH, "w") as f:
        json.dump(symbol_to_indices, f, indent=2, sort_keys=True)
    print(f"\nReverse lookup: {len(symbol_to_indices)} symbols → {SYMBOL_MAP_PATH.relative_to(REPO_DIR)}")

    # Enrich tracked_universe.json with indices field
    with open(UNIVERSE_PATH) as f:
        universe_data = json.load(f)
    updated = 0
    for company in universe_data.get("universe", []):
        sym = company.get("symbol", "")
        idx_list = symbol_to_indices.get(sym, [])
        if company.get("indices") != idx_list:
            company["indices"] = idx_list
            updated += 1
    with open(UNIVERSE_PATH, "w") as f:
        json.dump(universe_data, f, indent=2)
    print(f"Universe enriched: {updated} companies updated with index membership")

    return {
        "total": len(all_indices),
        "included": len(included),
        "saved": len(saved),
        "already_exists": len(already_exists),
        "errors": len(errors),
        "error_list": errors,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Re-download even if file exists")
    args = parser.parse_args()
    result = run(force=args.force)
    print(f"\nDone: {result}")
