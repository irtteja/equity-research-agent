"""
HTML research report generator — produces a nicely formatted browser-viewable report
per ticker, pulling the same data as generate_report.py but rendered as HTML.

Features:
  - Inline CSS only — no external dependencies
  - Dark/light theme aware (prefers-color-scheme media query)
  - Signal color badges, metric tables, risk register
  - Saves to reports/tickers/<TICKER>/latest.html

CLI:
  python -m jobs.generate_report_html --ticker RELIANCE
  python -m jobs.generate_report_html --all --limit 50
"""
import json
import sqlite3
from datetime import datetime, date
from pathlib import Path

# Import all data helpers from the main generator to avoid duplication
import sys
REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from jobs.generate_report import (
    _get_node, _get_nodes, _get_bear_case, _get_alerts, _get_macro,
    _get_conviction, _get_conviction_trend, _get_sentiment,
    _fmt_pct, _fmt_num, _fmt_arg, _build_thesis, _conn,
)

OUT_DIR = REPO / "reports" / "tickers"

# ── HTML boilerplate ──────────────────────────────────────────────────────────

CSS = """
:root {
  --bg:       #ffffff;
  --bg2:      #f4f6f9;
  --bg3:      #e8ecf2;
  --text:     #1a1d23;
  --text2:    #4a5568;
  --border:   #d1d9e0;
  --accent:   #2563eb;
  --buy:      #16a34a;
  --avoid:    #dc2626;
  --hold:     #d97706;
  --info:     #0369a1;
  --tag-bg:   #eff6ff;
  --tag-txt:  #1d4ed8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:      #111827;
    --bg2:     #1f2937;
    --bg3:     #374151;
    --text:    #f9fafb;
    --text2:   #9ca3af;
    --border:  #374151;
    --accent:  #60a5fa;
    --buy:     #4ade80;
    --avoid:   #f87171;
    --hold:    #fbbf24;
    --info:    #38bdf8;
    --tag-bg:  #1e3a5f;
    --tag-txt: #93c5fd;
  }
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text); font-size: 15px; line-height: 1.6;
  padding: 0 16px 48px;
}
.page { max-width: 860px; margin: 0 auto; }
header { padding: 32px 0 24px; border-bottom: 2px solid var(--border); margin-bottom: 28px; }
header h1 { font-size: 26px; font-weight: 700; letter-spacing: -0.5px; }
header .meta { color: var(--text2); font-size: 13px; margin-top: 4px; }
.signal-badge {
  display: inline-block; padding: 3px 12px; border-radius: 999px;
  font-weight: 700; font-size: 13px; letter-spacing: 0.5px; margin-left: 10px;
}
.signal-BUY   { background: var(--buy);   color: #fff; }
.signal-AVOID { background: var(--avoid); color: #fff; }
.signal-HOLD  { background: var(--hold);  color: #fff; }
.section { margin: 28px 0; }
.section h2 {
  font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px;
  color: var(--text2); border-bottom: 1px solid var(--border); padding-bottom: 6px; margin-bottom: 14px;
}
.thesis {
  background: var(--bg2); border-left: 4px solid var(--accent);
  padding: 14px 18px; border-radius: 6px; font-size: 15px; line-height: 1.7;
  color: var(--text);
}
.exec-bullets { list-style: none; }
.exec-bullets li {
  padding: 8px 14px; border-radius: 6px; margin-bottom: 6px; background: var(--bg2);
  font-size: 14px;
}
.exec-bullets li strong { color: var(--accent); }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { padding: 9px 12px; text-align: left; border-bottom: 1px solid var(--border); }
th { background: var(--bg2); font-weight: 600; color: var(--text2); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
tr:last-child td { border-bottom: none; }
.table-wrap { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.metric-row td:first-child { color: var(--text2); font-size: 13px; }
.metric-row td:last-child  { font-weight: 600; }
.verdict-cheap    { color: var(--buy); }
.verdict-fair     { color: var(--hold); }
.verdict-expensive{ color: var(--avoid); }
.verdict-BUY      { color: var(--buy); font-weight: 700; }
.verdict-HOLD     { color: var(--hold); font-weight: 700; }
.verdict-AVOID    { color: var(--avoid); font-weight: 700; }
.risk-list { list-style: none; }
.risk-list li {
  padding: 10px 14px; border-left: 3px solid var(--avoid);
  background: var(--bg2); border-radius: 0 6px 6px 0; margin-bottom: 8px;
  font-size: 14px;
}
.alert-list { list-style: none; }
.alert-list li { padding: 8px 12px; border-radius: 6px; background: var(--bg3); margin-bottom: 6px; font-size: 13px; }
.alert-critical { border-left: 3px solid var(--avoid); }
.alert-warning  { border-left: 3px solid var(--hold); }
.alert-info     { border-left: 3px solid var(--info); }
.tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; background: var(--tag-bg); color: var(--tag-txt); margin-right: 4px; }
.note { color: var(--text2); font-style: italic; font-size: 13px; padding: 10px 0; }
.footer { margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--border); color: var(--text2); font-size: 12px; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
@media (max-width: 600px) { .two-col { grid-template-columns: 1fr; } }
.card { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
.card .kpi-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--text2); }
.card .kpi-value { font-size: 22px; font-weight: 700; margin-top: 2px; }
.card .kpi-sub   { font-size: 12px; color: var(--text2); margin-top: 2px; }
.trend-rising  { color: var(--buy); }
.trend-falling { color: var(--avoid); }
.trend-stable  { color: var(--text2); }
"""

HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
<div class="page">
"""

HTML_FOOT = """
</div>
</body>
</html>"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _h(text: str) -> str:
    """HTML-escape text."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _table(headers: list[str], rows: list[list], row_class: str = "") -> str:
    th_html = "".join(f"<th>{_h(h)}</th>" for h in headers)
    rows_html = ""
    for row in rows:
        tds = "".join(f"<td>{cell}</td>" for cell in row)
        rows_html += f'<tr class="{row_class}">{tds}</tr>\n'
    return f'<div class="table-wrap"><table><thead><tr>{th_html}</tr></thead><tbody>{rows_html}</tbody></table></div>'


def _section(title: str, body: str) -> str:
    return f'<div class="section"><h2>{_h(title)}</h2>{body}</div>'


# ── Main HTML generator ───────────────────────────────────────────────────────

def generate_report_html(ticker: str) -> str:
    """Generate a full HTML research report for the given ticker. Returns HTML string."""
    today = date.today().isoformat()
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Data pull (identical to generate_report.py) ───────────────────────────
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
    rs_node   = _get_node(ticker, "RelativeStrength")
    concall   = _get_node(ticker, "ConcallInsight")
    peer_cmp  = _get_node(ticker, "PeerComparison")
    biz_qual  = _get_node(ticker, "BusinessQuality")

    # ── Derived fields ────────────────────────────────────────────────────────
    comp_props = (company or {}).get("properties", {})
    name   = (company or {}).get("name") or ticker
    sector = comp_props.get("sector") or (trend or {}).get("properties", {}).get("sector", "—") or "—"
    industry = comp_props.get("industry", "")

    techp      = (tech or {}).get("properties", {})
    pp         = (price_sn or {}).get("properties", {})
    close      = pp.get("close",    pp.get("ltp", "—"))
    chg_pct    = pp.get("change_pct", pp.get("pct_change", "—"))
    hi52w      = pp.get("high_52w",   pp.get("yearHigh", techp.get("high52w", "—")))
    lo52w      = pp.get("low_52w",    pp.get("yearLow",  techp.get("low52w",  "—")))
    volume     = pp.get("volume",     pp.get("tradedVolume", "—"))

    sp         = (signal or {}).get("properties", {})
    raw_type   = sp.get("type", "")
    sig_name   = "BUY" if "BUY" in raw_type else ("AVOID" if "AVOID" in raw_type else sp.get("signal", "HOLD"))
    conf       = signal.get("confidence", 0.5) if signal else 0.5
    bstate     = signal.get("belief_state", "forming") if signal else "forming"

    vp         = (val or {}).get("properties", {})
    pe_val     = vp.get("pe", "—")
    pat_growth = vp.get("pat_growth_pct", "—")
    rev_growth = vp.get("rev_growth_pct", "—")
    ebit_mgn   = vp.get("ebit_margin_pct", "—")
    fcf_proxy  = vp.get("fcf_proxy_cr", "—")
    debt_eq    = vp.get("debt_equity", "—")
    int_cov    = vp.get("int_coverage", "—")
    val_verd   = vp.get("verdict", "—")
    val_score  = vp.get("valuation_score", "—")

    rsi_tech   = techp.get("rsi", (trend or {}).get("properties", {}).get("rsi", "—"))
    macd_hist  = techp.get("macd_hist", "—")
    ema_cross  = techp.get("above_ema20", "—")
    tech_sig   = techp.get("signal", "—")
    pos_52w    = techp.get("pos_52w", None)
    vol_ratio  = techp.get("vol_ratio", None)

    conv_str   = f"{conv:.2f}" if conv else "—"
    conv_trend = _get_conviction_trend(ticker)
    sent_label = (senti or {}).get("label", "neutral")
    sent_score = (senti or {}).get("score", 0.5)

    bear_score     = (bear or {}).get("bear_score", 0)
    bear_args      = (bear or {}).get("arguments", [])
    net_conviction = (bear or {}).get("net_conviction", 0)

    rsp      = (rs_node or {}).get("properties", {})
    rs_label = rsp.get("label", "—")
    rs_30    = rsp.get("rs_30", "—")

    ccp      = (concall or {}).get("properties", {})
    pcp      = (peer_cmp or {}).get("properties", {})
    bqp      = (biz_qual or {}).get("properties", {})

    macro_sig   = macro.get("macro_signal", "—")
    macro_score = macro.get("macro_score", "—")

    thesis = _build_thesis(ticker, signal, trend, tech, bear, senti, val)

    # ── Header ────────────────────────────────────────────────────────────────
    sig_cls  = f"signal-{sig_name}"
    state_lbl = bstate.upper()
    meta_line = f"{sector}{' · ' + industry if industry else ''} · {today}"

    html = HTML_HEAD.format(title=f"{ticker} — {_h(name)} Research Report", css=CSS)
    html += f"""
