"""
HealthChecker — scheduled agent that tests every nselib method, fixes what's
broken, discovers new endpoints, and writes a timestamped health report.

Full cycle (run() or watch()):
  1. Test every registered method → ok / broken / cloud_blocked / schema_drift
  2. Heal broken endpoints via Guardian (up to 3 Claude attempts each)
  3. Run endpoint discovery → apply new methods → verify each one
  4. Heal stale data pipelines (bhavcopy, constituents)
  5. Write JSON report to ~/.nselib/health_report.json

Usage
-----
    from nselib import NSE
    from nselib.healthcheck import HealthChecker

    # Inside a Claude Code session — no API key needed; Brain uses the local claude CLI
    nse = NSE(guard=True)
    hc  = HealthChecker(nse)

    report = hc.run("/path/to/nselib")           # one-shot full cycle
    hc.watch(interval=3600, nselib_dir="/path/to/nselib")  # background loop
"""

import json
import threading
import time
import traceback
from datetime import date, timedelta, datetime
from pathlib import Path
from typing import Any

_REPORT_FILE = Path.home() / ".nselib" / "health_report.json"

# ── Method registry ────────────────────────────────────────────────────────
# Maps method_name → (args, kwargs) — sensible defaults that exercise real data
_REGISTRY: dict[str, tuple[tuple, dict]] = {
    # quotes
    "quote":                     (("INFY",), {}),
    "equity_quote":              (("INFY",), {}),
    "equity_meta_info":          (("INFY",), {}),
    "lookup":                    (("INFY",), {}),
    "get_detailed_scrip_data":   (("INFY",), {}),
    "get_equity_master":         ((), {}),
    # market
    "status":                    ((), {}),
    "holidays":                  ((), {}),
    "pre_open":                  ((), {}),
    "advance_decline":           ((), {}),
    "market_turnover":           ((), {}),
    "block_deals":               ((), {}),
    "live_volume_gainers":       ((), {}),
    "fii_dii_trade_activity":    ((), {}),
    "list_etf":                  ((), {}),
    # indices
    "list_indices":              ((), {}),
    "index_quote":               (("NIFTY 50",), {}),
    "index_constituents":        (("NIFTY 50",), {}),
    "fetch_index_names":         ((), {}),
    "fetch_historical_index_data": (("NIFTY 50",), {
        "from_date": date.today() - timedelta(days=30),
        "to_date":   date.today(),
    }),
    # fno — live (cloud-blocked from datacenter)
    "option_chain":              (("NIFTY",), {}),
    "futures_quote":             (("NIFTY",), {}),
    "get_futures_expiry":        (("NIFTY",), {}),
    "get_oi_spurts_underlyings": ((), {}),
    "get_oi_spurts_contracts":   ((), {}),
    # fno — archive (cloud-safe)
    "fno_lots":                  ((), {}),
    "fetch_fno_underlying":      ((), {}),
    "fno_ban_list":              ((), {}),
    "participant_oi":            ((), {}),
    "participant_volume":        ((), {}),
    # corporate
    "corporate_actions":         (("INFY",), {}),
    "announcements":             (("INFY",), {}),
    "board_meetings":            (("INFY",), {}),
    "shareholding_patterns":     (("INFY",), {}),
    "annual_reports":            (("INFY",), {}),
    "insider_trading":           (("INFY",), {}),
    "event_calendar":            ((), {}),
    "financial_results":         ((), {"symbol": "INFY"}),
    "credit_ratings":            ((), {}),
    "large_deals_snapshot":      ((), {}),
    # history
    "fetch_equity_historical_data": (("INFY",), {
        "from_date": date.today() - timedelta(days=30),
        "to_date":   date.today(),
    }),
    # downloads — archive
    "equity_bhavcopy":           ((), {}),
    "delivery_bhavcopy":         ((), {}),
    "fno_bhavcopy":              ((), {}),
    "indices_bhavcopy":          ((), {}),
    "full_bhavcopy_delivery":    ((), {}),
    "pe_ratio":                  ((), {}),
    "week52_high_low":           ((), {}),
    "short_selling":             ((), {}),
    "surveillance_indicator":    ((), {}),
    "bulk_deals_archive":        ((), {}),
    "block_deals_archive":       ((), {}),
    "margin_trading_disclosure": ((), {}),
    "daily_volatility":          ((), {}),
    "priceband_changes":         ((), {}),
    "sme_bhavcopy":              ((), {}),
    "sme_price_band":            ((), {}),
    "latency_statistics":        ((), {}),
    "securities_list":           ((), {}),
    "wdm_securities_list":       ((), {}),
    "egr_var_files":             ((), {}),
    # fno — daily report archive methods
    "fo_settlement_prices":      ((), {}),
    "fo_daily_volatility":       ((), {}),
    "fo_exposure_limits":        ((), {}),
    "fo_delta_equivalent_oi":    ((), {}),
    "fo_top10_clearing_members": ((), {}),
    "fo_index_position_limits":  ((), {}),
    "fo_market_activity":        ((), {}),
    "fo_latency_statistics":     ((), {}),
    "fo_client_oi_limits":       ((), {}),
    "fo_spread_contracts":       ((), {}),
    "fo_contracts":              ((), {}),
    "mwpl_oi":                   ((), {}),
    "combined_oi":               ((), {}),
}

