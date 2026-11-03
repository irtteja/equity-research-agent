#!/usr/bin/env python3
"""
Build/rebuild the knowledge graph enrichment layers in graph.db.

Adds structural nodes and edges beyond Company/MetricValue/TrendSummary:
  Layer 1 — MacroSector, Sector, Industry nodes + Company→BELONGS_TO edges
  Layer 2 — Index nodes + Company→CONSTITUENT_OF→Index edges
  Layer 3 — COMPETES_WITH edges (companies in same NSE industry)
  Layer 4 — BusinessGroup nodes + Company→PART_OF_GROUP edges

Source: memory_store/company_meta.json (rebuilt by jobs/rebuild_sector_map.py)
Output: memory_store/graph.db (upserts; idempotent)

Run: python jobs/build_knowledge_graph.py
     Typically takes <10s. Safe to re-run.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
import re
import sys

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"
NOW     = datetime.now(timezone.utc).isoformat()

STRUCTURAL_EDGE_TYPES = {
    "PART_OF_MACRO", "PART_OF_SECTOR", "BELONGS_TO_SECTOR", "BELONGS_TO_INDUSTRY",
    "CONSTITUENT_OF", "COMPETES_WITH", "PART_OF_GROUP",
}

GROUP_RULES = [
    (r"\bTata\b",                                   "Tata Group"),
    (r"\bAdani\b",                                  "Adani Group"),
    (r"\bReliance\b",                               "Reliance Industries Group"),
    (r"\bHDFC\b",                                   "HDFC Group"),
    (r"\bICICI\b",                                  "ICICI Group"),
    (r"\bBirla\b|Aditya Birla|Grasim|Hindalco|Ultratech", "Aditya Birla Group"),
    (r"\bBajaj\b",                                  "Bajaj Group"),
    (r"\bMahindra\b",                               "Mahindra Group"),
    (r"\bInfosys\b",                                "Infosys Group"),
    (r"\bWipro\b",                                  "Wipro Group"),
    (r"\bHCL\b",                                    "HCL Group"),
    (r"\bL&T\b|Larsen|Mindtree|LTI\b|LTIM\b",      "Larsen & Toubro Group"),
    (r"\bITC\b",                                    "ITC Group"),
    (r"\bSBI\b|State Bank",                         "SBI Group"),
    (r"\bAxis Bank\b",                              "Axis Group"),
    (r"\bKotak\b",                                  "Kotak Group"),
    (r"\bSun Pharma\b|Sun Pharmaceutical",          "Sun Pharma Group"),
    (r"\bCipla\b",                                  "Cipla Group"),
    (r"\bTVS\b",                                    "TVS Group"),
    (r"\bBajaj Auto\b",                             "Bajaj Auto Group"),
    (r"\bMaruti\b",                                 "Maruti Suzuki Group"),
    (r"\bONGC\b",                                   "ONGC Group"),
    (r"\bNTPC\b",                                   "NTPC Group"),
    (r"\bGodrej\b",                                 "Godrej Group"),
    (r"\bMurugappa\b",                              "Murugappa Group"),
    (r"\bShriram\b",                                "Shriram Group"),
    (r"\bVedanta\b|Hindustan Zinc",                 "Vedanta Group"),
    (r"\bJSW\b",                                    "JSW Group"),
    (r"\bJindal\b",                                 "Jindal Group"),
    (r"\bEmami\b",                                  "Emami Group"),
    (r"\bDabur\b",                                  "Dabur Group"),
    (r"\bPidilite\b",                               "Pidilite Group"),
    (r"\bAsian Paints\b",                           "Asian Paints Group"),
]
_COMPILED_RULES = [(re.compile(p, re.IGNORECASE), g) for p, g in GROUP_RULES]


def _idx_id(name: str) -> str:
    return "INDEX_" + name.replace(' ', '_').replace('&', 'AND').replace('/', '_')


def _grp_id(name: str) -> str:
    clean = name.replace(' ', '_').replace('&', 'AND').replace('.', '').replace("'", '')
    return f"GROUP_{clean}"


def detect_group(company_name: str) -> str | None:
    for pat, grp in _COMPILED_RULES:
        if pat.search(company_name):
            return grp
    return None


def build_layers(meta: dict) -> tuple[dict, list]:
    """Return (nodes_dict, edges_list) for all structural layers."""
    nodes = {}
    edges = []
    seen_e = set()

    def add_node(nid, node_type, name, props=None):
        if nid not in nodes:
            nodes[nid] = {"id": nid, "node_type": node_type, "name": name,
                          "ticker": "", "properties": props or {}}

    def add_edge(etype, src, tgt, props=None):
        key = (etype, src, tgt)
        if key not in seen_e:
            seen_e.add(key)
            edges.append({"edge_type": etype, "source_id": src, "target_id": tgt,
                          "properties": props or {}})

    # Layer 1 — Sectors
    for sym, m in meta.items():
        macro    = (m.get("macro")    or "Other").strip()
        sector   = (m.get("sector")   or "Other").strip()
        industry = (m.get("industry") or "Other").strip()
        macro_id    = f"MACRO_{macro}"
        sector_id   = f"SECTOR_{sector}"
        industry_id = f"INDUSTRY_{industry}"
        company_id  = f"company-{sym}"
        add_node(macro_id,    "MacroSector", macro,    {"company_count": 0})
        add_node(sector_id,   "Sector",      sector,   {"macro": macro, "company_count": 0})
        add_node(industry_id, "Industry",    industry, {"sector": sector, "macro": macro, "company_count": 0})
        nodes[macro_id]["properties"]["company_count"]    += 1
        nodes[sector_id]["properties"]["company_count"]   += 1
        nodes[industry_id]["properties"]["company_count"] += 1
        add_edge("PART_OF_MACRO",       sector_id,   macro_id)
        add_edge("PART_OF_SECTOR",      industry_id, sector_id)
        add_edge("BELONGS_TO_SECTOR",   company_id,  sector_id)
        add_edge("BELONGS_TO_INDUSTRY", company_id,  industry_id)

    # Layer 2 — Indices
    index_counts: dict[str, int] = {}
    for sym, m in meta.items():
        for idx in m.get("indices", []):
            if isinstance(idx, str):
                name = idx
            elif isinstance(idx, dict):
                name = idx.get("name") or idx.get("indexName", "")
            else:
                continue
            name = name.strip()
            if name:
                index_counts[name] = index_counts.get(name, 0) + 1
    for name, cnt in index_counts.items():
        if cnt >= 2:
            tier = "broad" if any(k in name for k in ("NIFTY 50", "NIFTY 100", "NIFTY 200", "NIFTY 500", "SENSEX")) else "sector"
            add_node(_idx_id(name), "Index", name, {"member_count": cnt, "tier": tier})
    for sym, m in meta.items():
        for idx in m.get("indices", []):
            if isinstance(idx, str):
                name = idx
            elif isinstance(idx, dict):
                name = idx.get("name") or idx.get("indexName", "")
            else:
                continue
            name = name.strip()
            if name and index_counts.get(name, 0) >= 2:
                add_edge("CONSTITUENT_OF", f"company-{sym}", _idx_id(name))

    # Layer 3 — COMPETES_WITH (same NSE industry, ≤20 peers each)
    by_industry: dict[str, list] = {}
    for sym, m in meta.items():
        ind = (m.get("industry") or "Other").strip()
        by_industry.setdefault(ind, []).append(sym)
    for ind, syms in by_industry.items():
        if len(syms) < 2:
            continue
        syms_s = sorted(syms)
        for i, sym in enumerate(syms_s):
            lo = max(0, i - 10)
            hi = min(len(syms_s), i + 11)
            for peer in syms_s[lo:hi]:
                if peer != sym:
                    a, b = sorted([f"company-{sym}", f"company-{peer}"])
                    add_edge("COMPETES_WITH", a, b)

    # Layer 4 — BusinessGroups
    groups: dict[str, list] = {}
    for sym, m in meta.items():
        cname = (m.get("company_name") or sym).strip()
        grp = detect_group(cname)
        if grp:
            groups.setdefault(grp, []).append(sym)
    for grp_name, syms in groups.items():
        if len(syms) < 2:
            continue
        gid = _grp_id(grp_name)
        add_node(gid, "BusinessGroup", grp_name, {"member_count": len(syms), "members": syms})
        for sym in syms:
            add_edge("PART_OF_GROUP", f"company-{sym}", gid)

    return nodes, edges


def run():
    meta_path = REPO / "memory_store" / "company_meta.json"
    if not meta_path.exists():
        print("[build_kg] company_meta.json not found — run jobs/rebuild_sector_map.py first")
        return

    meta = json.loads(meta_path.read_text())
    print(f"[build_kg] building layers for {len(meta)} companies…")

    nodes, edges = build_layers(meta)
    print(f"[build_kg] generated {len(nodes)} nodes, {len(edges)} edges")

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    # Upsert nodes
    conn.executemany(
        "INSERT INTO nodes(id, node_type, name, ticker, properties, created_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET node_type=excluded.node_type, "
        "name=excluded.name, properties=excluded.properties",
        [(n["id"], n["node_type"], n["name"], n.get("ticker", ""),
          json.dumps(n.get("properties", {})), NOW)
         for n in nodes.values()]
    )

    # Delete + re-insert structural edges (clean rebuild, idempotent)
    for et in STRUCTURAL_EDGE_TYPES:
        conn.execute("DELETE FROM edges WHERE edge_type=?", (et,))
    conn.executemany(
        "INSERT INTO edges(id, edge_type, source_id, target_id, properties, created_at) "
        "VALUES (?,?,?,?,?,?)",
        [(str(uuid.uuid4()), e["edge_type"], e["source_id"], e["target_id"],
          json.dumps(e.get("properties", {})), NOW)
         for e in edges]
    )
    conn.commit()

    rows = conn.execute(
        "SELECT node_type, COUNT(*) FROM nodes GROUP BY node_type ORDER BY COUNT(*) DESC"
    ).fetchall()
    errows = conn.execute(
        "SELECT edge_type, COUNT(*) FROM edges GROUP BY edge_type ORDER BY COUNT(*) DESC"
    ).fetchall()
    total_n = sum(c for _, c in rows)
    total_e = sum(c for _, c in errows)
    conn.close()

    print(f"[build_kg] graph.db now: {total_n} nodes, {total_e} edges")
    for nt, cnt in rows:
        print(f"  {nt}: {cnt}")

    return {"nodes": total_n, "edges": total_e}


if __name__ == "__main__":
    run()