<header>
  <h1>{_h(ticker)} — {_h(name)} <span class="signal-badge {sig_cls}">{_h(sig_name)}</span></h1>
  <div class="meta">{_h(meta_line)}</div>
</header>
"""

    # ── KPI row ───────────────────────────────────────────────────────────────
    trend_cls = f"trend-{conv_trend}"
    trend_arr = "↗" if conv_trend == "rising" else ("↘" if conv_trend == "falling" else "→")
    pos_pct   = f"{float(pos_52w)*100:.0f}%" if pos_52w is not None else "—"
    chg_disp  = _fmt_pct(chg_pct)

    html += f"""
<div class="two-col" style="margin:0 0 28px">
  <div class="card">
    <div class="kpi-label">Close Price</div>
    <div class="kpi-value">₹{_h(str(close))}</div>
    <div class="kpi-sub">Change {_h(chg_disp)} · 52W pos {_h(pos_pct)}</div>
  </div>
  <div class="card">
    <div class="kpi-label">Confidence · Conviction</div>
    <div class="kpi-value">{conf:.0%} · {_h(conv_str)}</div>
    <div class="kpi-sub">{_h(state_lbl)} state · <span class="{trend_cls}">{trend_arr} {_h(conv_trend)}</span></div>
  </div>
</div>
"""

    # ── INVESTMENT THESIS ──────────────────────────────────────────────────────
    html += _section("Investment Thesis", f'<p class="thesis">{_h(thesis)}</p>')

    # ── EXECUTIVE SUMMARY ──────────────────────────────────────────────────────
    key_driver = sp.get("reason", "Signal forming — awaiting further corroboration.")
    top_bear_txt = ""
    if bear_score > 0.1 and bear_args:
        top_bear_txt = _fmt_arg(bear_args[0])
    watch_line = top_bear_txt if top_bear_txt else f"No material bear arguments (score {bear_score:.2f})"

    exec_body = f"""<ul class="exec-bullets">
  <li><strong>Signal:</strong> {_h(sig_name)} · {conf:.0%} confidence · {_h(state_lbl)} state</li>
  <li><strong>Key driver:</strong> {_h(key_driver)}</li>
  <li><strong>Watch:</strong> {_h(watch_line)}</li>
