#!/usr/bin/env python3
"""
Rebuild sector_map.json using NSE's official sector/industry/macro metadata.

Fetches equity_meta_info() for all tracked tickers in parallel (20 workers).
Updates:
  memory_store/sector_map.json       — symbol → macro (used by alert_agent)
  memory_store/company_meta.json     — full metadata: sector, industry, macro, isin, is_fno, indices

Also patches Company nodes in graph.db with sector/industry/macro if present.

Run: python jobs/rebuild_sector_map.py
     Takes ~20s for 885 symbols with 20 workers.
"""
import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

WORKERS = 20
OUT_SECTOR_MAP  = REPO / "memory_store" / "sector_map.json"
OUT_COMPANY_META = REPO / "memory_store" / "company_meta.json"


def load_universe() -> list[str]:
    prog = REPO / "memory_store" / "kb_build_progress.json"
    if not prog.exists():
        # fallback to symbols.json
        syms_path = REPO / "memory_store" / "symbols.json"
        if syms_path.exists():
            return json.loads(syms_path.read_text())
        return []
    data = json.loads(prog.read_text())
    return sorted(s.upper() for s in data)


def fetch_one(nse, symbol: str) -> dict:
    try:
        m = nse.equity_meta_info(symbol)
        if not m:
            return {"symbol": symbol, "error": "empty"}
        return {
            "symbol":       symbol,
            "company_name": m.get("company_name", ""),
            "isin":         m.get("isin", ""),
            "sector":       m.get("sector", ""),
            "industry":     m.get("industry", ""),
            "macro":        m.get("macro", "Other"),
            "is_fno":       m.get("is_fno", False),
            "listing_date": m.get("listing_date", ""),
            "face_value":   m.get("face_value"),
            "indices":      m.get("indices", []),
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


def patch_graph_companies(meta_by_symbol: dict):
    """Patch Company nodes in graph.db with sector/industry/macro."""
    try:
        import sqlite3
        db_path = REPO / "memory_store" / "graph.db"
        if not db_path.exists():
            return
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        updated = 0
        for sym, m in meta_by_symbol.items():
            if "error" in m:
                continue
            node_id = f"company-{sym}"
            row = conn.execute(
                "SELECT properties FROM nodes WHERE id=?", (node_id,)
            ).fetchone()
            if not row:
                continue
            props = json.loads(row["properties"]) if isinstance(row["properties"], str) else row["properties"]
            props["sector"]   = m.get("sector", "")
            props["industry"] = m.get("industry", "")
            props["macro"]    = m.get("macro", "Other")
            props["isin"]     = m.get("isin", "")
            props["is_fno"]   = m.get("is_fno", False)
            conn.execute(
                "UPDATE nodes SET properties=? WHERE id=?",
                (json.dumps(props), node_id)
            )
            updated += 1
        conn.commit()
        conn.close()
        print(f"[sector_map] patched {updated} Company nodes in graph.db")
    except Exception as e:
        print(f"[sector_map] graph patch failed: {e}")


def run():
    sys.path.insert(0, str(REPO.parent / "irtteja" / "nselib"))
    from nselib import NSE

    symbols = load_universe()
    print(f"[sector_map] fetching metadata for {len(symbols)} symbols ({WORKERS} workers)…")
    t0 = time.time()

    results = {}
    errors  = []

    with NSE() as nse:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(fetch_one, nse, sym): sym for sym in symbols}
            done = 0
            for future in as_completed(futures):
                sym = futures[future]
                r   = future.result()
                done += 1
                if "error" in r:
                    errors.append(r)
                else:
                    results[sym] = r
                if done % 100 == 0:
                    print(f"  {done}/{len(symbols)} done…")

    elapsed = time.time() - t0
    print(f"[sector_map] fetched {len(results)} ok, {len(errors)} errors in {elapsed:.1f}s")

    # Build sector_map: symbol → macro (for alert_agent backward compat)
    sector_map = {sym: m["macro"] for sym, m in results.items()}
    # Preserve any existing entries not in NSE response
    if OUT_SECTOR_MAP.exists():
        existing = json.loads(OUT_SECTOR_MAP.read_text())
        for sym, sec in existing.items():
            if sym not in sector_map:
                sector_map[sym] = sec

    OUT_SECTOR_MAP.write_text(json.dumps(dict(sorted(sector_map.items())), indent=2))
    print(f"[sector_map] sector_map.json: {len(sector_map)} entries → {OUT_SECTOR_MAP}")

    # Save full metadata
    OUT_COMPANY_META.write_text(json.dumps(
        {sym: m for sym, m in sorted(results.items())}, indent=2, default=str
    ))
    print(f"[sector_map] company_meta.json: {len(results)} entries → {OUT_COMPANY_META}")

    # Macro distribution
    from collections import Counter
    macro_counts = Counter(m["macro"] for m in results.values())
    print("\n[sector_map] macro distribution:")
    for macro, cnt in sorted(macro_counts.items(), key=lambda x: -x[1]):
        print(f"  {macro}: {cnt}")

    # Patch graph.db
    patch_graph_companies(results)

    if errors:
        print(f"\n[sector_map] errors ({len(errors)}):")
        for e in errors[:10]:
            print(f"  {e['symbol']}: {e['error']}")

    return {"ok": len(results), "errors": len(errors), "sector_map_size": len(sector_map)}


if __name__ == "__main__":
    run()
