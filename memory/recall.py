"""
Retrieval layer — query everything we know about a company or the market.

All functions return a dict with:
  data   → raw structured data (for programmatic use)
  text   → formatted string ready to inject into a Claude session

Entry points:
  company_snapshot(symbol)         — full company brief across all sources
  get_announcements(symbol, ...)   — recent NSE filings
  get_insights(symbol)             — extracted knowledge base entries
  get_price_series(symbol, days)   — OHLCV from bhavcopy
  get_results(symbol, n)           — XBRL quarterly financials
  get_documents(symbol, doc_type)  — ingested doc index
  search_documents(symbol, query)  — keyword search across doc chunks
  get_macro(date)                  — macro snapshot
  search_facts(query, symbol)      — cross-source keyword search
"""
import csv
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

_REPO   = Path(__file__).parent.parent
_DB     = _REPO / "memory" / "research.db"
_STORE  = _REPO / "memory_store"
_BHAV   = _STORE / "bhavcopy"
_ANNS   = _STORE / "announcements"
_DOCS   = _STORE / "documents"
_RESULTS = _STORE / "results"


# ── helpers ────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB))
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_date(d) -> str:
    if isinstance(d, (date, datetime)):
        return d.isoformat()[:10]
    return str(d)[:10]


def _section(title: str, body: str) -> str:
    bar = "─" * len(title)
    return f"\n{title}\n{bar}\n{body}\n"


# ── announcements ──────────────────────────────────────────────────────────

def get_announcements(
    symbol: str,
    days: int = 30,
    category: str | None = None,
    limit: int = 20,
) -> dict:
    """
    Recent NSE announcements for a symbol.
    Searches processed SQLite records first; falls back to raw daily JSONs.
    """
    sym = symbol.upper()
    rows = []

    # 1. SQLite (processed, with impact)
    with _db() as conn:
        q = "SELECT title, category, content, impact, impact_reasoning, date FROM announcements WHERE ticker=? ORDER BY date DESC"
        params = [sym]
        if category:
            q = q.replace("WHERE ticker=?", "WHERE ticker=? AND category=?")
            params.append(category)
        for row in conn.execute(q, params).fetchmany(limit):
            rows.append(dict(row))

    # 2. Raw daily JSONs (last N days) — fill gaps
    if len(rows) < limit:
        cutoff = date.today() - timedelta(days=days)
        seen_titles = {r["title"] for r in rows}
        for ann_file in sorted(_ANNS.glob("*.json"), reverse=True):
            try:
                file_date = date.fromisoformat(ann_file.stem)
            except ValueError:
                continue
            if file_date < cutoff:
                break
            try:
                with open(ann_file) as f:
                    daily = json.load(f)
                anns = daily.get("announcements", {}).get(sym, [])
                for a in anns:
                    title = a.get("attchmntText", a.get("desc", ""))[:200]
                    if title not in seen_titles:
                        rows.append({
                            "title":    title,
                            "category": a.get("desc", ""),
                            "content":  a.get("attchmntText", ""),
                            "impact":   None,
                            "impact_reasoning": None,
                            "date":     ann_file.stem,
                        })
                        seen_titles.add(title)
            except Exception:
                continue
            if len(rows) >= limit:
                break

    rows = rows[:limit]

    # format
    if not rows:
        text = f"No announcements found for {sym} in the last {days} days."
    else:
        lines = []
        for r in rows:
            imp = f"  [{r['impact'].upper()}]" if r.get("impact") else ""
            lines.append(f"• {r['date']}  {r['category']:20s}{imp}\n  {r['title'][:120]}")
            if r.get("impact_reasoning"):
                lines.append(f"  → {r['impact_reasoning'][:100]}")
        text = "\n".join(lines)

    return {
        "data": rows,
        "text": _section(f"Announcements — {sym} (last {days}d)", text),
    }


# ── knowledge base / insights ──────────────────────────────────────────────

def get_insights(symbol: str) -> dict:
    sym = symbol.upper()
    rows = []
    with _db() as conn:
        for row in conn.execute(
            "SELECT insight, source, created_at FROM knowledge_base WHERE ticker=? ORDER BY created_at DESC",
            [sym],
        ).fetchall():
            rows.append(dict(row))

    if not rows:
        text = f"No stored insights for {sym}."
    else:
        lines = [f"• [{r['created_at'][:10]}] ({r['source']})\n  {r['insight']}" for r in rows]
        text = "\n".join(lines)

    return {
        "data": rows,
        "text": _section(f"Insights — {sym}", text),
    }


# ── price series ───────────────────────────────────────────────────────────