</ul>"""
    html += _section("Executive Summary", exec_body)

    # ── SIGNAL & CONVICTION ───────────────────────────────────────────────────
    sig_rows = [
        ["Signal", f'<span class="{sig_cls}" style="font-weight:700">{_h(sig_name)}</span>'],
        ["Confidence", f"{conf:.1%}"],
        ["Belief State", _h(state_lbl)],
        ["Conviction Score", _h(conv_str)],
        ["Conviction Trend (7d)", f'<span class="{trend_cls}">{trend_arr} {_h(conv_trend.upper())}</span>'],
        ["Sentiment", _h(f"{sent_label} ({sent_score:.2f})")],
        ["Net Conviction", _fmt_num(net_conviction)],
    ]
    html += _section("Signal & Conviction", _table(["Metric", "Value"], sig_rows, "metric-row"))

    # ── PRICE & TECHNICALS ────────────────────────────────────────────────────
    ema_txt = "Above EMA20" if ema_cross is True else ("Below EMA20" if ema_cross is False else str(ema_cross))
    vol_txt = f"{float(vol_ratio):.2f}x avg" if vol_ratio is not None else "—"
    price_rows = [
        ["Close",             f"₹{_h(str(close))}"],
        ["Change",            _h(_fmt_pct(chg_pct))],
        ["52W High",          f"₹{_h(str(hi52w))}"],
        ["52W Low",           f"₹{_h(str(lo52w))}"],
        ["52W Position",      _h(f"{pos_pct} of range")],
        ["Relative Strength", _h(f"{rs_label} (rs_30: {_fmt_num(rs_30, 1)})")],
        ["Volume vs Avg",     _h(vol_txt)],
        ["RSI (14)",          _h(_fmt_num(rsi_tech, 1))],
        ["MACD Hist",         _h(_fmt_num(macd_hist, 2))],
        ["EMA Status",        _h(ema_txt)],
        ["Technical Signal",  f"<strong>{_h(str(tech_sig))}</strong>"],
    ]
    html += _section("Price & Technicals", _table(["Metric", "Value"], price_rows, "metric-row"))

    # ── VALUATION ─────────────────────────────────────────────────────────────
    verd_cls = f"verdict-{val_verd}" if val_verd in ("cheap", "fair", "expensive") else ""
    val_rows = [
        ["P/E",               _h(_fmt_num(pe_val, 1) + "x")],
        ["Valuation Score",   _h(_fmt_num(val_score, 2) + " (0=cheap → 1=expensive)")],
        ["Verdict",           f'<span class="{verd_cls}"><strong>{_h(str(val_verd).upper())}</strong></span>'],
        ["PAT Growth YoY",    _h(_fmt_pct(pat_growth))],
        ["Rev Growth YoY",    _h(_fmt_pct(rev_growth))],
        ["EBIT Margin",       _h(_fmt_pct(ebit_mgn))],
        ["FCF Proxy (₹Cr)",   _h(_fmt_num(fcf_proxy, 0))],
        ["Debt/Equity",       _h(_fmt_num(debt_eq, 2))],
        ["Interest Coverage", _h(_fmt_num(int_cov, 1) + "x")],
    ]
    if pcp:
        val_rows.append(["Peer P/E Rank", _h(str(pcp.get("pe_rank", "—")))])
    html += _section("Valuation", _table(["Metric", "Value"], val_rows, "metric-row"))

    # ── BUSINESS QUALITY ──────────────────────────────────────────────────────
    if biz_qual and bqp:
        bq_rows = [
            ["ROCE Trend",          _h(bqp.get("roce_trend", "—"))],
            ["FCF Quality",         _h(bqp.get("fcf_quality", "—"))],
            ["Margin Trend",        _h(bqp.get("margin_trend", "—"))],
            ["Capital Allocation",  _h(bqp.get("capital_allocation", "—"))],
            ["Overall Quality",     f"<strong>{_h(bqp.get('overall_quality', '—'))}</strong>"],
        ]
        bq_body = _table(["Dimension", "Assessment"], bq_rows, "metric-row")
    else:
        # Proxy from ValuationScore
        fcf_cr   = vp.get("fcf_proxy_cr", 0)
        int_cov_v= vp.get("int_coverage", 0)
        try:
            fcf_q = "positive" if float(fcf_cr) > 0 else "negative"
        except Exception:
            fcf_q = "—"
        try:
            lev = "low" if float(int_cov_v) > 5 else ("moderate" if float(int_cov_v) > 2 else "stressed")
        except Exception:
            lev = "—"
        try:
            mgn_q = "expanding" if float(ebit_mgn) > 10 else ("stable" if float(ebit_mgn) > 5 else "thin")
        except Exception:
            mgn_q = "—"
        bq_rows = [
            ["FCF Generation",    _h(f"{fcf_q} (₹{_fmt_num(fcf_cr, 0)} Cr TTM)")],
            ["Leverage",          _h(f"{lev} (interest coverage {_fmt_num(int_cov_v, 1)}x)")],
            ["Margin Profile",    _h(f"{mgn_q} (EBIT margin {_fmt_pct(ebit_mgn)})")],
        ]
        bq_body = '<p class="note">BusinessQuality node not yet built — proxy from ValuationScore:</p>'
        bq_body += _table(["Dimension", "Proxy Assessment"], bq_rows, "metric-row")
    html += _section("Business Quality", bq_body)

    # ── MANAGEMENT GUIDANCE ───────────────────────────────────────────────────
    if concall and ccp:
        guidance_raw = ccp.get("guidance", [])
        if isinstance(guidance_raw, list):
            guidance = "; ".join(guidance_raw) if guidance_raw else None
        else:
            guidance = str(guidance_raw) or None

        tone      = ccp.get("tone", "—")
        themes    = ccp.get("key_themes", [])
        numbers   = ccp.get("specific_numbers", ccp.get("key_numbers", []))
        pos_sigs  = ccp.get("positive_signals", [])
        stmt_cnt  = ccp.get("statement_count", "?")

        mg_parts = [f'<p><strong>Tone:</strong> {_h(tone)} &nbsp;|&nbsp; <strong>Sources:</strong> {_h(str(stmt_cnt))} management statements</p>']
        if guidance:
            mg_parts.append(f'<p style="margin-top:10px"><strong>Guidance:</strong> {_h(guidance)}</p>')
        if themes:
            theme_tags = " ".join(f'<span class="tag">{_h(t)}</span>' for t in themes[:5])
            mg_parts.append(f'<p style="margin-top:10px"><strong>Key Themes:</strong> {theme_tags}</p>')
        if pos_sigs:
            quotes = "".join(f'<li style="margin-bottom:6px;font-size:13px;color:var(--text2)">{_h(str(s)[:250])}</li>' for s in pos_sigs[:2])
            mg_parts.append(f'<p style="margin-top:12px;font-weight:600;font-size:13px">Notable statements:</p><ul style="list-style:disc;padding-left:20px;margin-top:4px">{quotes}</ul>')
        if numbers:
            num_tags = " ".join(f'<span class="tag">{_h(str(n))}</span>' for n in numbers[:4])
            mg_parts.append(f'<p style="margin-top:10px"><strong>Numbers mentioned:</strong> {num_tags}</p>')
        mg_body = "\n".join(mg_parts)
    elif stmts:
        stmts_html = ""
        for s in stmts:
            sp2  = s.get("properties", {})
            text = sp2.get("text", sp2.get("statement", ""))
            src  = sp2.get("source", "")
            if text:
                stmts_html += f'<p style="margin-bottom:8px;font-size:13px;color:var(--text2)">{_h(text[:300])}'
                if src:
                    stmts_html += f' <em>(source: {_h(src)})</em>'
                stmts_html += '</p>'
        mg_body = '<p class="note">ConcallInsight node not yet built — recent management statements:</p>' + stmts_html
    else:
        mg_body = '<p class="note">No management guidance data available yet.</p>'
    html += _section("Management Guidance", mg_body)

    # ── RISK REGISTER ─────────────────────────────────────────────────────────
    risk_html = ""
    if bear_args:
        items = ""
        for arg in bear_args:
            txt = _fmt_arg(arg)
            if txt:
                items += f"<li>{_h(txt)}</li>\n"
        if items:
            risk_html += f'<ul class="risk-list">{items}</ul>'
    if not risk_html:
        risk_html = '<p class="note">No material bear arguments identified.</p>'

    concall_risks = ccp.get("risks_flagged", ccp.get("risks", []))
    if concall and concall_risks:
        risk_html += '<p style="margin-top:14px;font-weight:600;font-size:13px">Additional risks from concall:</p>'
        risk_html += '<ul class="risk-list">'
        for r in concall_risks[:3]:
            risk_html += f"<li>{_h(str(r))}</li>"
        risk_html += "</ul>"

    if alerts:
        risk_html += '<p style="margin-top:14px;font-weight:600;font-size:13px">Active alerts:</p><ul class="alert-list">'
        for a in alerts:
            sev_cls = f"alert-{a['severity']}"
            risk_html += f'<li class="{sev_cls}"><strong>{_h(a["type"])}</strong> — {_h(a["msg"])}</li>'
        risk_html += "</ul>"
    html += _section("Risk Register", risk_html)

    # ── PEER CONTEXT ──────────────────────────────────────────────────────────
    if peer_cmp and pcp:
        peer_verdict = pcp.get("peer_verdict", "—")
        peer_rows = [
            ["P/E Rank",          _h(str(pcp.get("pe_rank", "—")))],
            ["Revenue Growth",    _h(str(pcp.get("growth_rank", "—")))],
            ["EBIT Margin Rank",  _h(str(pcp.get("margin_rank", "—")))],
            ["Overall Verdict",   f"<strong>{_h(peer_verdict)}</strong>"],
        ]
        peer_body = f'<p style="margin-bottom:12px;font-size:14px"><strong>vs Sector Peers:</strong> {_h(peer_verdict)}</p>'
        peer_body += _table(["Dimension", "Peer Rank"], peer_rows, "metric-row")
    else:
        peer_body = f'<p class="note">PeerComparison node not yet built.</p><p style="margin-top:8px;font-size:14px">Sector: <strong>{_h(sector)}</strong>{" · " + _h(industry) if industry else ""}</p>'
    html += _section("Peer Context", peer_body)

    # ── MACRO CONTEXT ─────────────────────────────────────────────────────────
    sector_macro = {
        "Oil Gas & Consumable Fuels": "sensitive to crude oil prices and USD/INR",
        "Information Technology":     "sensitive to USD/INR (revenue in USD) and US economic conditions",
        "Banking":                    "sensitive to RBI rate cycle and credit growth",
        "Financial Services":         "sensitive to interest rates and credit cycle",
        "Automobiles":                "sensitive to commodity costs and consumer sentiment",
        "Pharmaceuticals":            "sensitive to US FDA approvals and USD/INR",
        "Fast Moving Consumer Goods": "sensitive to rural demand and input cost inflation",
        "Metals & Mining":            "sensitive to global commodity prices and China demand",
        "Capital Goods":              "linked to government capex cycle and domestic investment",
        "Realty":                     "sensitive to interest rates, home loan availability",
    }
    macro_sensitivity = ""
    for key, note in sector_macro.items():
        if key.lower() in (sector or "").lower():
            macro_sensitivity = note
            break

    macro_body = f'<p><strong>Global Signal:</strong> {_h(str(macro_sig))} &nbsp;|&nbsp; <strong>Score:</strong> {_h(str(macro_score))}</p>'
    if macro_sensitivity:
        macro_body += f'<p style="margin-top:10px;font-size:13px"><strong>Sector sensitivity:</strong> {_h(sector)} is {_h(macro_sensitivity)}.</p>'
    html += _section("Macro Context", macro_body)

    # ── FUNDAMENTALS ──────────────────────────────────────────────────────────
    mp = (met or {}).get("properties", {})
    roe          = mp.get("roe", "—")
    promoter_pct = mp.get("promoter_holding", "—")

    fund_rows = [
        ["Revenue Growth YoY",  _h(_fmt_pct(rev_growth))],
        ["PAT Growth YoY",      _h(_fmt_pct(pat_growth))],
        ["EBIT Margin",         _h(_fmt_pct(ebit_mgn))],
        ["Debt/Equity",         _h(_fmt_num(debt_eq, 2))],
        ["Interest Coverage",   _h(_fmt_num(int_cov, 1) + "x")],
        ["FCF Proxy (₹Cr)",     _h(_fmt_num(fcf_proxy, 0))],
        ["ROE",                 _h(_fmt_pct(roe))],
        ["Promoter Holding",    _h(_fmt_pct(promoter_pct))],
    ]
    html += _section("Fundamentals", _table(["Metric", "Value"], fund_rows, "metric-row"))

    # ── Footer ────────────────────────────────────────────────────────────────
    html += f"""
