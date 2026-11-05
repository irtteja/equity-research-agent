"""
Research report generator — deep-dive 2-3 page per-ticker markdown reports from graph.db.

Pulls all data layers: investment thesis, executive summary, signal, price, technicals,
valuation, business quality, management guidance, risk register, peer context, macro context,
fundamentals. Saves to reports/tickers/<TICKER>/YYYY-MM-DD.md and latest.md.

New in this version:
  - Investment Thesis paragraph synthesized from available signals
  - Executive Summary 3-bullet header
  - Conviction trend (rising/falling/stable) from conviction_history
  - Business Quality section (BusinessQuality node or fallback from ValuationScore)
  - Management Guidance section (ConcallInsight node — graceful if absent)
  - Risk Register combining BearCase + alerts
  - Peer Context (PeerComparison node — graceful if absent)
  - 52W position percentile and volume vs avg from TechnicalSignal
  - RelativeStrength label integrated into Price section

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


# ── DB helpers ────────────────────────────────────────────────────────────────

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
        "bear_score":     p.get("bear_score", 0),
        "net_conviction": p.get("net_conviction", 0),
        "verdict":        p.get("verdict", "?"),
        "arguments":      p.get("bear_arguments", []),
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
                "SELECT conviction_score FROM conviction_history WHERE ticker=? ORDER BY record_date DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        return float(row[0]) if row else None
    except Exception:
        return None


def _get_conviction_trend(ticker: str) -> str:
    """Return 'rising', 'falling', or 'stable' based on last 7 days of conviction_history."""
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT record_date, conviction_score FROM conviction_history
                   WHERE ticker=? ORDER BY record_date DESC LIMIT 7""",
                (ticker,),
            ).fetchall()
        if not rows or len(rows) < 2:
            return "stable"
        # rows[0] = most recent, rows[-1] = oldest
        newest = float(rows[0][1])
        oldest = float(rows[-1][1])
        delta = newest - oldest
        if delta > 0.05:
            return "rising"
        elif delta < -0.05:
            return "falling"
        return "stable"
    except Exception:
        return "stable"


