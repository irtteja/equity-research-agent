"""
Export live dashboard data from graph.db to memory_store/dashboard_data.json,
and patch dashboard.html constants in-place so the artifact always shows fresh data.

Run after each swarm cycle (called from daily_monitor.py).
"""
import json
import re
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path

REPO          = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))
DB_PATH       = REPO / "memory_store" / "graph.db"
OUT_JSON      = REPO / "memory_store" / "dashboard_data.json"
DASHBOARD_HTML = Path("/tmp/claude-0/-home-user-equity-research-agent/"
                      "7da2f756-c32a-5a1d-a8f6-b341848f262a/scratchpad/dashboard.html")


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _p(row_or_str) -> dict:
    """Safely parse a properties JSON string or Row field."""
    if row_or_str is None:
        return {}
    s = row_or_str if isinstance(row_or_str, str) else str(row_or_str)
    try:
        return json.loads(s)
    except Exception:
        return {}


# ── Graph stats ──────────────────────────────────────────────────────────────

def _graph_stats(c) -> dict:
    nodes    = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edges    = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    episodes = c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    bs = dict(c.execute(
        "SELECT belief_state, COUNT(*) FROM nodes GROUP BY belief_state"
    ).fetchall())
    revisions = c.execute("SELECT COUNT(*) FROM belief_log").fetchone()[0]
    node_types = {r[0]: r[1] for r in c.execute(
        "SELECT node_type, COUNT(*) FROM nodes GROUP BY node_type ORDER BY COUNT(*) DESC"
    ).fetchall()}
    return {
        "nodes": nodes, "edges": edges, "episodes": episodes,
        "belief_states": {
            "confirmed": bs.get("confirmed", 0),
            "forming":   bs.get("forming", 0),
            "contested": bs.get("contested", 0),
            "retracted": bs.get("retracted", 0),
        },
        "belief_revisions": revisions,
        "node_types": node_types,
    }


# ── Screener (all 159 ConfluenceSignal tickers) ──────────────────────────────

def _screener(c) -> list:
    rows = c.execute("""
        SELECT n.ticker, n.confidence, n.belief_state,
               n.properties    AS sp,
               p.properties    AS pp,
               t.properties    AS tp,
               co.properties   AS cp
        FROM nodes n
        LEFT JOIN nodes p  ON p.ticker  = n.ticker AND p.node_type  = 'PriceSnapshot'
        LEFT JOIN nodes t  ON t.ticker  = n.ticker AND t.node_type  = 'TrendSummary'
        LEFT JOIN nodes co ON co.ticker = n.ticker AND co.node_type = 'Company'
        WHERE n.node_type = 'ConfluenceSignal'
        ORDER BY n.confidence DESC, CAST(json_extract(n.properties,'$.score') AS REAL) DESC
    """).fetchall()

    out = []
    for r in rows:
        sp  = _p(r["sp"])
        pp  = _p(r["pp"])
        tp  = _p(r["tp"])
        cp  = _p(r["cp"])

        signal_type = sp.get("type", "BUY_CONFLUENCE")
        rec = "BUY" if "BUY" in signal_type else ("AVOID" if "AVOID" in signal_type else "HOLD")

        close    = pp.get("close") or sp.get("close") or 0
        near_low = pp.get("near_52w_low_pct") or sp.get("near_low_pct") or 0
        streak   = tp.get("consistent_periods") or sp.get("streak") or 1
        score    = sp.get("score") or round(r["confidence"], 3)

        out.append({
            "ticker":       r["ticker"],
            "sector":       cp.get("sector", ""),
            "industry":     cp.get("industry", ""),
            "close":        round(float(close), 2) if close else 0,
            "near_low":     round(float(near_low), 2) if near_low else 0,
            "score":        round(float(score), 3),
            "conv":         round(r["confidence"], 3),
            "rec":          rec,
            "streak":       int(streak),
            "belief_state": r["belief_state"],
        })
    return out


# ── Analyst top-20 ───────────────────────────────────────────────────────────

