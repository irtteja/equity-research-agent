"""
PriceAgent — pulls live daily prices for all tracked stocks and writes
PriceSnapshot nodes to the knowledge graph.

Reads:
  memory_store/kb_build_progress.json — full symbol universe
  NSE live data via nselib (bulk index_constituents calls: Nifty 500 + Microcap 250 + Smallcap 250)

Writes:
  PriceSnapshot nodes  — close, 52w high/low, price_zone, value_signal flag
  PRICE_OF edges       — PriceSnapshot → Company node
  memory_store/prices/YYYY-MM-DD.json — daily summary

A PriceSnapshot is marked value_signal=True when:
  - TrendSummary for the ticker shows direction=improving, momentum >= 0.6
  - AND price_zone == 'near_low' (stock within 10% of 52-week low)

Run: python jobs/price_agent_runner.py
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "vendor"))  # vendored nselib fallback

NEAR_THRESHOLD = 0.10   # within 10% of 52w high or low counts as "near"
MOMENTUM_MIN   = 0.6    # minimum momentum to flag as value signal

GROWTH_METRICS = {
    "revenue", "revenue_from_operations", "total_income", "pat", "pbt",
    "ebit", "ebitda", "eps", "eps_basic", "eps_diluted",
    "operating_profit", "gross_profit", "net_profit",
    "total_comprehensive_income",
}


class PriceAgent:
    """Fetches live prices and writes PriceSnapshot nodes to the graph."""

    def __init__(self):
        from memory.graph_store import get_store
        from nselib import NSE
        self.gs  = get_store()
        self.nse = NSE()

    # ── Public interface ────────────────────────────────────────────────────

    def run(self, as_of: date | None = None) -> dict:
        as_of = as_of or date.today()

        universe   = self._load_universe()
        prices     = self._fetch_prices(universe)
        trend_map  = self._load_trend_signals()
        stats      = self._write_to_graph(prices, trend_map, as_of)
        self._save_summary(prices, stats, as_of)
        return stats

    # ── Data loading ────────────────────────────────────────────────────────

    def _load_universe(self) -> set[str]:
        prog = REPO / "memory_store" / "kb_build_progress.json"
        if not prog.exists():
            return set()
        data = json.loads(prog.read_text())
        return {s.upper() for s, v in data.items() if isinstance(v, dict) and v.get("status") == "done"}

    # Broad indices first so early calls catch the most new symbols
    _PRIORITY = {
        "NIFTY TOTAL MARKET": 0, "NIFTY 500": 1,
        "NIFTY MICROCAP 250": 2, "NIFTY SMALLCAP 250": 3,
        "NIFTY 200": 4, "NIFTY 100": 5, "NIFTY 50": 6,
    }

    def _load_all_indices(self) -> list[str]:
        """Return all NSE mainboard equity index names (live from API, excluding SME/fixed-income)."""
        try:
            resp = self.nse.list_indices()
            data = resp.get("data", resp) if isinstance(resp, dict) else resp
            names = [
                d["index"] for d in data
                if d.get("key") != "FIXED INCOME INDICES"
                and "SME" not in d.get("index", "").upper()
                and "EMERGE" not in d.get("index", "").upper()
            ]
            if names:
                return sorted(names, key=lambda x: (self._PRIORITY.get(x, 99), x))
        except Exception as e:
            print(f"[PriceAgent] list_indices failed ({e}), falling back to cached list")
        # Fallback: cached index names from symbol_to_indices.json
        path = REPO / "data" / "symbol_to_indices.json"
        if path.exists():
            cached: set[str] = set()
            for idx_list in json.loads(path.read_text()).values():
                cached.update(idx_list)
            return sorted(cached, key=lambda x: (self._PRIORITY.get(x, 99), x))
        return ["NIFTY 500", "NIFTY MICROCAP 250", "NIFTY SMALLCAP 250"]

    def _fetch_prices(self, universe: set[str]) -> dict[str, dict]:
        """Fetch live prices — all tracked indices, deduplicate by symbol, fallback for gaps."""
        prices: dict[str, dict] = {}
        all_indices = self._load_all_indices()

        for index_name in all_indices:
            try:
                members = self.nse.index_constituents(index_name)
                before = len(prices)
                for m in members:
                    sym = (m.get("symbol") or "").upper()
                    if not sym or sym in prices:
                        continue
                    try:
                        prices[sym] = {
                            "close":      float(m.get("lastPrice") or 0),
                            "prev_close": float(m.get("previousClose") or 0),
                            "pct_change": float(m.get("pChange") or 0),
                            "year_high":  float(m.get("yearHigh") or 0),
                            "year_low":   float(m.get("yearLow") or 0),
                        }
                    except (TypeError, ValueError):
                        continue
                added = len(prices) - before
                if added:
                    print(f"[PriceAgent] {index_name}: +{added} new ({len(prices)} total)")
            except Exception as e:
                print(f"[PriceAgent] {index_name} failed: {e}")

        # Per-symbol fallback for any stocks still missing (sector-only names)
        missing = universe - set(prices.keys())
        if missing:
            print(f"[PriceAgent] per-symbol fallback for {len(missing)} remaining stocks")
        fallback_ok = 0
        for sym in sorted(missing):
            try:
                q = self.nse.equity_quote(sym)
                if q and q.get("close"):
                    prices[sym] = {
                        "close":      float(q.get("close") or 0),
                        "prev_close": float(q.get("prev_close") or 0),
                        "pct_change": float(q.get("pct_change") or 0),
                        "year_high":  float(q.get("year_high") or 0),
                        "year_low":   float(q.get("year_low") or 0),
                    }
                    fallback_ok += 1
            except Exception:
                continue

        if fallback_ok:
            print(f"[PriceAgent] fallback fetch: {fallback_ok} more stocks")

        return prices

    def _load_trend_signals(self) -> dict[str, dict]:
        """Load TrendSummary nodes — keyed by ticker, best improving signal."""
        with self.gs._conn() as conn:
            rows = conn.execute(
                "SELECT ticker, properties FROM nodes WHERE node_type='TrendSummary'"
            ).fetchall()
        signals: dict[str, dict] = {}
        for row in rows:
            try:
                props = json.loads(row["properties"]) if isinstance(row["properties"], str) else row["properties"]
                ticker = (row["ticker"] or "").upper()
                if not ticker:
                    continue
                momentum = float(props.get("momentum_score", 0))
                direction = props.get("direction", "stable")
                # Keep best improving signal per ticker — growth metrics only, streak ≥ 2
                metric = props.get("metric", "").lower().replace("-", "_")
                streak = int(props.get("consistent_periods", 1))
                if (direction == "improving" and momentum >= MOMENTUM_MIN
                        and streak >= 2 and metric in GROWTH_METRICS):
                    existing = signals.get(ticker)
                    if not existing or momentum > float(existing.get("momentum_score", 0)):
                        signals[ticker] = props
            except Exception:
                continue
        return signals

    # ── Graph writing ───────────────────────────────────────────────────────

    def _write_to_graph(self, prices: dict, trend_map: dict, as_of: date) -> dict:
        date_str  = as_of.isoformat()
        written   = 0
        value_sig = 0

        for sym, p in prices.items():
            close     = p["close"]
            yr_high   = p["year_high"]
            yr_low    = p["year_low"]

            if close <= 0:
                continue

            # Price zone
            near_high_pct = ((yr_high - close) / yr_high * 100) if yr_high > 0 else None
            near_low_pct  = ((close - yr_low)  / yr_low  * 100) if yr_low  > 0 else None

            if near_high_pct is not None and near_high_pct <= NEAR_THRESHOLD * 100:
                price_zone = "near_high"
            elif near_low_pct is not None and near_low_pct <= NEAR_THRESHOLD * 100:
                price_zone = "near_low"
            else:
                price_zone = "mid_range"

            # Value signal: fundamentals improving + price near 52w low
            is_value = price_zone == "near_low" and sym in trend_map
            if is_value:
                value_sig += 1

            node_id = f"PRICE_{sym}_{date_str}"
            props = {
                "ticker":          sym,
                "date":            date_str,
                "close":           close,
                "prev_close":      p["prev_close"],
                "pct_change":      p["pct_change"],
                "year_high":       yr_high,
                "year_low":        yr_low,
                "near_52w_high_pct": round(near_high_pct, 2) if near_high_pct is not None else None,
                "near_52w_low_pct":  round(near_low_pct, 2)  if near_low_pct  is not None else None,
                "price_zone":      price_zone,
                "value_signal":    is_value,
            }
            if is_value:
                ts = trend_map[sym]
                props["trend_metric"]    = ts.get("metric")
                props["trend_momentum"]  = ts.get("momentum_score")
                props["trend_streak"]    = ts.get("consistent_periods")
                props["trend_delta_pct"] = ts.get("latest_delta_pct")

            self.gs.add_node(
                node_id=node_id,
                node_type="PriceSnapshot",
                name=f"{sym} price {date_str}",
                ticker=sym,
                properties=props,
            )

            # Link to Company node
            company_id = f"COMPANY_{sym}"
            self.gs.add_edge(
                edge_type="PRICE_OF",
                source_id=node_id,
                target_id=company_id,
                properties={"date": date_str},
            )
            written += 1

        print(f"[PriceAgent] wrote {written} PriceSnapshot nodes, {value_sig} value signals")
        return {"prices_written": written, "value_signals": value_sig, "date": date_str}

    # ── Persistence ─────────────────────────────────────────────────────────

    def _save_summary(self, prices: dict, stats: dict, as_of: date):
        out_dir = REPO / "memory_store" / "prices"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Include value signals in summary
        with self.gs._conn() as conn:
            value_rows = conn.execute(
                "SELECT ticker, properties FROM nodes "
                "WHERE node_type='PriceSnapshot' AND json_extract(properties,'$.value_signal')=1 "
                "AND json_extract(properties,'$.date')=?",
                (as_of.isoformat(),),
            ).fetchall()

        value_signals = []
        for row in value_rows:
            try:
                props = json.loads(row["properties"]) if isinstance(row["properties"], str) else row["properties"]
                value_signals.append({
                    "ticker":       row["ticker"],
                    "close":        props.get("close"),
                    "near_low_pct": props.get("near_52w_low_pct"),
                    "trend_metric": props.get("trend_metric"),
                    "trend_delta":  props.get("trend_delta_pct"),
                    "streak":       props.get("trend_streak"),
                    "price_zone":   props.get("price_zone"),
                })
            except Exception:
                continue

        summary = {
            "date":          as_of.isoformat(),
            "generated_at":  datetime.now().isoformat(),
            "stats":         stats,
            "value_signals": sorted(value_signals, key=lambda x: x.get("near_low_pct") or 99),
            "movers_up":   sorted(
                [{"ticker": s, "pct": p["pct_change"]} for s, p in prices.items() if p["pct_change"] >= 3],
                key=lambda x: -x["pct"]
            )[:20],
            "movers_down": sorted(
                [{"ticker": s, "pct": p["pct_change"]} for s, p in prices.items() if p["pct_change"] <= -3],
                key=lambda x: x["pct"]
            )[:20],
        }
        out = out_dir / f"{as_of.isoformat()}.json"
        out.write_text(json.dumps(summary, indent=2, default=str))
        print(f"[PriceAgent] summary saved → {out}")
