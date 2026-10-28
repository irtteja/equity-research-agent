#!/usr/bin/env python3
"""
Universe updater — runs weekly (Sunday 7am IST).
Re-fetches Nifty Total Market constituents, re-maps thematic indices and sectors,
diffs against the saved universe, and commits to GitHub only when something changed.
"""
import sys, os, json, time, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_NSELIB_PATH = os.path.expanduser("~/irtteja/nselib")
if _NSELIB_PATH not in sys.path:
    sys.path.insert(0, _NSELIB_PATH)

from datetime import date

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _self_update():
    """Pull latest code from GitHub before running, so scheduled tasks always use current code."""
    try:
        result = subprocess.run(
            ["git", "-C", REPO_DIR, "pull", "origin", "master"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            print(f"[git] {result.stdout.strip() or 'Already up to date.'}")
        else:
            print(f"[git] pull warning: {result.stderr.strip()}")
    except Exception as e:
        print(f"[git] pull failed (continuing anyway): {e}")
UNIVERSE_PATH = os.path.join(REPO_DIR, "data", "tracked_universe.json")
PROGRESS_PATH = os.path.join(REPO_DIR, "PROGRESS.md")

_SME_SERIES = {"SM", "ST"}

THEMATIC_INDICES = {
    "NIFTY AUTO":           "NIFTY AUTO",
    "NIFTY BANK":           "NIFTY BANK",
    "NIFTY PSU BANK":       "NIFTY PSU BANK",
    "NIFTY PVT BANK":       "NIFTY PRIVATE BANK",
    "NIFTY FIN SERVICE":    "NIFTY FINANCIAL SERVICES",
    "NIFTY FMCG":           "NIFTY FMCG",
    "NIFTY IT":             "NIFTY IT",
    "NIFTY MEDIA":          "NIFTY MEDIA",
    "NIFTY METAL":          "NIFTY METAL",
    "NIFTY PHARMA":         "NIFTY PHARMA",
    "NIFTY HEALTHCARE":     "NIFTY HEALTHCARE INDEX",
    "NIFTY REALTY":         "NIFTY REALTY",
    "NIFTY CONSR DURBL":    "NIFTY CONSUMER DURABLES",
    "NIFTY OIL AND GAS":    "NIFTY OIL & GAS",
    "NIFTY CHEMICALS":      "NIFTY CHEMICALS",
    "NIFTY CEMENT":         "NIFTY CEMENT",
    "NIFTY COMMODITIES":    "NIFTY COMMODITIES",
    "NIFTY CONSUMPTION":    "NIFTY INDIA CONSUMPTION",
    "NIFTY CPSE":           "NIFTY CPSE",
    "NIFTY ENERGY":         "NIFTY ENERGY",
    "NIFTY INFRA":          "NIFTY INFRASTRUCTURE",
    "NIFTY MNC":            "NIFTY MNC",
    "NIFTY PSE":            "NIFTY PSE",
    "NIFTY SERV SECTOR":    "NIFTY SERVICES SECTOR",
    "NIFTY IND DIGITAL":    "NIFTY INDIA DIGITAL",
    "NIFTY INDIA MFG":      "NIFTY INDIA MANUFACTURING",
    "NIFTY IND DEFENCE":    "NIFTY INDIA DEFENCE",
    "NIFTY IND TOURISM":    "NIFTY INDIA TOURISM",
    "NIFTY CAPITAL MKT":    "NIFTY CAPITAL MARKETS",
    "NIFTY EV":             "NIFTY EV & NEW AGE AUTOMOTIVE",
    "NIFTY HOUSING":        "NIFTY HOUSING",
    "NIFTY COREHOUSING":    "NIFTY CORE HOUSING",
    "NIFTY RURAL":          "NIFTY RURAL",
    "NIFTY TRANS LOGIS":    "NIFTY TRANSPORTATION & LOGISTICS",
    "NIFTY RAILWAYSPSU":    "NIFTY INDIA RAILWAYS PSU",
    "NIFTYCONGLOMERATE":    "NIFTY CONGLOMERATE 50",
    "NIFTY IPO":            "NIFTY IPO",
    "NIFTY INTERNET":       "NIFTY INDIA INTERNET",
    "NIFTY MOBILITY":       "NIFTY MOBILITY",
    "NIFTY NEW CONSUMP":    "NIFTY INDIA NEW AGE CONSUMPTION",
    "NIFTY NONCYC CONS":    "NIFTY NON-CYCLICAL CONSUMER",
    "NIFTY WAVES":          "NIFTY WAVES",
    "NIFTY INFRALOG":       "NIFTY INDIA INFRASTRUCTURE & LOGISTICS",
}

CAP_INDICES = {
    "NIFTY 50":           "Large Cap",
    "NIFTY NEXT 50":      "Large Cap",
    "NIFTY MIDCAP 150":   "Mid Cap",
    "NIFTY SMLCAP 250":   "Small Cap",
    "NIFTY MICROCAP250":  "Micro Cap",
}

SECTOR_RULES = [
    ({"NIFTY IT"},                                    "Technology — IT Services"),
    ({"NIFTY INDIA DIGITAL", "NIFTY INDIA INTERNET"}, "Technology — Digital & Internet"),
    ({"NIFTY PHARMA"},                                "Healthcare — Pharma"),
    ({"NIFTY HEALTHCARE INDEX"},                      "Healthcare — Hospitals & Services"),
    ({"NIFTY BANK"},                                  "Banking — Private & PSU"),
    ({"NIFTY PSU BANK"},                              "Banking — PSU"),
    ({"NIFTY PRIVATE BANK"},                          "Banking — Private"),
    ({"NIFTY FINANCIAL SERVICES"},                    "Financial Services"),
    ({"NIFTY CAPITAL MARKETS"},                       "Financial Services — Capital Markets"),
    ({"NIFTY FMCG"},                                  "Consumer — FMCG"),
    ({"NIFTY NON-CYCLICAL CONSUMER"},                 "Consumer — Non-Cyclical"),
    ({"NIFTY CONSUMER DURABLES"},                     "Consumer — Durables"),
    ({"NIFTY INDIA NEW AGE CONSUMPTION"},             "Consumer — New Age"),
    ({"NIFTY INDIA CONSUMPTION"},                     "Consumer — Broad"),
    ({"NIFTY AUTO"},                                  "Auto & EV"),
    ({"NIFTY EV & NEW AGE AUTOMOTIVE"},               "Auto & EV"),
    ({"NIFTY MOBILITY"},                              "Auto & EV"),
    ({"NIFTY METAL"},                                 "Metals & Mining"),
    ({"NIFTY COMMODITIES"},                           "Commodities"),
    ({"NIFTY OIL & GAS"},                             "Energy — Oil & Gas"),
    ({"NIFTY ENERGY"},                                "Energy — Broad"),
    ({"NIFTY CHEMICALS"},                             "Chemicals"),
    ({"NIFTY CEMENT"},                                "Infrastructure — Cement"),
    ({"NIFTY REALTY"},                                "Real Estate"),
    ({"NIFTY HOUSING", "NIFTY CORE HOUSING"},         "Real Estate & Housing"),
    ({"NIFTY INFRASTRUCTURE"},                        "Infrastructure — Broad"),
    ({"NIFTY INDIA MANUFACTURING"},                   "Manufacturing"),
    ({"NIFTY INDIA DEFENCE"},                         "Defence & Aerospace"),
    ({"NIFTY INDIA RAILWAYS PSU"},                    "Railways & PSU"),
    ({"NIFTY TRANSPORTATION & LOGISTICS"},            "Logistics & Transport"),
    ({"NIFTY INDIA INFRASTRUCTURE & LOGISTICS"},      "Infrastructure & Logistics"),
    ({"NIFTY INDIA TOURISM"},                         "Tourism & Hospitality"),
    ({"NIFTY MEDIA"},                                 "Media & Entertainment"),
    ({"NIFTY WAVES"},                                 "Media & Entertainment"),
    ({"NIFTY RURAL"},                                 "Rural & Agri"),
    ({"NIFTY SERVICES SECTOR"},                       "Services — Broad"),
    ({"NIFTY CPSE", "NIFTY PSE"},                     "PSU — Government Enterprise"),
    ({"NIFTY MNC"},                                   "MNC"),
    ({"NIFTY CONGLOMERATE 50"},                       "Conglomerate"),
    ({"NIFTY IPO"},                                   "Recent IPO"),
]

MACRO_MAP = {
    "Financial Services": "Financial Services",
    "Consumer Goods": "Consumer — FMCG",
    "Consumer Services": "Consumer — Services",
    "Information Technology": "Technology — IT Services",
    "Healthcare": "Healthcare — Pharma",
    "Energy": "Energy — Broad",
    "Basic Materials": "Metals & Mining",
    "Industrials": "Manufacturing",
    "Real Estate": "Real Estate",
    "Communication Services": "Media & Entertainment",
    "Utilities": "Utilities",
    "Agriculture": "Rural & Agri",
}

SECTOR_MAP = {
    "Banks": "Banking — Private & PSU",
    "Finance": "Financial Services",
    "Insurance": "Insurance",
    "Pharmaceuticals": "Healthcare — Pharma",
    "Healthcare": "Healthcare — Hospitals & Services",
    "IT-Software": "Technology — IT Services",
    "IT-Hardware": "Technology — IT Hardware",
    "Chemicals": "Chemicals",
    "Automobiles": "Auto & EV",
    "Auto Components": "Auto & EV",
    "Oil Gas & Consumable Fuels": "Energy — Oil & Gas",
    "Power": "Energy — Power",
    "Cement": "Infrastructure — Cement",
    "Construction": "Infrastructure — Construction",
    "Real Estate": "Real Estate",
    "Metals": "Metals & Mining",
    "Mining": "Metals & Mining",
    "Media": "Media & Entertainment",
    "Entertainment": "Media & Entertainment",
    "Textiles": "Textiles & Apparel",
    "FMCG": "Consumer — FMCG",
    "Retailing": "Consumer — Retail",
    "Food": "Consumer — Food & Beverages",
    "Beverages": "Consumer — Food & Beverages",
    "Hotels": "Tourism & Hospitality",
    "Tourism": "Tourism & Hospitality",
    "Aviation": "Logistics & Transport",
    "Logistics": "Logistics & Transport",
    "Shipping": "Logistics & Transport",
    "Transport": "Logistics & Transport",
    "Telecom": "Telecom",
    "Capital Goods": "Capital Goods",
    "Engineering": "Capital Goods",
    "Defence": "Defence & Aerospace",
    "Aerospace": "Defence & Aerospace",
    "Diversified": "Conglomerate",
    "Trading": "Trading & Distribution",
    "Agrochemicals": "Chemicals",
    "Fertilizers": "Rural & Agri",
    "Agro": "Rural & Agri",
    "Paper": "Paper & Packaging",
    "Packaging": "Paper & Packaging",
    "Paints": "Chemicals",
}


def _nse_meta_sector(nse, sym):
    try:
        meta = nse.equity_meta_info(sym)
        return meta.get("sector", ""), meta.get("macro", ""), meta.get("industry", "")
    except Exception:
        return "", "", ""


def _assign_sector_from_meta(nse_sector, nse_macro):
    for k, v in SECTOR_MAP.items():
        if k.lower() in nse_sector.lower():
            return v
    for k, v in MACRO_MAP.items():
        if k.lower() in nse_macro.lower():
            return v
    return f"Others — {nse_macro}" if nse_macro else "Others — Diversified"


def _git_commit(message):
    try:
        subprocess.run(["git", "add", "data/tracked_universe.json", "PROGRESS.md"],
                       cwd=REPO_DIR, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=REPO_DIR, check=True)
        subprocess.run(["git", "push", "-u", "origin", "master"], cwd=REPO_DIR, check=True)
        print(f"[git] {message}")
    except subprocess.CalledProcessError as e:
        print(f"[git] {e}")


def run():
    _self_update()

    from nselib import NSE

    today = date.today().isoformat()
    print(f"\n{'='*60}")
    print(f"Universe Updater — {today}")
    print(f"{'='*60}\n")

    # Load existing universe
    old_syms = set()
    if os.path.exists(UNIVERSE_PATH):
        with open(UNIVERSE_PATH) as f:
            old_data = json.load(f)
        old_syms = {s["symbol"] for s in old_data.get("universe", [])}

    with NSE() as nse:
        # 1. Fetch base universe
        print("[1/4] Fetching Nifty Total Market constituents...")
        base = nse.index_constituents("NIFTY TOTAL MARKET")
        stocks_raw = {
            r["symbol"]: r for r in base
            if r.get("symbol") != "NIFTY TOTAL MARKET"
            and r.get("series") is not None
            and str(r.get("series", "")).upper() not in _SME_SERIES
        }
        new_syms = set(stocks_raw.keys())
        print(f"  {len(new_syms)} mainboard stocks")

        # Diff
        added = sorted(new_syms - old_syms)
        removed = sorted(old_syms - new_syms)
        if not added and not removed:
            print(f"\nNo constituent changes since last update. Skipping commit.")
            return
        print(f"  +{len(added)} added, -{len(removed)} removed")
        if added:   print(f"  Added:   {', '.join(added)}")
        if removed: print(f"  Removed: {', '.join(removed)}")

        # 2. Cap tiers
        print("\n[2/4] Mapping cap tiers...")
        cap_map = {}
        for idx_key, tier in CAP_INDICES.items():
            try:
                members = nse.index_constituents(idx_key)
                for r in members:
                    sym = r.get("symbol")
                    if sym and sym in stocks_raw and sym not in cap_map:
                        cap_map[sym] = tier
                time.sleep(0.3)
            except Exception as e:
                print(f"  {idx_key}: {e}")
        for sym in stocks_raw:
            if sym not in cap_map:
                cap_map[sym] = "Micro Cap"

        # 3. Thematic index membership
        print("\n[3/4] Fetching thematic index memberships...")
        thematic_map = {}
        index_members = {}
        for idx_key, idx_label in THEMATIC_INDICES.items():
            try:
                members = nse.index_constituents(idx_key)
                syms = [
                    r["symbol"] for r in members
                    if r.get("symbol") and r.get("symbol") in stocks_raw
                    and str(r.get("series", "")).upper() not in _SME_SERIES
                ]
                index_members[idx_label] = syms
                for sym in syms:
                    thematic_map.setdefault(sym, []).append(idx_label)
                time.sleep(0.25)
            except Exception as e:
                print(f"  {idx_label}: {e}")

        # 4. Sector assignment
        print("\n[4/4] Assigning sectors...")
        sym_idx = {}
        for idx_name, syms in index_members.items():
            for sym in syms:
                sym_idx.setdefault(sym, set()).add(idx_name)

        def assign_sector(sym):
            indices = sym_idx.get(sym, set())
            for required, label in SECTOR_RULES:
                if indices & required:
                    return label
            return None  # needs meta lookup

        # Bulk-assign via sector index membership
        needs_meta = []
        sector_pre = {}
        for sym in stocks_raw:
            sec = assign_sector(sym)
            if sec:
                sector_pre[sym] = sec
            else:
                needs_meta.append(sym)

        print(f"  {len(needs_meta)} stocks need NSE meta lookup...")
        meta_cache = {}
        for i, sym in enumerate(needs_meta):
            nse_sec, nse_mac, nse_ind = _nse_meta_sector(nse, sym)
            meta_cache[sym] = {"sector": nse_sec, "macro": nse_mac, "industry": nse_ind}
            sector_pre[sym] = _assign_sector_from_meta(nse_sec, nse_mac)
            if (i + 1) % 50 == 0:
                print(f"    {i+1}/{len(needs_meta)}...")
            time.sleep(0.15)

    # Build universe list
    universe = []
    for sym, data in stocks_raw.items():
        m = meta_cache.get(sym, {})
        universe.append({
            "symbol": sym,
            "company": data.get("companyName") or "",
            "series": data.get("series", "EQ"),
            "ltp": data.get("lastPrice"),
            "pchange": data.get("pChange"),
            "pchange_365d": data.get("perChange365d"),
            "year_high": data.get("yearHigh"),
            "year_low": data.get("yearLow"),
            "ffmc": data.get("ffmc"),
            "cap_tier": cap_map.get(sym, "Micro Cap"),
            "thematic_indices": thematic_map.get(sym, []),
            "sector": sector_pre.get(sym, "Others — Diversified"),
            "nse_sector": m.get("sector", ""),
            "nse_macro": m.get("macro", ""),
            "nse_industry": m.get("industry", ""),
        })
    universe.sort(key=lambda x: x.get("ffmc") or 0, reverse=True)

    # Build sector groups
    sector_groups = {}
    for s in universe:
        sector_groups.setdefault(s["sector"], []).append(s["symbol"])

    out = {
        "date": today,
        "total": len(universe),
        "universe": universe,
        "thematic_index_members": index_members,
        "sector_groups": sector_groups,
        "last_diff": {
            "date": today,
            "added": added,
            "removed": removed,
        },
    }

    os.makedirs(os.path.dirname(UNIVERSE_PATH), exist_ok=True)
    with open(UNIVERSE_PATH, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved {len(universe)} stocks to {UNIVERSE_PATH}")

    # Update PROGRESS.md
    with open(PROGRESS_PATH, "a") as f:
        f.write(f"\n## {today} — Universe Update\n")
        f.write(f"- Total: {len(universe)} mainboard stocks\n")
        if added:
            f.write(f"- Added: {', '.join(added)}\n")
        if removed:
            f.write(f"- Removed: {', '.join(removed)}\n")

    msg = f"chore: universe update {today} — +{len(added)} -{len(removed)} ({len(universe)} total)"
    _git_commit(msg)
    print(f"\nDone. Changes committed.")


if __name__ == "__main__":
    run()