def _analyst(c, screener: list) -> list:
    top20 = screener[:20]
    out = []
    for s in top20:
        ticker = s["ticker"]
        # Pull ManagementStatement for tone
        ms = c.execute("""
            SELECT properties FROM nodes
            WHERE node_type='ManagementStatement' AND ticker=?
            ORDER BY created_at DESC LIMIT 1
        """, (ticker,)).fetchone()
        ms_p = _p(ms["properties"]) if ms else {}
        tone = ms_p.get("tone", "neutral")
        mom  = round(s["score"] * 10, 1)   # proxy momentum score out of 10

        out.append({
            "ticker":  ticker,
            "sector":  s["sector"],
            "rec":     s["rec"],
            "conv":    s["conv"],
            "mom":     mom,
            "streak":  s["streak"],
            "tone":    tone,
            "rat":     f"{ticker}: conf={s['conv']:.3f}, near_low={s['near_low']}%, streak={s['streak']}Q",
        })
    return out


# ── Sectors ──────────────────────────────────────────────────────────────────

def _sectors(c, screener: list) -> list:
    from collections import defaultdict
    sec_map: dict = defaultdict(lambda: {"n": 0, "sigs": 0, "conf_sum": 0.0})

    # Company count per sector
    companies = c.execute(
        "SELECT properties FROM nodes WHERE node_type='Company'"
    ).fetchall()
    for row in companies:
        p = _p(row["properties"])
        s = p.get("sector", "Unknown")
        sec_map[s]["n"] += 1

    # Signal count + avg confidence per sector
    for sig in screener:
        s = sig["sector"] or "Unknown"
        sec_map[s]["sigs"]     += 1
        sec_map[s]["conf_sum"] += sig["conv"]

    # TrendSummary improving % per sector (approximate via ticker → sector)
    ticker_sector = {}
    for sig in screener:
        ticker_sector[sig["ticker"]] = sig["sector"]

    improving: dict = defaultdict(list)
    trends = c.execute(
        "SELECT ticker, properties FROM nodes WHERE node_type='TrendSummary'"
    ).fetchall()
    for row in trends:
        p = _p(row["properties"])
        ticker = row["ticker"]
        sec = ticker_sector.get(ticker, "Unknown")
        improving[sec].append(1 if p.get("direction") == "improving" else 0)

    out = []
    for s, v in sorted(sec_map.items(), key=lambda x: -x[1]["sigs"]):
        if v["n"] == 0:
            continue
        imp_list = improving.get(s, [])
        pct = round(sum(imp_list) / len(imp_list) * 100, 1) if imp_list else 50.0
        avg_conf = round(v["conf_sum"] / v["sigs"], 3) if v["sigs"] else 0
        out.append({
            "s":    s,
            "pct":  pct,
            "sigs": v["sigs"],
            "n":    v["n"],
            "conf": avg_conf,
        })
    return out


# ── Corp feed ────────────────────────────────────────────────────────────────

def _corp_feed(c) -> list:
    rows = c.execute("""
        SELECT ticker, properties, created_at FROM nodes
        WHERE node_type='CorporateEvent'
        ORDER BY created_at DESC LIMIT 25
    """).fetchall()
    out = []
    for r in rows:
        p = _p(r["properties"])
        out.append({
            "sym":  r["ticker"],
            "subj": p.get("subject") or p.get("desc") or p.get("headline") or "Corporate Event",
            "cat":  p.get("category") or p.get("type") or "Corporate_Action",
            "t":    r["created_at"][11:16] if r["created_at"] else "",
        })
    return out


# ── Belief log ───────────────────────────────────────────────────────────────

def _belief_log(c) -> list:
    rows = c.execute("""
        SELECT ticker, node_type, old_conf, new_conf, reason, created_at
        FROM belief_log ORDER BY created_at DESC LIMIT 15
    """).fetchall()
    out = []
    for r in rows:
        out.append({
            "dir":    "↑" if r["new_conf"] > r["old_conf"] else "↓",
            "ticker": r["ticker"],
            "type":   r["node_type"],
            "from":   round(r["old_conf"], 3),
            "to":     round(r["new_conf"], 3),
            "reason": r["reason"],
            "ts":     r["created_at"],
        })
    return out