def get_price_series(symbol: str, days: int = 30) -> dict:
    sym = symbol.upper()
    cutoff = date.today() - timedelta(days=days)
    records = []

    for bhav_file in sorted(_BHAV.glob("*.csv"), reverse=True):
        try:
            file_date = date.fromisoformat(bhav_file.stem)
        except ValueError:
            continue
        if file_date < cutoff:
            break
        try:
            with open(bhav_file, newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("TckrSymb", "").upper() == sym:
                        records.append({
                            "date":   bhav_file.stem,
                            "open":   float(row.get("OpnPric") or 0),
                            "high":   float(row.get("HghPric") or 0),
                            "low":    float(row.get("LwPric") or 0),
                            "close":  float(row.get("ClsPric") or 0),
                            "volume": int(float(row.get("TtlTradgVol") or 0)),
                        })
                        break
        except Exception:
            continue

    records.sort(key=lambda r: r["date"])

    if not records:
        text = f"No price data found for {sym}."
    else:
        latest = records[-1]
        first  = records[0]
        ret    = ((latest["close"] / first["close"]) - 1) * 100 if first["close"] else 0
        high   = max(r["high"] for r in records)
        low    = min(r["low"] for r in records if r["low"] > 0)
        lines  = [
            f"Latest ({latest['date']}): ₹{latest['close']:,.2f}  |  Vol: {latest['volume']:,}",
            f"Period high: ₹{high:,.2f}  |  Low: ₹{low:,.2f}  |  Return: {ret:+.1f}%",
            "",
        ]
        lines += [
            f"  {r['date']}  O:{r['open']:8.2f}  H:{r['high']:8.2f}  L:{r['low']:8.2f}  C:{r['close']:8.2f}  V:{r['volume']:>12,}"
            for r in records[-10:]  # last 10 sessions
        ]
        text = "\n".join(lines)

    return {
        "data": records,
        "text": _section(f"Price Series — {sym} (last {days}d, showing last 10)", text),
    }


# ── XBRL results ───────────────────────────────────────────────────────────

def get_results(symbol: str, n: int = 4) -> dict:
    sym = symbol.upper()
    results_file = _RESULTS / f"{sym}.jsonl"

    quarters = []
    if results_file.exists():
        with open(results_file) as f:
            for line in f:
                try:
                    quarters.append(json.loads(line))
                except Exception:
                    pass
        quarters = quarters[-n:]

    if not quarters:
        text = f"No XBRL results stored for {sym}."
    else:
        lines = []
        for q in quarters:
            rev  = q.get("revenue_from_operations") or q.get("total_income")
            pat  = q.get("pat")
            eps  = q.get("eps_basic")
            period = q.get("period_end") or q.get("quarter", "")
            lines.append(
                f"  {period:12s}  Rev: {_fmt_cr(rev):>12s}  PAT: {_fmt_cr(pat):>12s}  EPS: {_fmt_num(eps):>8s}"
            )
        text = "\n".join(lines)

    return {
        "data": quarters,
        "text": _section(f"Quarterly Results — {sym} (last {n}Q)", text),
    }


def _fmt_cr(val) -> str:
    if val is None:
        return "N/A"
    try:
        v = float(val)
        if abs(v) >= 1e7:
            return f"₹{v/1e7:.0f}Cr"
        return f"₹{v:,.0f}"
    except Exception:
        return str(val)


def _fmt_num(val) -> str:
    if val is None:
        return "N/A"
    try:
        return f"{float(val):.2f}"
    except Exception:
        return str(val)


# ── documents ──────────────────────────────────────────────────────────────

def get_documents(symbol: str, doc_type: str | None = None) -> dict:
    from memory.doc_store import search_index
    sym = symbol.upper()
    records = search_index(symbol=sym, doc_type=doc_type)

    if not records:
        text = f"No documents stored for {sym}" + (f" ({doc_type})" if doc_type else "") + "."
    else:
        lines = []
        for r in records:
            lines.append(
                f"• [{r['doc_type']:15s}] {r.get('period') or '':12s} {r['source_name'][:50]}  "
                f"({r['char_count']:,} chars)  [{r['confidence']}]"
            )
        text = "\n".join(lines)

    return {
        "data": records,
        "text": _section(f"Documents — {sym}", text),
    }


def search_documents(symbol: str, query: str, top_k: int = 5) -> dict:
    """Keyword search across stored document chunks for a symbol."""
    from memory.doc_store import search_index, load_document

    sym = symbol.upper()
    records = search_index(symbol=sym)
    query_terms = re.compile(
        "|".join(re.escape(w) for w in query.lower().split() if len(w) > 2),
        re.I,
    )

    hits = []
    for rec in records:
        doc = load_document(rec["doc_id"])
        if not doc:
            continue
        for i, chunk in enumerate(doc.get("chunks", [])):
            score = len(query_terms.findall(chunk))
            if score > 0:
                hits.append({
                    "doc_id":    rec["doc_id"],
                    "source":    rec["source_name"],
                    "doc_type":  rec["doc_type"],
                    "period":    rec.get("period"),
                    "chunk_idx": i,
                    "score":     score,
                    "text":      chunk,
                })

    hits.sort(key=lambda h: h["score"], reverse=True)
    hits = hits[:top_k]

    if not hits:
        text = f"No document chunks matched '{query}' for {sym}."
    else:
        lines = []
        for h in hits:
            lines.append(f"[{h['doc_type']} / {h['source']} / chunk {h['chunk_idx']}  score={h['score']}]")
            lines.append(h["text"][:600])
            lines.append("")
        text = "\n".join(lines)

    return {
        "data": hits,
        "text": _section(f"Document Search — {sym} / '{query}'", text),
    }


# ── macro ──────────────────────────────────────────────────────────────────

def get_macro(target_date: str | None = None) -> dict:
    d = target_date or date.today().isoformat()
    with _db() as conn:
        row = conn.execute(
            "SELECT date, summary, data FROM macro_snapshots WHERE date<=? ORDER BY date DESC LIMIT 1",
            [d],
        ).fetchone()

    if not row:
        return {"data": {}, "text": _section("Macro Snapshot", "No macro data available.")}

    summary = row["summary"] or ""
    try:
        data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
    except Exception:
        data = {}

    return {
        "data": data,
        "text": _section(f"Macro Snapshot ({row['date']})", summary[:1500]),
    }


# ── cross-source search ────────────────────────────────────────────────────

def search_facts(query: str, symbol: str | None = None) -> dict:
    """
    Keyword search across: SQLite announcements, knowledge_base, document chunks.
    Returns unified ranked results.
    """
    pattern = re.compile(
        "|".join(re.escape(w) for w in query.lower().split() if len(w) > 2),
        re.I,
    )
    hits = []

    with _db() as conn:
        # announcements
        q = "SELECT ticker, title, category, impact, date FROM announcements"
        params = []
        if symbol:
            q += " WHERE ticker=?"
            params.append(symbol.upper())
        for row in conn.execute(q, params).fetchall():
            text = f"{row['title']} {row['category']}"
            score = len(pattern.findall(text))
            if score > 0:
                hits.append({"source": "announcement", "symbol": row["ticker"],
                             "date": row["date"], "score": score, "text": row["title"][:150]})

        # knowledge_base
        q2 = "SELECT ticker, insight, source, created_at FROM knowledge_base"
        params2 = []
        if symbol:
            q2 += " WHERE ticker=?"
            params2.append(symbol.upper())
        for row in conn.execute(q2, params2).fetchall():
            score = len(pattern.findall(row["insight"]))
            if score > 0:
                hits.append({"source": "insight", "symbol": row["ticker"],
                             "date": row["created_at"][:10], "score": score, "text": row["insight"][:150]})

    hits.sort(key=lambda h: h["score"], reverse=True)
    hits = hits[:20]

    if not hits:
        text = f"No results for '{query}'" + (f" ({symbol})" if symbol else "") + "."
    else:
        lines = [f"[{h['source']:12s}] {h['symbol']:12s} {h['date']:10s}  {h['text']}" for h in hits]
        text = "\n".join(lines)

    return {
        "data": hits,
        "text": _section(f"Search: '{query}'" + (f" — {symbol}" if symbol else ""), text),
    }


# ── company snapshot ──────────────────────────────────────────────────────

def company_snapshot(symbol: str, price_days: int = 30) -> dict:
    """
    Everything we know about a company — assembled into one readable block.
    This is the primary context-injection function.
    """
    sym = symbol.upper()

    prices   = get_price_series(sym, days=price_days)
    results  = get_results(sym, n=4)
    anns     = get_announcements(sym, days=60, limit=10)
    insights = get_insights(sym)
    docs     = get_documents(sym)

    full_text = (
        f"╔══ COMPANY BRIEF: {sym} ══╗\n"
        + prices["text"]
        + results["text"]
        + anns["text"]
        + insights["text"]
        + docs["text"]
        + f"\n╚══ END: {sym} ══╝\n"
    )

    return {
        "symbol":  sym,
        "data": {
            "prices":   prices["data"],
            "results":  results["data"],
            "announcements": anns["data"],
            "insights": insights["data"],
            "documents": docs["data"],
        },
        "text": full_text,
    }
