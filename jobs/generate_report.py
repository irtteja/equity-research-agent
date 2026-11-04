"""
Research report generator — produces 1-page per-ticker markdown reports from graph.db.

Pulls all data layers: price, trend, technicals, valuation, fundamentals,
management statements, bear case, alert events, global macro context.
Saves to reports/<TICKER>/YYYY-MM-DD.md and reports/TICKER/latest.md.

CLI:
  python -m jobs.generate_report --ticker RELIANCE
  python -m jobs.generate_report --all --limit 50
"""
import json
import sqlite3
from datetime import datetime, date
from pathlib import Path

REPO    = Path(__file__).parent.parent
DB_PATH = REPO / "memory_store" / "graph.db"
OUT_DIR = REPO / "reports" / "tickers"


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _get_node(ticker: str, node_type: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM nodes WHERE ticker=? AND node_type=? ORDER BY created_at DESC LIMIT 1",
            (ticker, node_type),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["properties"] = json.loads(d.get("properties") or "{}")
    except Exception:
        d["properties"] = {}
    return d


def _get_nodes(ticker: str, node_type: str, limit: int = 5) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM nodes WHERE ticker=? AND node_type=? ORDER BY created_at DESC LIMIT ?",
            (ticker, node_type, limit),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["properties"] = json.loads(d.get("properties") or "{}")
        except Exception:
            d["properties"] = {}
        out.append(d)
    return out


def _get_bear_case(ticker: str) -> dict | None:
    n = _get_node(ticker, "BearCase")
    if not n:
        return None
    p = n["properties"]
    return {
        "bear_score":    p.get("bear_score", 0),
        "net_conviction": p.get("net_conviction", 0),
        "verdict":       p.get("verdict", "?"),
        "arguments":     p.get("bear_arguments", []),
    }


def _get_alerts(ticker: str) -> list[dict]:
    nodes = _get_nodes(ticker, "AlertEvent", 5)
    out = []
    for n in nodes:
        p = n["properties"]
        out.append({
            "type":     p.get("alert_type", "?"),
            "severity": p.get("severity", "info"),
            "msg":      p.get("message", ""),
            "ts":       n.get("created_at", ""),
        })
    return out


def _get_macro() -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM nodes WHERE node_type='GlobalMacro' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {}
    d = dict(row)
    try:
        return json.loads(d.get("properties") or "{}")
    except Exception:
        return {}


def _get_conviction(ticker: str) -> float | None:
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT conviction_score FROM conviction_history WHERE ticker=? ORDER BY date DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        return float(row[0]) if row else None
    except Exception:
        return None


def _get_sentiment(ticker: str) -> dict | None:
    n = _get_node(ticker, "SentimentScore")
    if not n:
        return None
    p = n["properties"]
    return {
        "score":  p.get("sentiment_score", 0.5),
        "label":  p.get("label", "neutral"),
        "bull":   p.get("bull_count", 0),
        "bear":   p.get("bear_count", 0),
    }


def _fmt_pct(v, decimals=1) -> str:
    try:
        return f"{float(v):.{decimals}f}%"
    except Exception:
        return "—"


def _fmt_num(v, decimals=2) -> str:
    try:
        return f"{float(v):.{decimals}f}"
    except Exception:
        return "—"


def generate_report(ticker: str) -> str:
    """Generate a markdown research report for the given ticker. Returns markdown string."""
    today = date.today().isoformat()
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Data pull ──────────────────────────────────────────────────────────────
    company   = _get_node(ticker, "Company")
    price_sn  = _get_node(ticker, "PriceSnapshot")
    trend     = _get_node(ticker, "TrendSummary")
    tech      = _get_node(ticker, "TechnicalSignal")
    val       = _get_node(ticker, "ValuationScore")
    met       = _get_node(ticker, "MetricValue")
    signal    = _get_node(ticker, "ConfluenceSignal")
    stmts     = _get_nodes(ticker, "ManagementStatement", 3)
    bear      = _get_bear_case(ticker)
    alerts    = _get_alerts(ticker)
    macro     = _get_macro()
    conv      = _get_conviction(ticker)
    senti     = _get_sentiment(ticker)

    # ── Company header ─────────────────────────────────────────────────────────
    name   = (company or {}).get("name") or ticker
    sector = (company or {}).get("properties", {}).get("sector", "—")
    if sector == "—" and trend:
        sector = trend.get("properties", {}).get("sector", "—")

    # ── Price data ─────────────────────────────────────────────────────────────
    pp  = (price_sn or {}).get("properties", {})
    close     = pp.get("close",  pp.get("ltp", "—"))
    chg_pct   = pp.get("change_pct", pp.get("pct_change", "—"))
    hi52w     = pp.get("high_52w",   pp.get("yearHigh", "—"))
    lo52w     = pp.get("low_52w",    pp.get("yearLow",  "—"))
    volume    = pp.get("volume",     pp.get("tradedVolume", "—"))
    pos52w    = pp.get("pos52w",     None)

    # ── Signal ─────────────────────────────────────────────────────────────────
    sp     = (signal or {}).get("properties", {})
    sig_name  = sp.get("signal",    "—")
    conf      = signal.get("confidence", 0.5) if signal else 0.5
    bstate    = signal.get("belief_state", "forming") if signal else "forming"

    # ── Trend ──────────────────────────────────────────────────────────────────
    tp = (trend or {}).get("properties", {})
    trend_dir   = tp.get("trend",        "—")
    rsi_val     = tp.get("rsi",          "—")
    ema_signal  = tp.get("ema_signal",   "—")

    # ── Technicals ────────────────────────────────────────────────────────────
    techp = (tech or {}).get("properties", {})
    rsi_tech   = techp.get("rsi",         "—")
    macd_hist  = techp.get("macd_hist",   "—")
    ema_cross  = techp.get("ema_cross",   "—")
    tech_sig   = techp.get("signal",      "—")

    # ── Valuation ─────────────────────────────────────────────────────────────
    vp = (val or {}).get("properties", {})
    pe_val    = vp.get("pe",          "—")
    pe_avg    = vp.get("pe_3y_avg",   "—")
    pb_val    = vp.get("pb",          "—")
    ev_ebitda = vp.get("ev_ebitda",   "—")
    val_verd  = vp.get("verdict",     "—")

    # ── Fundamentals ──────────────────────────────────────────────────────────
    mp = (met or {}).get("properties", {})
    rev_growth   = mp.get("revenue_growth_yoy", mp.get("revenue_growth", "—"))
    pat_growth   = mp.get("pat_growth_yoy",     mp.get("pat_growth", "—"))
    roe          = mp.get("roe",            "—")
    debt_equity  = mp.get("debt_equity",   "—")
    promoter_pct = mp.get("promoter_holding", "—")

    # ── Sentiment & conviction ────────────────────────────────────────────────
    sent_label = (senti or {}).get("label", "neutral")
    sent_score = (senti or {}).get("score", 0.5)
    conv_str   = f"{conv:.2f}" if conv else "—"

    # ── Macro ─────────────────────────────────────────────────────────────────
    macro_sig    = macro.get("macro_signal",  "—")
    macro_score  = macro.get("macro_score",   "—")

    # ── Build markdown ─────────────────────────────────────────────────────────
    # Signal color indicator
    sig_icon = "🟢" if sig_name == "BUY" else ("🔴" if sig_name == "AVOID" else "🟡")
    state_icon = "✅" if bstate == "confirmed" else ("⚠️" if bstate == "contested" else "🔵")

    lines = [
        f"# {ticker} — {name}",
        f"*{sector} | Generated {ts}*",
        "",
        "---",
        "",
        f"## Signal: {sig_icon} {sig_name}  {state_icon} {bstate.upper()}",
        f"**Confidence:** {conf:.1%}  |  **Conviction:** {conv_str}  |  **Sentiment:** {sent_label} ({sent_score:.2f})",
        "",
        "---",
        "",
        "## Price",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Close  | ₹{close} |",
        f"| Change | {_fmt_pct(chg_pct)} |",
        f"| 52W High | ₹{hi52w} |",
        f"| 52W Low  | ₹{lo52w} |",
        f"| 52W Position | {_fmt_pct(pos52w) if pos52w else '—'} |",
        f"| Volume | {volume} |",
        "",
        "## Technicals",
        f"| Indicator | Value |",
        f"|-----------|-------|",
        f"| RSI (14)  | {rsi_tech or rsi_val} |",
        f"| MACD Hist | {macd_hist} |",
        f"| EMA Cross | {ema_cross} |",
        f"| Trend     | {trend_dir} |",
        f"| Signal    | {tech_sig} |",
        "",
        "## Valuation",
        f"| Metric     | Value | Comment |",
        f"|------------|-------|---------|",
        f"| P/E        | {pe_val} | 3Y avg: {pe_avg} |",
        f"| P/B        | {pb_val} | |",
        f"| EV/EBITDA  | {ev_ebitda} | |",
        f"| Verdict    | **{val_verd}** | |",
        "",
        "## Fundamentals",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Revenue Growth YoY | {_fmt_pct(rev_growth)} |",
        f"| PAT Growth YoY     | {_fmt_pct(pat_growth)} |",
        f"| ROE                | {_fmt_pct(roe)} |",
        f"| Debt/Equity        | {debt_equity} |",
        f"| Promoter Holding   | {_fmt_pct(promoter_pct)} |",
        "",
        "## Macro Context",
        f"**Signal:** {macro_sig}  |  **Score:** {macro_score}",
    ]

    # Management statements
    if stmts:
        lines += ["", "## Management Statements"]
        for s in stmts:
            sp2 = s.get("properties", {})
            text = sp2.get("text", sp2.get("statement", ""))
            src  = sp2.get("source", "")
            if text:
                lines.append(f"- {text[:200]} *(source: {src})*" if src else f"- {text[:200]}")

    # Bear case
    if bear:
        lines += ["", "## Bear Case"]
        lines.append(f"**Bear Score:** {bear['bear_score']:.2f}  |  **Net Conviction:** {bear['net_conviction']:.2f}  |  **Verdict:** {bear['verdict']}")
        for arg in (bear.get("arguments") or []):
            lines.append(f"- {arg}")

    # Active alerts
    if alerts:
        lines += ["", "## Active Alerts"]
        for a in alerts:
            icon = "🚨" if a["severity"] == "critical" else ("⚠️" if a["severity"] == "warning" else "ℹ️")
            lines.append(f"{icon} **{a['type']}** — {a['msg']}")

    lines += ["", "---", f"*Report generated by equity-research-agent on {today}.*"]

    return "\n".join(lines)


def save_report(ticker: str) -> Path:
    """Generate and save report. Returns path to saved file."""
    md   = generate_report(ticker)
    today = date.today().isoformat()

    ticker_dir = OUT_DIR / ticker.upper()
    ticker_dir.mkdir(parents=True, exist_ok=True)

    dated = ticker_dir / f"{today}.md"
    dated.write_text(md)

    latest = ticker_dir / "latest.md"
    latest.write_text(md)

    return dated


def run_all_reports(limit: int = 50) -> int:
    """Generate reports for all ConfluenceSignal tickers with BUY signal."""
    with _conn() as c:
        rows = c.execute(
            """SELECT DISTINCT ticker FROM nodes
               WHERE node_type='ConfluenceSignal'
               AND json_extract(properties,'$.signal')='BUY'
               ORDER BY confidence DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    tickers = [r[0] for r in rows]

    saved = 0
    for t in tickers:
        try:
            path = save_report(t)
            saved += 1
        except Exception as e:
            print(f"  [report] SKIP {t}: {e}")

    return saved


if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))
    p = argparse.ArgumentParser(description="Generate per-ticker research reports")
    p.add_argument("--ticker", help="Single ticker to report")
    p.add_argument("--all",    action="store_true", help="Generate for all BUY signals")
    p.add_argument("--limit",  type=int, default=50)
    args = p.parse_args()

    if args.ticker:
        path = save_report(args.ticker.upper())
        print(f"Report saved: {path}")
    elif args.all:
        n = run_all_reports(limit=args.limit)
        print(f"Generated {n} reports under reports/tickers/")
    else:
        p.print_help()