# ── Confidence by node type ──────────────────────────────────────────────────

def _conf_by_type(c) -> list:
    rows = c.execute("""
        SELECT node_type,
               COUNT(*) as n,
               ROUND(AVG(confidence), 3) as conf,
               SUM(CASE WHEN belief_state='confirmed' THEN 1 ELSE 0 END) as confirmed
        FROM nodes
        WHERE node_type IN (
            'ConfluenceSignal','PriceSnapshot','MetricValue',
            'TrendSummary','ManagementStatement','CorporateEvent'
        )
        GROUP BY node_type ORDER BY conf DESC
    """).fetchall()
    return [{"type": r[0], "n": r[1], "conf": r[2], "confirmed": r[3]} for r in rows]


# ── Top signals ──────────────────────────────────────────────────────────────

def _top_signals(c) -> list:
    rows = c.execute("""
        SELECT ticker, confidence, belief_state
        FROM nodes WHERE node_type='ConfluenceSignal'
        ORDER BY confidence DESC LIMIT 10
    """).fetchall()
    return [{"ticker": r[0], "conf": round(r[1], 3), "state": r[2]} for r in rows]


# ── Ranked watchlist (top 20 CompositeRank nodes) ────────────────────────────

def _ranked(c) -> list:
    """Return top 20 tickers by composite_score from CompositeRank nodes."""
    rows = c.execute("""
        SELECT ticker, properties FROM nodes
        WHERE node_type='CompositeRank'
        ORDER BY CAST(json_extract(properties,'$.composite_score') AS REAL) DESC
        LIMIT 20
    """).fetchall()
    out = []
    for r in rows:
        p = _p(r["properties"])
        out.append({
            "ticker":          r["ticker"],
            "composite_score": p.get("composite_score"),
            "quality_tier":    p.get("quality_tier"),
            "rank":            p.get("rank"),
            "conf":            p.get("conf"),
            "quality_score":   p.get("quality_score"),
            "peer_verdict":    p.get("peer_verdict"),
            "pos_52w":         p.get("pos_52w"),
        })
    return out


# ── FII/DII (from FIIDIIDaily nodes if present) ──────────────────────────────

def _fii_dii(c) -> list:
    rows = c.execute("""
        SELECT properties FROM nodes WHERE node_type='FIIDIIDaily'
        ORDER BY created_at DESC LIMIT 30
    """).fetchall()
    out = []
    for r in rows:
        p = _p(r["properties"])
        out.append(p)
    return out


# ── Main export ──────────────────────────────────────────────────────────────

def export_dashboard_data(patch_html: bool = True, verbose: bool = True) -> dict:
    c = _conn()
    if verbose:
        print("[export] reading graph.db...")

    graph   = _graph_stats(c)
    scrn    = _screener(c)
    analyst = _analyst(c, scrn)
    sectors = _sectors(c, scrn)
    feed    = _corp_feed(c)
    blog    = _belief_log(c)
    cbt     = _conf_by_type(c)
    topsig  = _top_signals(c)
    fiidii  = _fii_dii(c)
    ranked  = _ranked(c)

    data = {
        "generated_at":  datetime.now().isoformat(),
        "graph":         graph,
        "screener":      scrn,
        "analyst":       analyst,
        "sectors":       sectors,
        "corp_feed":     feed,
        "belief_log":    blog,
        "conf_by_type":  cbt,
        "top_signals":   topsig,
        "fii_dii":       fiidii,
        "ranked":        ranked,
    }

    OUT_JSON.write_text(json.dumps(data, indent=2, default=str))
    if verbose:
        print(f"[export] wrote {OUT_JSON}")
        print(f"[export] screener={len(scrn)} tickers  sectors={len(sectors)}")
        print(f"[export] graph: {graph['nodes']} nodes, {graph['edges']} edges")
        bs = graph["belief_states"]
        print(f"[export] belief: confirmed={bs['confirmed']} forming={bs['forming']} "
              f"contested={bs['contested']} retracted={bs['retracted']}")

    if patch_html and DASHBOARD_HTML.exists():
        _patch_html(data, verbose=verbose)

    return data