def _get_sentiment(ticker: str) -> dict | None:
    n = _get_node(ticker, "SentimentScore")
    if not n:
        return None
    p = n["properties"]
    return {
        "score":  p.get("sentiment_score", 0.5),
        "label":  p.get("label", p.get("verdict", "neutral")),
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


def _fmt_arg(arg) -> str:
    """Format a bear argument that may be a str or dict."""
    if isinstance(arg, str):
        return arg
    if isinstance(arg, dict):
        reason = arg.get("reason", "")
        dp = arg.get("data_point", "")
        sev = arg.get("severity", "")
        parts = [p for p in [reason, dp] if p]
        label = " | ".join(parts)
        if sev and sev != "medium":
            label = f"[{sev.upper()}] {label}"
        return label or str(arg)
    return str(arg)


# ── Thesis synthesis ──────────────────────────────────────────────────────────

def _build_thesis(ticker, signal, trend, tech, bear, senti, val) -> str:
    """
    Synthesize a one-paragraph investment thesis from available data.
    Priority: confluence reason → technical color → valuation → bear caveat.
    """
    parts = []

    # 1. Confluence reason — the core WHY this ticker surfaced
    sp = (signal or {}).get("properties", {})
    reason = sp.get("reason", "")
    if reason:
        parts.append(reason)

    # 2. Technical color
    techp = (tech or {}).get("properties", {})
    sig_type = techp.get("signal", "")
    rsi = techp.get("rsi")
    if sig_type == "oversold_bounce":
        parts.append("Technical indicators show oversold conditions with potential mean reversion.")
    elif sig_type == "momentum":
        rsi_txt = f" (RSI {rsi:.0f})" if rsi else ""
        parts.append(f"Price momentum is positive with healthy technicals{rsi_txt}.")
    elif sig_type == "breakout":
        parts.append("Price is breaking out above key moving averages on elevated volume.")
    elif sig_type == "trend_following":
        parts.append("Price is in a confirmed uptrend above key moving averages.")

    # 3. Valuation context
    vp = (val or {}).get("properties", {})
    verd = vp.get("verdict", "")
    pe = vp.get("pe")
    if verd == "cheap":
        pe_txt = f" (P/E {pe:.1f}x)" if pe else ""
        parts.append(f"Valuation is cheap relative to history{pe_txt}, offering a margin of safety.")
    elif verd == "fair":
        pe_txt = f" (P/E {pe:.1f}x)" if pe else ""
        parts.append(f"Valuation is fair{pe_txt} — not expensive for the growth on offer.")
    elif verd == "expensive":
        parts.append("Valuation is stretched; needs fundamental delivery to justify the multiple.")

    # 4. Sentiment tone
    senti_label = (senti or {}).get("label", "neutral")
    if senti_label in ("bullish", "very_bullish"):
        parts.append("Management tone is constructive based on recent communications.")
    elif senti_label in ("bearish", "very_bearish"):
        parts.append("Management language carries cautious undertones — monitor guidance closely.")

    # 5. Bear caveat — if bear score is meaningful
    bs = (bear or {}).get("bear_score", 0)
    args = (bear or {}).get("arguments", [])
    if bs > 0.2 and args:
        top_arg = _fmt_arg(args[0])
        if top_arg:
            parts.append(f"Key risk to monitor: {top_arg}.")
    elif bs > 0.1:
        parts.append("Bear arguments are limited but warrant monitoring.")

    # Ensure each part ends with a sentence-terminating character before joining
    cleaned = []
    for part in parts:
        part = part.strip()
        if part and part[-1] not in ".!?":
            part += "."
        if part:
            cleaned.append(part)
    return " ".join(cleaned) or "Insufficient data to synthesize a thesis; primary signals are forming."


# ── Main report generator ─────────────────────────────────────────────────────

def generate_report(ticker: str) -> str:
    """Generate a deep-dive markdown research report for the given ticker. Returns markdown string."""
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
    rs_node   = _get_node(ticker, "RelativeStrength")
    # Nodes that may not exist yet — handle gracefully
    concall   = _get_node(ticker, "ConcallInsight")
    peer_cmp  = _get_node(ticker, "PeerComparison")
    biz_qual  = _get_node(ticker, "BusinessQuality")
    analyst   = _get_node(ticker, "AnalystConsensus")
    surv_flag = _get_node(ticker, "SurveillanceFlag")

    # ── Company header ─────────────────────────────────────────────────────────
    comp_props = (company or {}).get("properties", {})
    name   = (company or {}).get("name") or ticker
    sector = comp_props.get("sector") or (trend or {}).get("properties", {}).get("sector", "—") or "—"
    industry = comp_props.get("industry", "")

    # ── Technicals (extracted early for 52W fallback) ────────────────────────
    techp      = (tech or {}).get("properties", {})

    # ── Price data ─────────────────────────────────────────────────────────────
    pp  = (price_sn or {}).get("properties", {})
    close     = pp.get("close",    pp.get("ltp", "—"))
    chg_pct   = pp.get("change_pct", pp.get("pct_change", "—"))
    # Fallback 52W hi/lo to TechnicalSignal which reliably stores them
    hi52w     = pp.get("high_52w",   pp.get("yearHigh", techp.get("high52w", "—")))
    lo52w     = pp.get("low_52w",    pp.get("yearLow",  techp.get("low52w",  "—")))
    volume    = pp.get("volume",     pp.get("tradedVolume", "—"))

    # ── Signal ─────────────────────────────────────────────────────────────────
    sp     = (signal or {}).get("properties", {})
    raw_type  = sp.get("type", "")
    sig_name  = "BUY" if "BUY" in raw_type else ("AVOID" if "AVOID" in raw_type else sp.get("signal", "HOLD"))
    conf      = signal.get("confidence", 0.5) if signal else 0.5
    bstate    = signal.get("belief_state", "forming") if signal else "forming"

    # ── Trend ──────────────────────────────────────────────────────────────────
    tp = (trend or {}).get("properties", {})
    trend_dir   = tp.get("trend",        "—")
    rsi_val     = tp.get("rsi",          "—")
    rsi_tech   = techp.get("rsi",         rsi_val)
    macd_hist  = techp.get("macd_hist",   "—")
    ema_cross  = techp.get("above_ema20", "—")
    tech_sig   = techp.get("signal",      "—")
    pos_52w    = techp.get("pos_52w",     None)   # fraction 0–1 of 52W range
    vol_ratio  = techp.get("vol_ratio",   None)   # vol / 20d avg vol

    # ── Valuation ─────────────────────────────────────────────────────────────
    vp = (val or {}).get("properties", {})
    pe_val    = vp.get("pe",            "—")
    pe_avg    = vp.get("pe_3y_avg",     "—")
    pat_growth= vp.get("pat_growth_pct","—")
    rev_growth= vp.get("rev_growth_pct","—")
    ebit_mgn  = vp.get("ebit_margin_pct","—")
    fcf_proxy = vp.get("fcf_proxy_cr",  "—")
    debt_eq   = vp.get("debt_equity",   "—")
    int_cov   = vp.get("int_coverage",  "—")
    val_verd  = vp.get("verdict",       "—")
    val_score = vp.get("valuation_score","—")

    # ── Fundamentals (MetricValue fallback) ───────────────────────────────────
    mp = (met or {}).get("properties", {})
    rev_growth_mv  = mp.get("revenue_growth_yoy", mp.get("revenue_growth", "—"))
    pat_growth_mv  = mp.get("pat_growth_yoy",     mp.get("pat_growth", "—"))
    roe            = mp.get("roe",             "—")
    promoter_pct   = mp.get("promoter_holding","—")
    # Use ValuationScore fields when available, fall back to MetricValue
    rev_g_disp = rev_growth if rev_growth != "—" else rev_growth_mv
    pat_g_disp = pat_growth if pat_growth != "—" else pat_growth_mv

    # ── Sentiment & conviction ────────────────────────────────────────────────
    sent_label = (senti or {}).get("label", "neutral")
    sent_score = (senti or {}).get("score", 0.5)
    conv_str   = f"{conv:.2f}" if conv else "—"
    conv_trend = _get_conviction_trend(ticker)

    # ── Bear case ─────────────────────────────────────────────────────────────
    bear_score     = (bear or {}).get("bear_score", 0)
    bear_args      = (bear or {}).get("arguments", [])
    bear_verdict   = (bear or {}).get("verdict", "—")
    net_conviction = (bear or {}).get("net_conviction", 0)

    # ── Relative strength ─────────────────────────────────────────────────────
    rsp = (rs_node or {}).get("properties", {})
    rs_label = rsp.get("label", "—")
    rs_30    = rsp.get("rs_30", "—")

    # ── Macro ─────────────────────────────────────────────────────────────────
    macro_sig    = macro.get("macro_signal",  "—")
    macro_score  = macro.get("macro_score",   "—")
    macro_detail = macro.get("details", {})

    # ── Peer comparison ───────────────────────────────────────────────────────
    pcp = (peer_cmp or {}).get("properties", {})

    # ── Business quality ──────────────────────────────────────────────────────
    bqp = (biz_qual or {}).get("properties", {})

    # ── ConcallInsight ────────────────────────────────────────────────────────
    ccp = (concall or {}).get("properties", {})

    # ── Analyst consensus (TradingView) ───────────────────────────────────────
    acp = (analyst or {}).get("properties", {})
    ac_consensus  = acp.get("consensus", "")
    ac_total      = acp.get("total_analysts", 0)
    ac_buy        = acp.get("buy_total", acp.get("buy", 0))
    ac_hold       = acp.get("hold", 0)
    ac_sell       = acp.get("sell_total", acp.get("sell", 0))
    ac_target     = acp.get("price_target_1y")
    ac_upside     = acp.get("upside_pct")

    # ── Surveillance flag ─────────────────────────────────────────────────────
    sfp = (surv_flag or {}).get("properties", {})
    sf_severity   = sfp.get("severity", "")
    sf_flags      = sfp.get("flags", {})

    # ── Build investment thesis ────────────────────────────────────────────────
    thesis = _build_thesis(ticker, signal, trend, tech, bear, senti, val)

    # ── Signal color indicator ─────────────────────────────────────────────────
    sig_icon   = "🟢" if sig_name == "BUY" else ("🔴" if sig_name == "AVOID" else "🟡")
    state_icon = "✅" if bstate == "confirmed" else ("⚠️" if bstate == "contested" else "🔵")
    trend_icon = "↗" if conv_trend == "rising" else ("↘" if conv_trend == "falling" else "→")

    # ── Assemble report ────────────────────────────────────────────────────────
    lines = [
        f"# {ticker} — {name}",
        f"*{sector}{' | ' + industry if industry else ''} | {today}*",
        "",
        "---",
        "",
    ]

    # ── INVESTMENT THESIS ──────────────────────────────────────────────────────
    lines += [
        "## INVESTMENT THESIS",
        "",
        thesis,
        "",
    ]

    # ── EXECUTIVE SUMMARY ──────────────────────────────────────────────────────
    # Build key driver from confluence reason
    key_driver = sp.get("reason", "Signal forming — awaiting further corroboration.")
    top_bear_txt = ""
    if bear_score > 0.1 and bear_args:
        top_bear_txt = _fmt_arg(bear_args[0])

    lines += [
        "## EXECUTIVE SUMMARY",
        "",
        f"- **Signal:** {sig_name} / {conf:.0%} confidence / {bstate.upper()} state",
        f"- **Key driver:** {key_driver}",
    ]
    if top_bear_txt:
        lines.append(f"- **Watch:** {top_bear_txt}")
    else:
        lines.append(f"- **Watch:** No material bear arguments at this time (bear score {bear_score:.2f})")
    lines.append("")

    # ── SIGNAL & CONVICTION ───────────────────────────────────────────────────
    lines += [
        "## SIGNAL & CONVICTION",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Signal | {sig_icon} **{sig_name}** |",
        f"| Confidence | {conf:.1%} |",
        f"| Belief State | {state_icon} {bstate.upper()} |",
        f"| Conviction Score | {conv_str} |",
        f"| Conviction Trend (7d) | {trend_icon} {conv_trend.upper()} |",
        f"| Sentiment | {sent_label} ({sent_score:.2f}) |",
        f"| Net Conviction | {_fmt_num(net_conviction)} |",
        "",
    ]

    # ── PRICE & TECHNICALS ────────────────────────────────────────────────────
    # 52W position as pct (pos_52w is fraction)
    pos_52w_pct = f"{float(pos_52w)*100:.0f}th percentile" if pos_52w is not None else "—"
    vol_vs_avg  = f"{float(vol_ratio):.2f}x average" if vol_ratio is not None else "—"
    ema_cross_txt = "Above EMA20" if ema_cross is True else ("Below EMA20" if ema_cross is False else str(ema_cross))

    lines += [
        "## PRICE & TECHNICALS",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Close | ₹{close} |",
        f"| Change | {_fmt_pct(chg_pct)} |",
        f"| 52W High | ₹{hi52w} |",
        f"| 52W Low | ₹{lo52w} |",
        f"| 52W Position | {pos_52w_pct} |",
        f"| Relative Strength | {rs_label} (rs_30: {_fmt_num(rs_30, 1)}) |",
        f"| Volume vs Avg | {vol_vs_avg} |",
        f"| RSI (14) | {_fmt_num(rsi_tech, 1)} |",
        f"| MACD Hist | {_fmt_num(macd_hist, 2)} |",
        f"| EMA20 | {ema_cross_txt} |",
        f"| Technical Signal | **{tech_sig}** |",
        "",
    ]

    # ── ANALYST CONSENSUS (TradingView) ──────────────────────────────────────
    if analyst and ac_total:
        ac_icon = "🟢" if ac_consensus in ("STRONG_BUY", "BUY") else ("🔴" if ac_consensus == "SELL" else "🟡")
        buy_pct = round(ac_buy / ac_total * 100) if ac_total else 0
        lines += [
            "## ANALYST CONSENSUS",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Consensus | {ac_icon} **{ac_consensus}** |",
            f"| Buy / Hold / Sell | {ac_buy} / {ac_hold} / {ac_sell} (of {ac_total} analysts) |",
            f"| Buy % | {buy_pct}% |",
        ]
        if ac_target:
            lines.append(f"| Price Target 1Y | ₹{ac_target:.0f} |")
        if ac_upside is not None:
            upside_icon = "▲" if ac_upside > 0 else "▼"
            lines.append(f"| Upside vs CMP | {upside_icon} {abs(ac_upside):.1f}% |")
        lines.append("")

    # ── SURVEILLANCE WARNING ──────────────────────────────────────────────────
    if surv_flag and sf_severity:
        active_flags = ", ".join(f"{k.upper()}:{v}" for k, v in sf_flags.items() if v)
        lines += [
            f"> ⚠️ **SURVEILLANCE FLAG ({sf_severity.upper()})**: This ticker is under NSE surveillance "
            f"({active_flags}). Treat BUY signal with extreme caution — elevated risk of regulatory action.",
            "",
        ]

    # ── VALUATION ─────────────────────────────────────────────────────────────
    lines += [
        "## VALUATION",
        "",
        f"| Metric | Value | Context |",
        f"|--------|-------|---------|",
        f"| P/E | {_fmt_num(pe_val, 1)}x | 3Y avg: {pe_avg} |",
        f"| Valuation Score | {_fmt_num(val_score, 2)} | 0=cheap, 1=expensive |",
        f"| Verdict | **{val_verd}** | |",
        f"| PAT Growth YoY | {_fmt_pct(pat_g_disp)} | |",
        f"| Rev Growth YoY | {_fmt_pct(rev_g_disp)} | |",
        f"| EBIT Margin | {_fmt_pct(ebit_mgn)} | |",
        f"| FCF Proxy (Cr) | {_fmt_num(fcf_proxy, 0)} | |",
        f"| Debt/Equity | {_fmt_num(debt_eq, 2)} | |",
        f"| Interest Coverage | {_fmt_num(int_cov, 1)}x | |",
    ]
    if pcp:
        pe_rank = pcp.get("pe_rank", "—")
        vs_peer_avg = pcp.get("vs_peer_avg_pe", "—")
        lines.append(f"| Peer P/E Rank | {pe_rank} | vs peer avg: {vs_peer_avg} |")
    lines.append("")

    # ── BUSINESS QUALITY ──────────────────────────────────────────────────────
    lines += ["## BUSINESS QUALITY", ""]
    if biz_qual and bqp:
        roce_trend  = bqp.get("roce_trend",  "—")
        fcf_quality = bqp.get("fcf_quality", "—")
        margin_trend= bqp.get("margin_trend","—")
        cap_alloc   = bqp.get("capital_allocation","—")
        quality_lbl = bqp.get("overall_quality","—")
        lines += [
            f"| Dimension | Assessment |",
            f"|-----------|-----------|",
            f"| ROCE Trend | {roce_trend} |",
            f"| FCF Quality | {fcf_quality} |",
            f"| Margin Trend | {margin_trend} |",
            f"| Capital Allocation | {cap_alloc} |",
            f"| **Overall Quality** | **{quality_lbl}** |",
            "",
        ]
    else:
        # Synthesize from ValuationScore data as a proxy
        fcf_cr = vp.get("fcf_proxy_cr", 0)
        int_cov_v = vp.get("int_coverage", 0)
        try:
            fcf_quality = "positive" if float(fcf_cr) > 0 else "negative"
        except Exception:
            fcf_quality = "—"
        try:
            leverage = "low" if float(int_cov_v) > 5 else ("moderate" if float(int_cov_v) > 2 else "stressed")
        except Exception:
            leverage = "—"
        try:
            margin_q = "expanding" if float(ebit_mgn) > 10 else ("stable" if float(ebit_mgn) > 5 else "thin")
        except Exception:
            margin_q = "—"
        lines += [
            "*BusinessQuality node not yet built — proxy assessment from ValuationScore:*",
            "",
            f"| Dimension | Proxy Assessment |",
            f"|-----------|-----------------|",
            f"| FCF Generation | {fcf_quality} (₹{_fmt_num(fcf_cr, 0)} Cr TTM) |",
            f"| Leverage | {leverage} (interest coverage {_fmt_num(int_cov_v, 1)}x) |",
            f"| Margin Profile | {margin_q} (EBIT margin {_fmt_pct(ebit_mgn)}) |",
            "",
        ]

    # ── MANAGEMENT GUIDANCE ───────────────────────────────────────────────────
    lines += ["## MANAGEMENT GUIDANCE", ""]
    if concall and ccp:
        # ConcallInsight may store guidance as list or string
        guidance_raw = ccp.get("guidance", [])
        if isinstance(guidance_raw, list):
            guidance = "; ".join(guidance_raw) if guidance_raw else "—"
        else:
            guidance = str(guidance_raw) or "—"
        themes     = ccp.get("key_themes", [])
        tone       = ccp.get("tone", "—")
        numbers    = ccp.get("specific_numbers", ccp.get("key_numbers", []))
        pos_signals= ccp.get("positive_signals", [])
        lines += [
            f"**Tone:** {tone}  |  **Sources:** {ccp.get('statement_count', '?')} management statements",
            "",
        ]
        if guidance != "—":
            lines += [f"**Guidance:** {guidance}", ""]
        if themes:
            lines.append("**Key Themes:**")
            for t in themes[:3]:
                lines.append(f"- {t}")
            lines.append("")
        if pos_signals:
            lines.append("**Notable statements:**")
            for s in pos_signals[:2]:
                lines.append(f"- {str(s)[:200]}")
            lines.append("")
        if numbers:
            lines.append("**Numbers mentioned:**")
            for n in numbers[:3]:
                lines.append(f"- {n}")
            lines.append("")
    elif stmts:
        lines.append("*ConcallInsight node not yet built — recent management statements from graph:*")
        lines.append("")
        for s in stmts:
            sp2  = s.get("properties", {})
            text = sp2.get("text", sp2.get("statement", ""))
            src  = sp2.get("source", "")
            if text:
                label = f"- {text[:300]}"
                if src:
                    label += f" *(source: {src})*"
                lines.append(label)
        lines.append("")
    else:
        lines.append("*No management guidance data available yet.*")
        lines.append("")

    # ── RISK REGISTER ─────────────────────────────────────────────────────────
    lines += ["## RISK REGISTER", ""]
    risk_count = 0
    if bear_args:
        for i, arg in enumerate(bear_args, 1):
            arg_txt = _fmt_arg(arg)
            if arg_txt:
                lines.append(f"{i}. {arg_txt}")
                risk_count += 1
    if not risk_count:
        lines.append("No material bear arguments identified.")
    lines.append("")

    # Concall risks as additional context
    concall_risks = ccp.get("risks_flagged", ccp.get("risks", []))
    if concall and concall_risks:
        lines.append("**Additional risks from concall:**")
        for r in concall_risks[:3]:
            lines.append(f"- {r}")
        lines.append("")

    # Alerts
    if alerts:
        for a in alerts:
            icon = "🚨" if a["severity"] == "critical" else ("⚠️" if a["severity"] == "warning" else "ℹ️")
            lines.append(f"{icon} **Monitor:** [{a['type']}] {a['msg']}")
        lines.append("")

    # ── PEER CONTEXT ──────────────────────────────────────────────────────────
    lines += ["## PEER CONTEXT", ""]
    if peer_cmp and pcp:
        peer_verdict = pcp.get("peer_verdict", "—")
        pe_rank      = pcp.get("pe_rank", "—")
        growth_rank  = pcp.get("growth_rank", "—")
        margin_rank  = pcp.get("margin_rank", "—")
        lines += [
            f"**Verdict vs peers:** {peer_verdict}",
            "",
            f"| Dimension | Peer Rank |",
            f"|-----------|----------|",
            f"| P/E | {pe_rank} |",
            f"| Revenue Growth | {growth_rank} |",
            f"| EBIT Margin | {margin_rank} |",
            "",
        ]
    else:
        lines += [
            "*PeerComparison node not yet built — scheduled for a future session.*",
            "",
            f"Sector: **{sector}**{' | ' + industry if industry else ''}",
            "",
        ]

    # ── MACRO CONTEXT ─────────────────────────────────────────────────────────
    lines += ["## MACRO CONTEXT", ""]
    # Map sector to macro sensitivity
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
        "Consumer Durables":          "sensitive to consumer sentiment and input costs",
        "Cement":                     "linked to real estate/infra activity and power costs",
        "Power":                      "linked to government policy, coal prices",
        "Chemicals":                  "sensitive to crude oil and global chemical prices",
    }
    macro_sensitivity = ""
    for key, note in sector_macro.items():
        if key.lower() in (sector or "").lower():
            macro_sensitivity = note
            break

    lines += [
        f"**Global Signal:** {macro_sig}  |  **Score:** {macro_score}",
        "",
    ]
    if macro_sensitivity:
        lines.append(f"**Sector sensitivity:** {sector} is {macro_sensitivity}.")
        lines.append("")

    # Macro detail if available
    if isinstance(macro_detail, dict) and macro_detail:
        for k, v in list(macro_detail.items())[:4]:
            lines.append(f"- {k}: {v}")
        lines.append("")

    # ── FUNDAMENTALS ──────────────────────────────────────────────────────────
    lines += [
        "## FUNDAMENTALS",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Revenue Growth YoY | {_fmt_pct(rev_g_disp)} |",
        f"| PAT Growth YoY | {_fmt_pct(pat_g_disp)} |",
        f"| EBIT Margin | {_fmt_pct(ebit_mgn)} |",
        f"| Debt/Equity | {_fmt_num(debt_eq, 2)} |",
        f"| Interest Coverage | {_fmt_num(int_cov, 1)}x |",
        f"| FCF Proxy (Cr) | {_fmt_num(fcf_proxy, 0)} |",
        f"| ROE | {_fmt_pct(roe)} |",
        f"| Promoter Holding | {_fmt_pct(promoter_pct)} |",
        "",
        "---",
        f"*Confidence: {conf:.0%} | Generated: {ts} | Next review: +30d*",
    ]

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
            save_report(t)
            saved += 1
        except Exception as e:
            print(f"  [report] SKIP {t}: {e}")

    return saved


if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, str(REPO))
    p = argparse.ArgumentParser(description="Generate deep-dive per-ticker research reports")
    p.add_argument("--ticker", help="Single ticker to report (prints to stdout + saves)")
    p.add_argument("--all",    action="store_true", help="Generate for all BUY signals")
    p.add_argument("--limit",  type=int, default=50)
    p.add_argument("--print",  action="store_true", help="Print report to stdout (used with --ticker)")
    args = p.parse_args()

    if args.ticker:
        path = save_report(args.ticker.upper())
        md = path.read_text()
        print(md)
        print(f"\n--- Report saved: {path}")
    elif args.all:
        n = run_all_reports(limit=args.limit)
        print(f"Generated {n} reports under reports/tickers/")
    else:
        p.print_help()
