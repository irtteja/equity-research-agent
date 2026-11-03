"""
AlertAgent — surfaces strong signals from the knowledge graph as a daily brief.

Reads:
  TrendSummary nodes (written by TrendAgent) — fundamental momentum signals
  Memory store indices bhavcopy — market context
  Memory store announcements — recent corporate events

Writes:
  reports/daily/alert-YYYY-MM-DD.json — structured brief
  Prints human-readable brief to stdout

Output brief covers:
  - Index context (Nifty, Bank, IT, MidCap)
  - Strong fundamental momentum (improving + deteriorating)
  - Sector pulse (how many stocks trending each way per sector)
  - Inflection points (momentum direction reversals)
  - Watch list
"""
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

# Sector mappings — based on MISSION.md universe + NSE indices
SECTOR_MAP = {
    "HDFCBANK": "Finance", "ICICIBANK": "Finance", "KOTAKBANK": "Finance",
    "SBIN": "Finance", "AXISBANK": "Finance", "BAJFINANCE": "Finance",
    "BAJAJFINSV": "Finance", "HDFCLIFE": "Finance", "SBILIFE": "Finance",
    "ICICIGI": "Finance", "INDUSINDBK": "Finance", "FEDERALBNK": "Finance",
    "BANDHANBNK": "Finance", "AUBANK": "Finance", "IDFCFIRSTB": "Finance",
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT",
    "TECHM": "IT", "LTIM": "IT", "PERSISTENT": "IT", "MPHASIS": "IT",
    "COFORGE": "IT", "OFSS": "IT", "KPIT": "IT",
    "RELIANCE": "Energy", "ONGC": "Energy", "BPCL": "Energy",
    "NTPC": "Energy", "POWERGRID": "Energy", "ADANIGREEN": "Energy",
    "ADANIPORTS": "Energy", "TATAPOWER": "Energy",
    "TATAMOTORS": "Auto", "MARUTI": "Auto", "M&M": "Auto",
    "BAJAJ-AUTO": "Auto", "HEROMOTOCO": "Auto", "EICHERMOT": "Auto",
    "TVSMOTOR": "Auto", "ASHOKLEY": "Auto",
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma",
    "DIVISLAB": "Pharma", "BIOCON": "Pharma", "LUPIN": "Pharma",
    "AUROPHARMA": "Pharma", "TORNTPHARM": "Pharma",
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG", "DABUR": "FMCG", "MARICO": "FMCG", "COLPAL": "FMCG",
    "TITAN": "Consumer", "VMART": "Consumer", "TRENT": "Consumer",
    "DMART": "Consumer",
    "LT": "Infra", "ADANIENT": "Infra", "ULTRACEMCO": "Infra",
    "GRASIM": "Infra", "SHREECEM": "Infra", "ACC": "Infra",
    "TATASTEEL": "Metals", "JSWSTEEL": "Metals", "HINDALCO": "Metals",
    "VEDL": "Metals", "COALINDIA": "Metals",
    "BHARTIARTL": "Telecom", "INDUSTOWER": "Telecom",
}

KEY_INDICES = [
    "Nifty 50", "Nifty Bank", "Nifty IT", "Nifty Midcap 150",
    "Nifty Smallcap 250", "India VIX",
]

MOMENTUM_STRONG   = 0.6
MOMENTUM_MODERATE = 0.3
INFLECTION_STREAK = 1   # first period after trend reversal

# Metrics that matter for "improving" signals (growth metrics)
GROWTH_METRICS = {
    "revenue", "revenue_from_operations", "total_income", "pat", "pbt",
    "ebit", "ebitda", "eps", "eps_basic", "eps_diluted",
    "operating_profit", "gross_profit", "net_profit",
    "total_comprehensive_income",
}
# Metrics where going DOWN is "improving" (cost efficiency)
COST_METRICS = {
    "total_expenses", "finance_costs", "employee_costs", "depreciation",
    "other_expenses", "tax_expense",
}


def _get_sector(ticker: str) -> str:
    return SECTOR_MAP.get(ticker.upper(), "Other")


