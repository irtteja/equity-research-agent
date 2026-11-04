"""
SwarmOrchestrator — unified coordinator for all agent swarms.

Runs the full research/analysis/ingestion/knowledge cycle in 4 phases:
  Phase 1 — Ingestion:   price, announcements, corp events, new sources
  Phase 2 — Analysis:    context enrichment, trend analysis, document processing
  Phase 3 — Synthesis:   analyst synthesis, sector analysis, alert evaluation
  Phase 4 — Meta:        coverage rebuild, meta-cognitive observation, tool synthesis

Each phase runs its agents in parallel via ThreadPoolExecutor.
After Phase 4 the meta-cognitive loop enqueues new gap-fill work,
so the next run automatically covers what was missed.

No external API calls. All work dispatched through work_queue.

Usage:
  python jobs/orchestrator.py --once                  # one full cycle
  python jobs/orchestrator.py --phase ingestion       # one phase only
  python jobs/orchestrator.py --continuous --hours 24 # daemon loop
  python jobs/orchestrator.py --status                # print queue depths + perf
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _log(phase: str, msg: str):
    print(f"[{_ts()}] [{phase:12s}] {msg}", flush=True)


def _record_perf(agent_id: str, ok: int, fail: int, dur: float, queue: str = ""):
    try:
        from memory.agent_perf import record_run
        record_run(agent_id, ok, fail, dur, queue)
    except Exception:
        pass


# ── Phase 1: Ingestion ────────────────────────────────────────────────────────

def run_ingestion_phase(verbose: bool = True) -> dict:
    """
    Ingest fresh data from NSE: prices, announcements, corp events, new sources.
    All ingestion is opportunistic — failures are logged, not fatal.
    """
    t0 = time.time()
    results = {}

    # Corp events (announcements + board meetings)
    try:
        _log("ingestion", "fetch_corp_events...")
        import jobs.fetch_corp_events as fce
        r = fce.run_all(save_to_graph=True)
        results["corp_events"] = r
        ok   = r.get("announcements_saved", 0) + r.get("board_meetings_saved", 0)
        fail = r.get("errors", 0)
        _record_perf("corp_event_fetcher", ok, fail, time.time()-t0, "ingestion")
        if verbose:
            _log("ingestion", f"corp_events: {ok} nodes written")
    except Exception as e:
        results["corp_events"] = {"error": str(e)}
        _log("ingestion", f"corp_events FAILED: {e}")

    # New data sources (delivery, insider, pledge, etc.)
    try:
        _log("ingestion", "ingest_new_sources...")
        t1 = time.time()
        import jobs.ingest_new_sources as ins
        r = ins.run_all()
        results["new_sources"] = r
        ok = sum(v.get("saved", 0) for v in r.values() if isinstance(v, dict))
        _record_perf("new_sources_ingester", ok, 0, time.time()-t1, "ingestion")
        if verbose:
            _log("ingestion", f"new_sources: {ok} total nodes written")
    except Exception as e:
        results["new_sources"] = {"error": str(e)}
        _log("ingestion", f"new_sources FAILED: {e}")

    results["duration_s"] = round(time.time() - t0, 1)
    return results


# ── Phase 2: Analysis ─────────────────────────────────────────────────────────

def run_analysis_phase(workers: int = 4, verbose: bool = True) -> dict:
    """
    Drain analysis work queues: context enrichment, trend, document.
    Uses work_queue → claim_batch → worker threads.
    """
    from jobs.swarm_runner import SwarmRunner
    t0 = time.time()
    runner = SwarmRunner()
    results = {}

    queues = [
        ("context_queue",  workers),
        ("trend_queue",    workers),
        ("document_queue", 2),
    ]

    for queue_name, w in queues:
        try:
            _log("analysis", f"draining {queue_name} ({w} workers)...")
            t1 = time.time()
            r = runner.run_queue(queue_name, batch_size=w * 5)
            results[queue_name] = r
            ok   = r.get("processed", 0)
            fail = r.get("errors",    0)
            _record_perf(f"swarm_{queue_name}", ok, fail, time.time()-t1, queue_name)
            if verbose:
                _log("analysis", f"{queue_name}: {ok} ok, {fail} fail")
        except Exception as e:
            results[queue_name] = {"error": str(e)}
            _log("analysis", f"{queue_name} FAILED: {e}")

    # Technical analysis (RSI, EMA, MACD, 52W position)
    try:
        _log("analysis", "technical_agent (signal tickers)...")
        t1 = time.time()
        from agents.technical_agent import TechnicalAgent
        ta = TechnicalAgent()
        r = ta.run_all(limit=200)
        _record_perf("technical_agent_v1", r.get("ok", 0), r.get("error", 0), time.time()-t1, "analysis")
        results["technical"] = r
        if verbose:
            _log("analysis", f"technical: {r.get('ok')} ok, signals={r.get('signals')}")
    except Exception as e:
        results["technical"] = {"error": str(e)}
        _log("analysis", f"technical_agent FAILED: {e}")

    # Valuation scoring (P/E, growth, margins)
    try:
        _log("analysis", "valuation_agent (signal tickers)...")
        t1 = time.time()
        from agents.valuation_agent import ValuationAgent
        va = ValuationAgent()
        r = va.run_all(limit=200)
        _record_perf("valuation_agent_v1", r.get("ok", 0), r.get("error", 0), time.time()-t1, "analysis")
        results["valuation"] = r
        if verbose:
            _log("analysis", f"valuation: {r.get('ok')} ok, verdicts={r.get('verdicts')}")
    except Exception as e:
        results["valuation"] = {"error": str(e)}
        _log("analysis", f"valuation_agent FAILED: {e}")

    results["duration_s"] = round(time.time() - t0, 1)
    return results


# ── Phase 3: Synthesis ────────────────────────────────────────────────────────

def run_synthesis_phase(verbose: bool = True) -> dict:
    """
    Run analyst synthesis + sector analysis.
    """
    t0 = time.time()
    results = {}

    # Analyst agent on top signal tickers
    try:
        _log("synthesis", "analyst_synthesizer (top 50 signals)...")
        t1 = time.time()
        from agents.analyst_agent import AnalystAgent
        agent = AnalystAgent()
        tickers = agent._tickers_with_signals(50)
        if tickers:
            r = agent.bulk_analyze(tickers)
            ok   = sum(1 for x in r if "recommendation" in x)
            fail = len(r) - ok
            _record_perf("analyst_synthesizer_v1", ok, fail, time.time()-t1, "analyst_queue")
            results["analyst"] = {"ok": ok, "fail": fail}
            if verbose:
                _log("synthesis", f"analyst: {ok} tickers synthesised")
        else:
            results["analyst"] = {"ok": 0, "note": "no signal tickers"}
    except Exception as e:
        results["analyst"] = {"error": str(e)}
        _log("synthesis", f"analyst FAILED: {e}")

    # Sector analysis
    try:
        _log("synthesis", "sector_analyst...")
        t1 = time.time()
        from agents.sector_analyst import SectorAnalyst
        sa = SectorAnalyst()
        r = sa.analyze_all_sectors()
        _record_perf("sector_analyst_v1", len(r), 0, time.time()-t1, "synthesis")
        results["sectors"] = {"analyzed": len(r)}
        if verbose:
            _log("synthesis", f"sectors: {len(r)} analyzed")
    except Exception as e:
        results["sectors"] = {"error": str(e)}
        _log("synthesis", f"sector_analyst FAILED: {e}")

    # Alert agent — belief_log state changes + stale signals
    try:
        _log("synthesis", "alert_agent (last 3h)...")
        t1 = time.time()
        from agents.alert_agent import AlertAgent
        aa = AlertAgent()
        r = aa.run(hours_back=3)
        _record_perf("alert_agent_v1", r.get("alerts_written", 0), 0, time.time()-t1, "synthesis")
        results["alerts"] = r
        if verbose:
            _log("synthesis", (
                f"alerts: {r.get('alerts_generated',0)} generated "
                f"critical={r.get('critical',0)} warning={r.get('warning',0)}"
            ))
    except Exception as e:
        results["alerts"] = {"error": str(e)}
        _log("synthesis", f"alert_agent FAILED: {e}")

    # Devil's advocate — bear case on top BUY signals
    try:
        _log("synthesis", "devil_advocate (top 30 BUY signals)...")
        t1 = time.time()
        from agents.devil_advocate import DevilAdvocate
        da = DevilAdvocate()
        da_results = da.run_all(limit=30)
        buy_held  = sum(1 for r in da_results if r.get("verdict") == "BUY")
        downgraded = sum(1 for r in da_results if r.get("verdict") in ("HOLD", "WAIT"))
        _record_perf("devil_advocate_v1", buy_held, 0, time.time()-t1, "synthesis")
        results["devil_advocate"] = {
            "analyzed": len(da_results),
            "buy_held": buy_held,
            "downgraded": downgraded,
        }
        if verbose:
            _log("synthesis", f"devil_advocate: {buy_held} BUY held, {downgraded} downgraded")
    except Exception as e:
        results["devil_advocate"] = {"error": str(e)}
        _log("synthesis", f"devil_advocate FAILED: {e}")

    # Risk agent — concentration, quality, liquidity checks
    try:
        _log("synthesis", "risk_agent...")
        t1 = time.time()
        from agents.risk_agent import RiskAgent
        ra = RiskAgent()
        r = ra.run()
        _record_perf("risk_agent_v1", 1, 0, time.time()-t1, "synthesis")
        results["risk"] = {
            "overall_risk":  r.get("overall_risk"),
            "flags_total":   r.get("flags_total"),
            "low_quality":   r.get("signal_quality", {}).get("low_quality", 0),
            "illiquid":      r.get("liquidity", {}).get("flagged", 0),
        }
        if verbose:
            _log("synthesis", (
                f"risk: overall={r.get('overall_risk')} "
                f"flags={r.get('flags_total')} "
                f"low_quality={r.get('signal_quality',{}).get('low_quality',0)}"
            ))
    except Exception as e:
        results["risk"] = {"error": str(e)}
        _log("synthesis", f"risk_agent FAILED: {e}")

    results["duration_s"] = round(time.time() - t0, 1)
    return results


# ── Phase 4: Meta-cognitive ───────────────────────────────────────────────────

def run_meta_phase(synthesize_tools: int = 5, verbose: bool = True) -> dict:
    """
    Coverage rebuild → meta-cognitive observation → gap enqueue → tool synthesis.
    """
    t0 = time.time()
    results = {}

    # Coverage agent — rebuild map
    try:
        _log("meta", "coverage_agent rebuild...")
        t1 = time.time()
        from agents.coverage_agent import CoverageAgent
        ca = CoverageAgent()
        cr = ca.rebuild_full()
        _record_perf("coverage_agent_v1", cr, 0, time.time()-t1, "meta")
        results["coverage_rebuild"] = {"rows": cr}
        if verbose:
            _log("meta", f"coverage: {cr} (ticker, node_type) rows rebuilt")
    except Exception as e:
        results["coverage_rebuild"] = {"error": str(e)}
        _log("meta", f"coverage_agent FAILED: {e}")

    # Meta-cognitive cycle
    try:
        _log("meta", "meta_cognition cycle...")
        t1 = time.time()
        from memory.meta_cognition import run_meta_cycle
        mc = run_meta_cycle(enqueue_work=True, verbose=verbose)
        _record_perf("meta_cognition", 1, len(mc.get("errors", [])), time.time()-t1, "meta")
        results["meta_cognition"] = mc
        if verbose:
            _log("meta", (
                f"gaps={mc['total_gaps']} enqueued={mc['enqueued']} "
                f"unused_nselib={mc['unused_nselib']}"
            ))
    except Exception as e:
        results["meta_cognition"] = {"error": str(e)}
        _log("meta", f"meta_cognition FAILED: {e}")

    # Graph dynamics cycle — learn/relearn/unlearn/prune
    try:
        _log("meta", "graph_dynamics cycle (learn/relearn/unlearn/prune)...")
        from jobs.dynamics_runner import run_cycle as run_dynamics
        t1 = time.time()
        dr = run_dynamics(limit=200, verbose=verbose)
        _record_perf("graph_dynamics_v1", dr.get("tickers_processed", 0),
                     len(dr.get("errors", [])), time.time() - t1, "meta")
        results["graph_dynamics"] = dr
        _log("meta", f"graph_dynamics done: {dr.get('tickers_processed',0)} tickers, "
             f"learned={dr.get('learned',0)} relearned={dr.get('relearned',0)} "
             f"pruned={dr.get('pruned',0)}")
    except Exception as e:
        results["graph_dynamics"] = {"error": str(e)}
        _log("meta", f"graph_dynamics FAILED: {e}")

    # Global macro snapshot (yfinance — no auth)
    try:
        _log("meta", "global_macro snapshot...")
        t1 = time.time()
        from tools.global_macro import run as macro_run
        macro_r = macro_run(verbose=verbose)
        _record_perf("global_macro_v1", 1, 0, time.time()-t1, "meta")
        results["global_macro"] = macro_r.get("signal", {})
        _log("meta", (
            f"macro: signal={macro_r.get('signal',{}).get('signal','?')}  "
            f"score={macro_r.get('signal',{}).get('macro_score',0):.2f}"
        ))
    except Exception as e:
        results["global_macro"] = {"error": str(e)}
        _log("meta", f"global_macro FAILED: {e}")

    # News sentiment scoring from ManagementStatement graph nodes
    try:
        _log("meta", "news_agent sentiment scoring...")
        t1 = time.time()
        from agents.news_agent import run_all as news_run
        news_r = news_run(verbose=verbose)
        scored = [r for r in news_r if "sentiment_score" in r]
        _record_perf("news_agent_v1", len(scored), 0, time.time()-t1, "meta")
        results["news_sentiment"] = {
            "scored": len(scored),
            "bullish": sum(1 for r in scored if r.get("signal") == "bullish"),
            "bearish": sum(1 for r in scored if r.get("signal") == "bearish"),
        }
        _log("meta", (
            f"news: scored={len(scored)}  "
            f"bullish={results['news_sentiment']['bullish']}  "
            f"bearish={results['news_sentiment']['bearish']}"
        ))
    except Exception as e:
        results["news_sentiment"] = {"error": str(e)}
        _log("meta", f"news_agent FAILED: {e}")

    # Conviction snapshot — blends bayesian + sentiment + macro + technical
    try:
        _log("meta", "conviction_tracker daily snapshot...")
        t1 = time.time()
        from memory.conviction_tracker import record_daily_conviction
        conv_r = record_daily_conviction(verbose=verbose)
        ok = [r for r in conv_r if "conviction_score" in r]
        _record_perf("conviction_tracker_v1", len(ok), 0, time.time()-t1, "meta")
        results["conviction"] = {
            "recorded": len(ok),
            "strong_buy": sum(1 for r in ok if r.get("signal") == "strong_buy"),
            "buy": sum(1 for r in ok if r.get("signal") == "buy"),
            "hold": sum(1 for r in ok if r.get("signal") == "hold"),
            "sell": sum(1 for r in ok if r.get("signal") in ("sell", "strong_sell")),
        }
        _log("meta", (
            f"conviction: recorded={len(ok)}  "
            f"strong_buy={results['conviction']['strong_buy']}  "
            f"buy={results['conviction']['buy']}  "
            f"sell={results['conviction']['sell']}"
        ))
    except Exception as e:
        results["conviction"] = {"error": str(e)}
        _log("meta", f"conviction_tracker FAILED: {e}")

    # Tool synthesis — recursive self-improvement
    if synthesize_tools > 0:
        try:
            _log("meta", f"tool_synthesizer (up to {synthesize_tools} new tools)...")
            t1 = time.time()
            from agents.tool_synthesizer import run_synthesis_cycle
            synth = run_synthesis_cycle(max_tools=synthesize_tools, verbose=verbose)
            created = sum(1 for r in synth if r.get("status") == "created")
            _record_perf("tool_synthesizer_v1", created, 0, time.time()-t1, "meta")
            results["tool_synthesis"] = {"created": created, "attempted": len(synth)}
            if verbose:
                _log("meta", f"tool_synthesis: {created} new tools generated")
        except Exception as e:
            results["tool_synthesis"] = {"error": str(e)}
            _log("meta", f"tool_synthesizer FAILED: {e}")

    results["duration_s"] = round(time.time() - t0, 1)
    return results


# ── Full cycle ────────────────────────────────────────────────────────────────

def run_full_cycle(
    skip_ingestion: bool = False,
    skip_analysis: bool = False,
    skip_synthesis: bool = False,
    skip_meta: bool = False,
    workers: int = 4,
    synthesize_tools: int = 5,
    verbose: bool = True,
) -> dict:
    """
    Run all 4 phases in sequence.
    Returns full results dict.
    """
    t0 = time.time()
    cycle = {
        "started_at": datetime.now().isoformat(),
        "phases": {},
    }

    if not skip_ingestion:
        _log("orchestrator", "=== Phase 1: Ingestion ===")
        cycle["phases"]["ingestion"] = run_ingestion_phase(verbose=verbose)

    if not skip_analysis:
        _log("orchestrator", "=== Phase 2: Analysis ===")
        cycle["phases"]["analysis"] = run_analysis_phase(workers=workers, verbose=verbose)

    if not skip_synthesis:
        _log("orchestrator", "=== Phase 3: Synthesis ===")
        cycle["phases"]["synthesis"] = run_synthesis_phase(verbose=verbose)

    if not skip_meta:
        _log("orchestrator", "=== Phase 4: Meta-cognitive ===")
        cycle["phases"]["meta"] = run_meta_phase(synthesize_tools=synthesize_tools, verbose=verbose)

    cycle["total_duration_s"] = round(time.time() - t0, 1)
    cycle["completed_at"]     = datetime.now().isoformat()

    _log("orchestrator", f"Cycle complete in {cycle['total_duration_s']}s")
    return cycle


def print_status():
    """Print queue depths, agent performance, and coverage stats."""
    import sqlite3
    conn = sqlite3.connect(str(REPO / "memory_store" / "graph.db"), timeout=10)
    conn.row_factory = sqlite3.Row

    print("\n── Queue Depths ─────────────────────────────")
    rows = conn.execute(
        "SELECT queue, status, COUNT(*) as n FROM work_queue GROUP BY queue, status ORDER BY queue, status"
    ).fetchall()
    for r in rows:
        print(f"  {r['queue']:25s} {r['status']:10s} {r['n']}")

    print("\n── Agent Performance (7d) ───────────────────")
    from memory.agent_perf import all_stats
    for s in all_stats(7):
        sr = f"{s['success_rate']:.0%}" if s["success_rate"] is not None else "n/a"
        print(f"  {s['agent_id']:35s} runs={s['runs']:3d} ok={s['total_ok']:5d} fail={s['total_fail']:4d} sr={sr}")

    print("\n── Coverage Stats ───────────────────────────")
    rows = conn.execute(
        "SELECT COUNT(DISTINCT ticker) as tickers, AVG(staleness_days) as avg_stale FROM ticker_coverage"
    ).fetchone()
    if rows and rows["tickers"]:
        print(f"  tickers_mapped={rows['tickers']}  avg_staleness={rows['avg_stale']:.1f}d")

    print("\n── Graph ─────────────────────────────────────")
    for tbl in ("nodes", "edges", "episodes"):
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl:12s}: {n:,}")

    conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Swarm orchestrator")
    p.add_argument("--once",        action="store_true", help="Run one full cycle")
    p.add_argument("--continuous",  action="store_true", help="Run continuously")
    p.add_argument("--hours",       type=float, default=24, help="Interval for --continuous")
    p.add_argument("--phase",       choices=["ingestion","analysis","synthesis","meta"],
                   help="Run one phase only")
    p.add_argument("--status",      action="store_true", help="Print status")
    p.add_argument("--workers",     type=int, default=4)
    p.add_argument("--synth-tools", type=int, default=5,
                   dest="synthesize_tools", help="Max new tools to synthesize per meta phase")
    p.add_argument("--no-ingestion",  action="store_true")
    p.add_argument("--no-analysis",   action="store_true")
    p.add_argument("--no-synthesis",  action="store_true")
    p.add_argument("--no-meta",       action="store_true")
    args = p.parse_args()

    if args.status:
        print_status()
    elif args.phase == "ingestion":
        print(json.dumps(run_ingestion_phase(), indent=2, default=str))
    elif args.phase == "analysis":
        print(json.dumps(run_analysis_phase(args.workers), indent=2, default=str))
    elif args.phase == "synthesis":
        print(json.dumps(run_synthesis_phase(), indent=2, default=str))
    elif args.phase == "meta":
        print(json.dumps(run_meta_phase(args.synthesize_tools), indent=2, default=str))
    elif args.once:
        result = run_full_cycle(
            skip_ingestion=args.no_ingestion,
            skip_analysis=args.no_analysis,
            skip_synthesis=args.no_synthesis,
            skip_meta=args.no_meta,
            workers=args.workers,
            synthesize_tools=args.synthesize_tools,
        )
        print(json.dumps(result, indent=2, default=str))
    elif args.continuous:
        interval = args.hours * 3600
        cycle_n  = 0
        while True:
            cycle_n += 1
            print(f"\n{'='*60}")
            print(f"CYCLE {cycle_n}  {datetime.now().isoformat()}")
            print(f"{'='*60}")
            run_full_cycle(workers=args.workers, synthesize_tools=args.synthesize_tools)
            print(f"Sleeping {args.hours}h until next cycle...")
            time.sleep(interval)
    else:
        p.print_help()