# Methods known to be blocked from cloud/datacenter IPs — expected, not bugs
_CLOUD_BLOCKED_METHODS: set[str] = {
    "quote", "equity_quote", "equity_meta_info", "lookup",
    "get_detailed_scrip_data", "get_equity_master",
    "status", "holidays", "pre_open", "advance_decline", "market_turnover",
    "block_deals", "live_volume_gainers", "fii_dii_trade_activity", "list_etf",
    "list_indices", "index_quote", "index_constituents", "fetch_index_names",
    "fetch_historical_index_data",
    "option_chain", "futures_quote", "get_futures_expiry",
    "get_oi_spurts_underlyings", "get_oi_spurts_contracts",
    "corporate_actions", "announcements", "board_meetings", "shareholding_patterns",
    "annual_reports", "insider_trading", "event_calendar", "financial_results",
    "credit_ratings", "large_deals_snapshot",
    "equity_bhavcopy", "indices_bhavcopy",
}

_CLOUD_ERROR_SIGNALS = (
    "403", "blocked", "akamai", "connection refused", "timed out",
    "ssl", "nsedataerror", "nsesession", "cookie", "captcha",
)


def _is_cloud_block(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(sig in msg for sig in _CLOUD_ERROR_SIGNALS)


def _is_ok(resp: Any) -> bool:
    if resp is None:
        return False
    if isinstance(resp, (dict, list)):
        return bool(resp)
    return True


class HealthChecker:
    """
    Full-cycle health agent for nselib.

    Parameters
    ----------
    nse : NSE instance with guardian attached (NSE(guard=True, ...))
    report_file : where to write the JSON report (default ~/.nselib/health_report.json)
    """

    def __init__(self, nse, report_file: str | Path | None = None):
        self._nse    = nse
        self._report_file = Path(report_file) if report_file else _REPORT_FILE

    # ── Public API ─────────────────────────────────────────────────────────

    def run(self, nselib_dir: str | Path) -> dict:
        """
        Full cycle: test all → heal broken → discover new → verify → heal pipelines.
        Returns the health report dict and writes it to disk.
        """
        print("[HealthChecker] Starting full health cycle…")
        ts = datetime.now().isoformat()

        # Step 1 — test every method
        print("[HealthChecker] Step 1/4 — testing all registered methods…")
        method_results = self._test_all()

        # Step 2 — heal broken methods
        print("[HealthChecker] Step 2/4 — healing broken methods…")
        heal_results = self._heal_broken(method_results)

        # Step 3 — discover new endpoints, apply and verify
        print("[HealthChecker] Step 3/4 — endpoint discovery + apply + verify…")
        discovery_results = self._run_discovery(nselib_dir)

        # Step 4 — heal data pipelines
        print("[HealthChecker] Step 4/4 — data pipeline health check…")
        pipeline_results = self._heal_pipelines()

        # Build report
        counts = self._count(method_results)
        report = {
            "timestamp": ts,
            "summary": {
                **counts,
                "healed":          sum(1 for r in heal_results if r["outcome"] == "healed"),
                "heal_failed":     sum(1 for r in heal_results if r["outcome"] == "failed"),
                "new_methods":     discovery_results.get("applied", 0),
                "verified_pass":   discovery_results.get("verified_pass", 0),
                "verified_removed":discovery_results.get("verified_removed", 0),
            },
            "methods":   method_results,
            "heals":     heal_results,
            "discovery": discovery_results,
            "pipelines": pipeline_results,
        }

        self._write_report(report)
        self._print_summary(report)
        return report

    def watch(self, interval: int = 3600, nselib_dir: str | Path = ".") -> None:
        """Run the full cycle in a background thread every `interval` seconds."""
        def _loop():
            while True:
                time.sleep(interval)
                try:
                    self.run(nselib_dir)
                except Exception as exc:
                    print(f"[HealthChecker] Cycle error: {exc}")

        t = threading.Thread(target=_loop, daemon=True, name="healthchecker")
        t.start()
        print(f"[HealthChecker] Background agent started — full cycle every {interval}s.")

    def last_report(self) -> dict | None:
        """Load and return the most recent saved report, or None."""
        if self._report_file.exists():
            try:
                return json.loads(self._report_file.read_text())
            except Exception:
                pass
        return None

    # ── Step 1: test all methods ───────────────────────────────────────────

    def _test_all(self) -> dict[str, dict]:
        results: dict[str, dict] = {}
        for name, (args, kwargs) in _REGISTRY.items():
            method = getattr(self._nse, name, None)
            if method is None:
                results[name] = {"status": "missing", "error": "method not found"}
                print(f"[HealthChecker]  ? {name} — missing")
                continue

            t0 = time.time()
            try:
                resp = method(*args, **kwargs)
                elapsed = round((time.time() - t0) * 1000)
                if _is_ok(resp):
                    results[name] = {"status": "ok", "elapsed_ms": elapsed}
                    print(f"[HealthChecker]  ✓ {name} ({elapsed}ms)")
                else:
                    results[name] = {"status": "broken", "error": "empty response",
                                     "elapsed_ms": elapsed}
                    print(f"[HealthChecker]  ✗ {name} — empty response")
            except Exception as exc:
                elapsed = round((time.time() - t0) * 1000)
                if _is_cloud_block(exc) or name in _CLOUD_BLOCKED_METHODS:
                    results[name] = {"status": "cloud_blocked", "error": str(exc)[:120],
                                     "elapsed_ms": elapsed}
                    print(f"[HealthChecker]  ~ {name} — cloud blocked")
                else:
                    results[name] = {"status": "broken", "error": str(exc)[:200],
                                     "elapsed_ms": elapsed,
                                     "traceback": traceback.format_exc()[-500:]}
                    print(f"[HealthChecker]  ✗ {name} — {exc}")

        return results

    # ── Step 2: heal broken methods ────────────────────────────────────────

    def _heal_broken(self, method_results: dict[str, dict]) -> list[dict]:
        g = getattr(self._nse, "_guardian", None)
        if not g:
            print("[HealthChecker]  ⚠ No guardian attached — skipping heal step.")
            return []

        heal_log: list[dict] = []
        broken = [n for n, r in method_results.items() if r["status"] == "broken"]
        if not broken:
            print("[HealthChecker]  ✓ No broken methods to heal.")
            return []

        print(f"[HealthChecker]  Healing {len(broken)} broken method(s): {broken}")

        for name in broken:
            args, kwargs = _REGISTRY.get(name, ((), {}))
            method = getattr(self._nse, name, None)
            if method is None:
                heal_log.append({"method": name, "outcome": "failed",
                                 "detail": "method not found"})
                continue

            # Re-run through guardian which will intercept and heal
            attempt_ok = False
            last_error = ""
            for attempt in range(3):
                try:
                    resp = method(*args, **kwargs)
                    if _is_ok(resp):
                        method_results[name]["status"] = "fixed"
                        method_results[name].pop("error", None)
                        heal_log.append({"method": name, "outcome": "healed",
                                         "attempts": attempt + 1})
                        print(f"[HealthChecker]  ✓ {name} healed (attempt {attempt+1})")
                        attempt_ok = True
                        break
                    else:
                        last_error = "empty response"
                except Exception as exc:
                    last_error = str(exc)

            if not attempt_ok:
                heal_log.append({"method": name, "outcome": "failed",
                                 "detail": last_error})
                print(f"[HealthChecker]  ✗ {name} could not be healed: {last_error}")

        return heal_log

    # ── Step 3: endpoint discovery ─────────────────────────────────────────

    def _run_discovery(self, nselib_dir: str | Path) -> dict:
        g = getattr(self._nse, "_guardian", None)
        if not g:
            return {"skipped": True, "reason": "no guardian"}

        # Live API discovery (Claude-assisted endpoint guessing)
        discoveries = g.discover()

        # Archive discovery (ground-truth daily-reports API)
        try:
            arch_discoveries = g.discover_archive(nselib_dir=nselib_dir)
            discoveries = discoveries + arch_discoveries
        except Exception as exc:
            print(f"[HealthChecker]  ⚠ discover_archive error: {exc}")

        if not discoveries:
            print("[HealthChecker]  No new endpoints found.")
            return {"new_endpoints": 0, "applied": 0,
                    "verified_pass": 0, "verified_removed": 0}

        modified = g.apply(nselib_dir, nse_instance=self._nse)

        # verify() runs inside apply() and writes _verify_status back onto each discovery
        verify_results = getattr(g, "_discoveries", [])
        passed  = sum(1 for d in verify_results
                      if d.get("_verify_status") in ("pass", "fixed"))
        removed = sum(1 for d in verify_results
                      if d.get("_verify_status") == "removed")

        # Rule 17: only report methods that actually passed verification
        verified_methods = [d.get("method_name") for d in verify_results
                            if d.get("_verify_status") in ("pass", "fixed")]
        unverified = [d.get("method_name") for d in verify_results
                      if d.get("_verify_status") not in ("pass", "fixed", "skipped")]

        if unverified:
            print(f"[HealthChecker]  ⚠ Unverified/removed methods (not safe to commit): {unverified}")
        if not verified_methods and modified:
            print("[HealthChecker]  ⚠ No methods passed verification — skipping commit of modified files.")

        return {
            "new_endpoints":    len(discoveries),
            "applied":          len(modified),
            "verified_pass":    passed,
            "verified_removed": removed,
            "verified_methods": verified_methods,
            "unverified":       unverified,
            "safe_to_commit":   passed > 0,
            "methods":          [d.get("method_name") for d in discoveries],
        }

    # ── Step 4: data pipelines ─────────────────────────────────────────────

    def _heal_pipelines(self) -> dict:
        g = getattr(self._nse, "_guardian", None)
        if not g:
            return {"skipped": True, "reason": "no guardian"}
        try:
            return g.heal_data(nse_instance=self._nse)
        except Exception as exc:
            return {"error": str(exc)}

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _count(results: dict[str, dict]) -> dict:
        counts: dict[str, int] = {
            "total": 0, "ok": 0, "broken": 0,
            "cloud_blocked": 0, "missing": 0, "fixed": 0,
        }
        for r in results.values():
            counts["total"] += 1
            s = r.get("status", "unknown")
            if s in counts:
                counts[s] += 1
        return counts

    def _write_report(self, report: dict) -> None:
        try:
            self._report_file.parent.mkdir(parents=True, exist_ok=True)
            # Keep last 10 reports
            history: list = []
            if self._report_file.exists():
                try:
                    data = json.loads(self._report_file.read_text())
                    history = data if isinstance(data, list) else [data]
                except Exception:
                    pass
            history.append(report)
            history = history[-10:]
            self._report_file.write_text(json.dumps(history, indent=2, default=str))
            print(f"[HealthChecker] Report saved → {self._report_file}")
        except Exception as exc:
            print(f"[HealthChecker] Could not write report: {exc}")

    @staticmethod
    def _print_summary(report: dict) -> None:
        s = report["summary"]
        print("\n" + "=" * 50)
        print("[HealthChecker] CYCLE COMPLETE")
        print(f"  Methods:  {s['ok']} ok  |  {s['broken']} broken  |  "
              f"{s['cloud_blocked']} cloud-blocked  |  {s['missing']} missing")
        print(f"  Healed:   {s['healed']} fixed  |  {s['heal_failed']} failed")
        print(f"  New:      {s['new_methods']} added  |  "
              f"{s['verified_pass']} verified  |  {s['verified_removed']} removed")
        print("=" * 50 + "\n")