class AlertAgent:
    """Generates the daily market intelligence brief."""

    def __init__(self):
        from memory.graph_store import get_store
        self.gs = get_store()

    # ── Public interface ────────────────────────────────────────────────────

    def generate_brief(self, as_of: date | None = None) -> dict:
        as_of = as_of or date.today()

        trends   = self._get_all_trends()
        context  = self._get_index_context(as_of)
        events   = self._get_recent_events(as_of)
        graph_st = self.gs.stats()

        brief = self._build_brief(trends, context, events, graph_st, as_of)
        self._save(brief, as_of)
        return brief

    def print_brief(self, brief: dict | None = None, as_of: date | None = None):
        if brief is None:
            brief = self.generate_brief(as_of)
        print(self._format(brief))
        return brief

    # ── Data collection ─────────────────────────────────────────────────────

    def _get_all_trends(self) -> list[dict]:
        """Pull all TrendSummary nodes from graph."""
        with self.gs._conn() as conn:
            rows = conn.execute(
                "SELECT id, ticker, properties FROM nodes WHERE node_type='TrendSummary'"
            ).fetchall()

        trends = []
        for row in rows:
            try:
                props = json.loads(row["properties"]) if isinstance(row["properties"], str) else row["properties"]
                trends.append({
                    "ticker":       props.get("ticker", row["ticker"] or "?").upper(),
                    "metric":       props.get("metric", "?"),
                    "direction":    props.get("direction", "stable"),
                    "streak":       props.get("consistent_periods", 1),
                    "delta_pct":    props.get("latest_delta_pct", 0.0),
                    "momentum":     props.get("momentum_score", 0.0),
                    "acceleration": props.get("acceleration", "steady"),
                    "periods":      props.get("periods_analyzed", 0),
                    "summary":      props.get("summary_text", ""),
                })
            except Exception:
                continue
        return trends

    def _get_index_context(self, as_of: date) -> dict:
        """Load most recent indices bhavcopy."""
        bhav_dir = REPO / "memory_store" / "indices_bhavcopy"
        if not bhav_dir.exists():
            return {}

        # Find the latest CSV at or before as_of
        csvs = sorted(bhav_dir.glob("*.csv"), reverse=True)
        target = None
        for p in csvs:
            try:
                d = date.fromisoformat(p.stem)
                if d <= as_of:
                    target = p
                    break
            except ValueError:
                continue

        if not target:
            return {}

        import csv
        indices = {}
        with open(target, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("Index Name", "").strip()
                if any(ki.lower() in name.lower() for ki in KEY_INDICES):
                    try:
                        indices[name] = {
                            "close":    float(row.get("Closing Index Value", 0) or 0),
                            "change":   float(row.get("Change(%)", 0) or 0),
                            "pe":       row.get("P/E", ""),
                            "pb":       row.get("P/B", ""),
                            "div":      row.get("Div Yield", ""),
                            "date":     row.get("Index Date", ""),
                        }
                    except (ValueError, TypeError):
                        pass
        return indices

    def _get_recent_events(self, as_of: date) -> list[dict]:
        """Pull recent corporate events from announcements store."""
        ann_dir = REPO / "memory_store" / "announcements"
        if not ann_dir.exists():
            return []

        events = []
        for f in sorted(ann_dir.glob("*.json"), reverse=True)[:3]:
            try:
                data = json.loads(f.read_text())
                for sym, anns in (data.get("announcements") or {}).items():
                    for ann in (anns or [])[:2]:
                        cat = ann.get("desc", ann.get("subject", ""))
                        if any(kw in cat.lower() for kw in (
                            "result", "dividend", "buyback", "block deal",
                            "acquisition", "merger", "qip", "split", "bonus"
                        )):
                            events.append({
                                "ticker": sym,
                                "date":   ann.get("excDt", ann.get("date", "")),
                                "event":  cat[:80],
                            })
            except Exception:
                continue
        return events[:20]

    # ── Brief construction ──────────────────────────────────────────────────

    def _build_brief(self, trends, context, events, graph_stats, as_of) -> dict:
        # Filter to meaningful growth signals only
        growth_up = [
            t for t in trends
            if t["direction"] == "improving"
            and t["momentum"] >= MOMENTUM_STRONG
            and t["streak"] >= 2
            and t["metric"].lower().replace("-", "_") in GROWTH_METRICS
        ]
        cost_down = [
            t for t in trends
            if t["direction"] == "deteriorating"
            and t["momentum"] <= -MOMENTUM_STRONG
            and t["streak"] >= 2
            and t["metric"].lower().replace("-", "_") in GROWTH_METRICS
        ]
        # Deduplicate: best signal per ticker (highest |momentum| × streak)
        def _dedup(lst):
            seen = {}
            for t in sorted(lst, key=lambda x: -(abs(x["momentum"]) * x["streak"])):
                if t["ticker"] not in seen:
                    seen[t["ticker"]] = t
            return sorted(seen.values(), key=lambda x: -(abs(x["momentum"]) * x["streak"]))

        strong_pos = _dedup(growth_up)
        strong_neg = _dedup(cost_down)

        inflections = [
            t for t in trends
            if t["streak"] == INFLECTION_STREAK
            and t["periods"] >= 2
            and t["metric"].lower().replace("-", "_") in GROWTH_METRICS
        ]
        # Dedup inflections by ticker too
        inf_seen = {}
        for t in inflections:
            if t["ticker"] not in inf_seen:
                inf_seen[t["ticker"]] = t
        inflections = list(inf_seen.values())

        # Sector pulse
        sector_pulse = {}
        for t in trends:
            sec = _get_sector(t["ticker"])
            if sec not in sector_pulse:
                sector_pulse[sec] = {"improving": set(), "deteriorating": set(), "stable": set()}
            if t["momentum"] >= MOMENTUM_MODERATE:
                sector_pulse[sec]["improving"].add(t["ticker"])
            elif t["momentum"] <= -MOMENTUM_MODERATE:
                sector_pulse[sec]["deteriorating"].add(t["ticker"])
            else:
                sector_pulse[sec]["stable"].add(t["ticker"])

        sector_summary = {
            sec: {
                "improving":    len(v["improving"]),
                "deteriorating":len(v["deteriorating"]),
                "stable":       len(v["stable"]),
                "tickers_up":   sorted(v["improving"])[:5],
                "tickers_down": sorted(v["deteriorating"])[:5],
            }
            for sec, v in sorted(sector_pulse.items())
        }

        return {
            "date":          as_of.isoformat(),
            "generated_at":  datetime.now().isoformat(),
            "graph":         graph_stats,
            "indices":       context,
            "strong_improving":   strong_pos[:15],
            "strong_deteriorating": strong_neg[:15],
            "inflections":   inflections[:10],
            "sector_pulse":  sector_summary,
            "recent_events": events,
            "summary": {
                "total_trends":    len(trends),
                "strong_positive": len(strong_pos),
                "strong_negative": len(strong_neg),
                "inflections":     len(inflections),
                "sectors_tracked": len(sector_pulse),
            },
        }

    # ── Formatting ──────────────────────────────────────────────────────────

    def _format(self, brief: dict) -> str:
        lines = []
        d     = brief.get("date", date.today().isoformat())
        ts    = brief.get("generated_at", "")[:16].replace("T", " ")

        lines.append(f"\n{'='*64}")
        lines.append(f"  MARKET INTELLIGENCE BRIEF — {d}  [{ts}]")
        lines.append(f"{'='*64}\n")

        # Indices
        idx = brief.get("indices", {})
        if idx:
            lines.append("INDICES")
            for name, v in idx.items():
                arrow  = "▲" if v["change"] >= 0 else "▼"
                change = f"{arrow}{abs(v['change']):.2f}%"
                pe_str = f"  P/E {v['pe']}" if v.get("pe") else ""
                lines.append(f"  {name:<22} {v['close']:>10,.0f}  {change:>8}{pe_str}")
            lines.append("")

        # Strong improving
        pos = brief.get("strong_improving", [])
        neg = brief.get("strong_deteriorating", [])

        if pos or neg:
            lines.append("FUNDAMENTAL MOMENTUM  (strong trends from quarterly results)")
            if pos:
                lines.append("\n  ▲ IMPROVING")
                for t in pos[:10]:
                    acc = f"  [{t['acceleration']}]" if t["acceleration"] != "steady" else ""
                    lines.append(
                        f"    {t['ticker']:<12} {t['metric']:<22} "
                        f"Δ{t['delta_pct']:+6.1f}%  {t['streak']}q streak  "
                        f"momentum={t['momentum']:+.2f}{acc}"
                    )
            if neg:
                lines.append("\n  ▼ DETERIORATING")
                for t in neg[:10]:
                    acc = f"  [{t['acceleration']}]" if t["acceleration"] != "steady" else ""
                    lines.append(
                        f"    {t['ticker']:<12} {t['metric']:<22} "
                        f"Δ{t['delta_pct']:+6.1f}%  {t['streak']}q streak  "
                        f"momentum={t['momentum']:+.2f}{acc}"
                    )
            lines.append("")

        # Inflection points
        inf = brief.get("inflections", [])
        if inf:
            lines.append("INFLECTION POINTS  (trend just reversed this quarter)")
            for t in inf[:8]:
                icon = "▲" if t["direction"] == "improving" else "▼"
                lines.append(
                    f"  {icon} {t['ticker']:<12} {t['metric']:<22} "
                    f"→ {t['direction'].upper()}  Δ{t['delta_pct']:+.1f}%"
                )
            lines.append("")

        # Sector pulse
        sp = brief.get("sector_pulse", {})
        if sp:
            lines.append("SECTOR PULSE")
            for sec, v in sorted(sp.items()):
                if v["improving"] + v["deteriorating"] == 0:
                    continue
                bar_up   = "█" * min(v["improving"], 10)
                bar_down = "█" * min(v["deteriorating"], 10)
                lines.append(
                    f"  {sec:<12}  ▲{v['improving']:>3} {bar_up:<10}  "
                    f"▼{v['deteriorating']:>3} {bar_down}"
                )
            lines.append("")

        # Recent events
        evts = brief.get("recent_events", [])
        if evts:
            lines.append("RECENT CORPORATE EVENTS")
            for e in evts[:8]:
                lines.append(f"  {e['ticker']:<12} {e['event']}")
            lines.append("")

        # Footer
        g = brief.get("graph", {})
        s = brief.get("summary", {})
        lines.append(
            f"[ Graph: {g.get('nodes',0):,} nodes · {g.get('edges',0):,} edges · "
            f"{s.get('total_trends',0)} trends · "
            f"{s.get('strong_positive',0)} strong↑ · "
            f"{s.get('strong_negative',0)} strong↓ ]"
        )
        lines.append(f"{'='*64}\n")
        return "\n".join(lines)

    # ── Persistence ─────────────────────────────────────────────────────────

    def _save(self, brief: dict, as_of: date):
        out_dir = REPO / "reports" / "daily"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"alert-{as_of.isoformat()}.json"
        out.write_text(json.dumps(brief, indent=2, default=str))
        print(f"[AlertAgent] brief saved → {out}")
