"""
Build sector_map.json from live NSE sector indices.

Pulls constituent lists from each sector index and writes
memory_store/sector_map.json (symbol → sector string).

Priority order: specific sector indices (IT, Bank, Auto…) win over
broader indices (Infra, Commodities, Services) when a stock appears
in both.
"""
import json
import sys
from pathlib import Path
from collections import defaultdict

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

# (index_name, sector, priority) — lower priority number = more specific
SECTOR_INDICES = [
    # Specific — priority 1
    ("NIFTY IT",               "IT",          1),
    ("NIFTY BANK",             "Finance",     1),
    ("NIFTY AUTO",             "Auto",        1),
    ("NIFTY PHARMA",           "Pharma",      1),
    ("NIFTY FMCG",             "FMCG",        1),
    ("NIFTY METAL",            "Metals",      1),
    ("NIFTY ENERGY",           "Energy",      1),
    ("NIFTY REALTY",           "Realty",      1),
    ("NIFTY MEDIA",            "Media",       1),
    ("NIFTY PSU BANK",         "Finance",     1),
    ("NIFTY CONSUMER DURABLES","Consumer",    1),
    ("NIFTY OIL AND GAS",      "Energy",      1),
    ("NIFTY HEALTHCARE INDEX", "Healthcare",  1),
    # Broader — priority 2
    ("NIFTY FIN SERVICE",      "Finance",     2),
    ("NIFTY INFRASTRUCTURE",   "Infra",       2),
    ("NIFTY COMMODITIES",      "Commodities", 2),
    ("NIFTY SERVICES SECTOR",  "Services",    2),
]


def build():
    from nselib import NSE
    nse = NSE()

    # symbol → (sector, priority)
    best: dict[str, tuple[str, int]] = {}

    for idx_name, sector, priority in SECTOR_INDICES:
        try:
            members = nse.index_constituents(idx_name)
            added = 0
            for m in members:
                sym = (m.get("symbol") or "").strip().upper()
                if not sym or m.get("series") != "EQ":
                    continue
                existing = best.get(sym)
                if existing is None or priority < existing[1]:
                    best[sym] = (sector, priority)
                    added += 1
            print(f"  {idx_name:<35} {len([m for m in members if m.get('series')=='EQ']):>3} stocks → {sector}")
        except Exception as e:
            print(f"  {idx_name:<35} ERROR: {e}")

    sector_map = {sym: sec for sym, (sec, _) in sorted(best.items())}

    # Summary
    counts: dict[str, int] = defaultdict(int)
    for sec in sector_map.values():
        counts[sec] += 1
    print(f"\nTotal mapped: {len(sector_map)} symbols across {len(counts)} sectors")
    for sec, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {sec:<20} {n}")

    out = REPO / "memory_store" / "sector_map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sector_map, indent=2, sort_keys=True))
    print(f"\nSaved → {out}")
    return sector_map


if __name__ == "__main__":
    build()