<div class="footer">
  Confidence: {conf:.0%} &nbsp;|&nbsp; Generated: {_h(ts)} &nbsp;|&nbsp; Next review: +30d
  &nbsp;|&nbsp; <em>equity-research-agent</em>
</div>
"""

    html += HTML_FOOT
    return html


def save_report_html(ticker: str) -> Path:
    """Generate and save HTML report. Returns path to saved file."""
    content = generate_report_html(ticker)
    ticker_dir = OUT_DIR / ticker.upper()
    ticker_dir.mkdir(parents=True, exist_ok=True)
    path = ticker_dir / "latest.html"
    path.write_text(content)
    return path


def run_all_reports_html(limit: int = 50) -> int:
    """Generate HTML reports for all ConfluenceSignal tickers with BUY signal."""
    with _conn() as c:
        rows = c.execute(
            """SELECT DISTINCT ticker FROM nodes
               WHERE node_type='ConfluenceSignal'
               AND (json_extract(properties,'$.signal')='BUY'
                    OR json_extract(properties,'$.type') LIKE 'BUY%')
               ORDER BY confidence DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    tickers = [r[0] for r in rows]

    saved = 0
    for t in tickers:
        try:
            save_report_html(t)
            saved += 1
        except Exception as e:
            print(f"  [html-report] SKIP {t}: {e}")
    return saved


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Generate HTML research reports per ticker")
    p.add_argument("--ticker", help="Single ticker to generate HTML report for")
    p.add_argument("--all",    action="store_true", help="Generate for all BUY signals")
    p.add_argument("--limit",  type=int, default=50)
    args = p.parse_args()

    if args.ticker:
        path = save_report_html(args.ticker.upper())
        print(f"HTML report saved: {path}")
    elif args.all:
        n = run_all_reports_html(limit=args.limit)
        print(f"Generated {n} HTML reports under reports/tickers/")
    else:
        p.print_help()