# ── HTML patcher ─────────────────────────────────────────────────────────────

def _patch_html(data: dict, verbose: bool = True) -> None:
    html = DASHBOARD_HTML.read_text()
    g    = data["graph"]
    bs   = g["belief_states"]

    patches = [
        # NODE_TYPES
        (r'const NODE_TYPES = \{[^}]+\};',
         f"const NODE_TYPES = {json.dumps(g['node_types'])};"),

        # BELIEF_STATES
        (r'const BELIEF_STATES = \{[^}]+\};',
         f"const BELIEF_STATES = {json.dumps(bs)};"),

        # CONF_BY_TYPE
        (r'const CONF_BY_TYPE = \[.*?\];',
         f"const CONF_BY_TYPE = {json.dumps(data['conf_by_type'])};"),

        # BELIEF_LOG
        (r'const BELIEF_LOG = \[.*?\];',
         f"const BELIEF_LOG = {json.dumps(data['belief_log'])};"),

        # TOP_SIGNALS
        (r'const TOP_SIGNALS = \[.*?\];',
         f"const TOP_SIGNALS = {json.dumps(data['top_signals'])};"),

        # SCREENER_DATA  (may be large — DOTALL)
        (r'const SCREENER_DATA = \[.*?\];',
         f"const SCREENER_DATA = {json.dumps(data['screener'])};"),

        # Graph KPI: nodes
        (r'(<div[^>]*>)\s*4,345\s*(</div>\s*<div[^>]*>Nodes)',
         rf'\g<1>{g["nodes"]:,}\2'),

        # Graph KPI: edges
        (r'(<div[^>]*>)\s*108,085\s*(</div>\s*<div[^>]*>Edges)',
         rf'\g<1>{g["edges"]:,}\2'),

        # Graph KPI: confirmed beliefs
        (r'(<div[^>]*>)\s*318\s*(</div>\s*<div[^>]*>Confirmed beliefs)',
         rf'\g<1>{bs["confirmed"]}\2'),

        # Graph KPI: belief revisions
        (r'(<div[^>]*>)\s*362\s*(</div>\s*<div[^>]*>Belief revisions)',
         rf'\g<1>{g["belief_revisions"]}\2'),

        # Screener badge (tab label)
        (r'(Screener\s*<span class="nbadge">)\d+(</span>)',
         rf'\g<1>{len(data["screener"])}\2'),

        # Graph badge
        (r'(Graph\s*<span class="nbadge">)[^<]+(</span>)',
         rf'\g<1>{g["nodes"]:,}\2'),
    ]

    def _sub(pattern, repl_str, text):
        # Use lambda to avoid re interpreting backslashes in replacement
        return re.sub(pattern, lambda m: repl_str, text, flags=re.DOTALL)

    for pattern, replacement in patches:
        new_html = _sub(pattern, replacement, html)
        if new_html != html:
            html = new_html

    # Update SECTORS data
    sec_js = "const SECTORS = [\n"
    for s in data["sectors"][:22]:
        macro = s.get("macro", s["s"].split()[0])
        sec_js += (f"  {{s:{json.dumps(s['s'])}, pct:{s['pct']}, "
                   f"sigs:{s['sigs']}, n:{s['n']}, macro:{json.dumps(macro)}}},\n")
    sec_js += "];"
    new_html = _sub(r'const SECTORS = \[.*?\];', sec_js, html)
    if new_html != html:
        html = new_html

    DASHBOARD_HTML.write_text(html)
    if verbose:
        print(f"[export] patched {DASHBOARD_HTML}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Export dashboard data from graph.db")
    p.add_argument("--no-patch", action="store_true", help="Skip HTML patching")
    p.add_argument("--json-only", action="store_true", help="Only write JSON, no HTML patch")
    args = p.parse_args()
    patch = not (args.no_patch or args.json_only)
    export_dashboard_data(patch_html=patch, verbose=True)
