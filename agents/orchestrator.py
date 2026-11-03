"""
OrchestratorAgent — the master brain of the swarm.

Runs every 30 minutes. On each cycle:

1. INBOX     — reads message bus for signals from all agents
2. HEALTH    — checks every agent's last heartbeat, flags stalled agents
3. CONFLUENCE — cross-references signals:
                  price near 52W low  + improving fundamentals  → BUY confluence
                  price near 52W high + deteriorating fundamentals → SELL confluence
                  announcement (material) + trend signal → CONFIRM confluence
                  macro shift (FII sell) + deteriorating trend → RISK confluence
4. REASONING  — scores each confluence by strength, logs to graph as ConfluenceSignal nodes
5. ACTIONS    — publishes action_required messages to agents that need to act
6. BROADCAST  — publishes daily_summary to 'all' at market close (4 PM IST)
7. SELF-CHECK — detects data gaps, missing pipeline steps, stale nodes
"""
import json
import sys
import os
from datetime import datetime, timedelta, date
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import sqlite3 as _sqlite3
_GRAPH_DB = REPO / "memory_store" / "graph.db"

from memory.message_bus import publish, subscribe, ack_all, get_recent


def _gconn():
    conn = _sqlite3.connect(str(_GRAPH_DB))
    conn.row_factory = _sqlite3.Row
    return conn


# ── Constants ─────────────────────────────────────────────────────────────────

AGENT_HEARTBEAT_WARN_HOURS = 26   # flag if agent hasn't reported in >26h
STALE_PRICE_HOURS          = 30   # PriceSnapshot older than this is stale
CONFLUENCE_MIN_SCORE       = 0.5  # below this = noise, don't emit

# Only these metrics produce meaningful confluence signals
# costs/depreciation/tax "improving" can be directionally misleading without context
CONFLUENCE_METRICS = {
    "total_income", "revenue", "net_profit", "pat", "ebitda", "ebit",
    "eps", "operating_profit", "gross_profit", "sales",
    # cost metrics only meaningful if explicitly falling (negative delta = good)
    "finance_costs", "total_expenses",
}

AGENTS = [
    "context_agent",
    "trend_agent",
    "alert_agent",
    "price_agent",
    "document_agent",
    "announcement_agent",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    from datetime import timezone
    import zoneinfo
    try:
        ist = zoneinfo.ZoneInfo("Asia/Kolkata")
        return datetime.now(ist)
    except Exception:
        return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _ist_hour() -> int:
    return _now_ist().hour


# ── Health Check ──────────────────────────────────────────────────────────────

def check_agent_health() -> dict:
    """
    Read recent agent_health messages from bus.
    Flag any agent that hasn't reported in AGENT_HEARTBEAT_WARN_HOURS.
    """
    health_msgs = get_recent(event_type="agent_health", hours=AGENT_HEARTBEAT_WARN_HOURS)
    reported = {m["sender"] for m in health_msgs}
    stalled  = [a for a in AGENTS if a not in reported]

    cutoff = (datetime.now() - timedelta(hours=STALE_PRICE_HOURS)).isoformat()
    try:
        with _gconn() as conn:
            stale_prices = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE node_type='PriceSnapshot' AND created_at < ?",
                (cutoff,)
            ).fetchone()[0]
    except Exception:
        stale_prices = -1

    return {
        "agents_reported": sorted(reported),
        "agents_stalled":  stalled,
        "stale_price_nodes": stale_prices,
        "checked_at": datetime.now().isoformat(),
    }


# ── Confluence Detection ──────────────────────────────────────────────────────

def detect_confluences() -> list[dict]:
    """
    Cross-reference PriceSnapshot + TrendSummary nodes to find confluences.
    Returns list of confluence dicts ready to be written as nodes.
    """
    confluences = []

    with _gconn() as conn:
        price_rows = conn.execute(
            "SELECT ticker, properties FROM nodes WHERE node_type='PriceSnapshot'"
        ).fetchall()
        trend_rows = conn.execute(
            "SELECT ticker, properties FROM nodes WHERE node_type='TrendSummary'"
        ).fetchall()

    prices = {}
    for r in price_rows:
        try:
            p = json.loads(r[1])
            prices[r[0]] = p
        except Exception:
            pass

    trends: dict[str, list] = {}
    for r in trend_rows:
        try:
            p = json.loads(r[1])
            t_ticker = r[0]
            if t_ticker not in trends:
                trends[t_ticker] = []
            trends[t_ticker].append(p)
        except Exception:
            pass

    # ── BUY confluence: near 52W low + improving fundamentals ────────────────
    for ticker, price in prices.items():
        near_low = float(price.get("near_52w_low_pct") or 999)
        if near_low > 15:
            continue  # not near low
        ticker_trends = trends.get(ticker, [])
        improving = [t for t in ticker_trends
                     if t.get("direction") == "improving"
                     and int(t.get("consistent_periods") or 0) >= 2
                     and t.get("metric") in CONFLUENCE_METRICS]
        if not improving:
            continue

        best = max(improving, key=lambda t: float(t.get("momentum_score") or 0))
        momentum = float(best.get("momentum_score") or 0)
        score = round(
            0.4 * (1 - near_low / 15) +   # closer to low = higher weight
            0.6 * momentum,
            3
        )
        if score < CONFLUENCE_MIN_SCORE:
            continue

        confluences.append({
            "ticker":       ticker,
            "type":         "BUY_CONFLUENCE",
            "score":        score,
            "near_low_pct": near_low,
            "metric":       best.get("metric"),
            "momentum":     momentum,
            "streak":       best.get("consistent_periods"),
            "reason":       (
                f"{ticker} is {near_low:.1f}% from 52W low with "
                f"{best.get('metric')} improving {best.get('consistent_periods')} quarters "
                f"(momentum {momentum:.2f})"
            ),
            "close":        price.get("close"),
            "year_low":     price.get("year_low"),
            "year_high":    price.get("year_high"),
        })

    # ── SELL confluence: near 52W high + deteriorating fundamentals ──────────
    for ticker, price in prices.items():
        near_high = float(price.get("near_52w_high_pct") or 999)
        if near_high > 10:
            continue
        ticker_trends = trends.get(ticker, [])
        deteriorating = [t for t in ticker_trends
                         if t.get("direction") == "deteriorating"
                         and int(t.get("consistent_periods") or 0) >= 2
                         and t.get("metric") in CONFLUENCE_METRICS]
        if not deteriorating:
            continue

        worst = max(deteriorating, key=lambda t: float(t.get("momentum_score") or 0))
        momentum = float(worst.get("momentum_score") or 0)
        score = round(
            0.4 * (1 - near_high / 10) +
            0.6 * momentum,
            3
        )
        if score < CONFLUENCE_MIN_SCORE:
            continue

        confluences.append({
            "ticker":        ticker,
            "type":          "SELL_CONFLUENCE",
            "score":         score,
            "near_high_pct": near_high,
            "metric":        worst.get("metric"),
            "momentum":      momentum,
            "streak":        worst.get("consistent_periods"),
            "reason":        (
                f"{ticker} is {near_high:.1f}% from 52W high with "
                f"{worst.get('metric')} deteriorating {worst.get('consistent_periods')} quarters"
            ),
            "close":         price.get("close"),
            "year_low":      price.get("year_low"),
            "year_high":     price.get("year_high"),
        })

    # sort by score descending
    confluences.sort(key=lambda c: c["score"], reverse=True)
    return confluences


# ── Write Confluence Nodes ────────────────────────────────────────────────────

def write_confluence_nodes(confluences: list[dict]) -> int:
    written = 0
    today   = date.today().isoformat()
    with _gconn() as conn:
        for c in confluences:
            node_id = f"confluence-{c['ticker']}-{c['type']}-{today}"
            existing = conn.execute(
                "SELECT id FROM nodes WHERE id=?", (node_id,)
            ).fetchone()
            now = datetime.now().isoformat()
            if existing:
                conn.execute(
                    "UPDATE nodes SET properties=?, enriched_at=? WHERE id=?",
                    (json.dumps(c), now, node_id)
                )
            else:
                conn.execute(
                    "INSERT INTO nodes(id,node_type,name,ticker,properties,created_at,enriched,enriched_at) "
                    "VALUES (?,?,?,?,?,?,1,?)",
                    (node_id, "ConfluenceSignal",
                     f"{c['ticker']} {c['type']} {today}",
                     c["ticker"], json.dumps(c), now, now)
                )
            written += 1
    return written


# ── Publish Bus Events ────────────────────────────────────────────────────────

def publish_confluences(confluences: list[dict]):
    for c in confluences[:20]:  # top 20 only
        publish(
            sender="orchestrator",
            event_type="confluence_signal",
            payload=c,
            recipient="all",
            priority="high" if c["score"] >= 0.75 else "normal",
        )


def publish_health_alert(stalled: list[str]):
    if stalled:
        publish(
            sender="orchestrator",
            event_type="gap_detected",
            payload={"stalled_agents": stalled, "ts": datetime.now().isoformat()},
            recipient="all",
            priority="high",
        )


# ── Self Check ────────────────────────────────────────────────────────────────

def self_check() -> dict:
    """Detect data gaps and stale pipeline steps."""
    gaps = []
    with _gconn() as conn:
        trend_count = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE node_type='TrendSummary'"
        ).fetchone()[0]
        backlog = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE processed=0"
        ).fetchone()[0]
        today = date.today().isoformat()
        fresh_conf = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE node_type='ConfluenceSignal' AND name LIKE ?",
            (f"%{today}%",)
        ).fetchone()[0]

    if trend_count == 0:
        gaps.append("No TrendSummary nodes — TrendAgent may not have run")
    if backlog > 10000:
        gaps.append(f"Episode backlog: {backlog:,} unprocessed — ContextAgent lagging")
    if fresh_conf == 0:
        gaps.append("No ConfluenceSignal nodes for today — first run or no signals met threshold")

    bhavcopy_dir = REPO / "memory_store" / "bhavcopy"
    if bhavcopy_dir.exists():
        files = sorted(bhavcopy_dir.glob("*.csv"))
        if files:
            latest = files[-1].stem
            if latest < (date.today() - timedelta(days=3)).isoformat():
                gaps.append(f"Bhavcopy stale — latest is {latest}")

    return {"gaps": gaps, "episode_backlog": backlog, "trend_nodes": trend_count}


# ── Main Orchestration Cycle ──────────────────────────────────────────────────

def run() -> dict:
    print(f"\n{'='*60}")
    print(f"  OrchestratorAgent  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    results = {"ts": datetime.now().isoformat()}

    # 1. Read inbox
    inbox = subscribe("orchestrator")
    print(f"[INBOX] {len(inbox)} unread messages")
    for msg in inbox:
        p = json.loads(msg["payload"]) if isinstance(msg["payload"], str) else msg["payload"]
        print(f"  {msg['sender']:20s} → {msg['event_type']:25s} | {str(p)[:80]}")
    ack_all("orchestrator")
    results["inbox_count"] = len(inbox)

    # 2. Health check
    health = check_agent_health()
    print(f"\n[HEALTH]")
    print(f"  Reported:  {health['agents_reported']}")
    print(f"  Stalled:   {health['agents_stalled']}")
    print(f"  Stale prices: {health['stale_price_nodes']}")
    if health["agents_stalled"]:
        publish_health_alert(health["agents_stalled"])
    results["health"] = health

    # 3. Self check
    check = self_check()
    print(f"\n[SELF-CHECK]")
    for gap in check["gaps"]:
        print(f"  !  {gap}")
    if not check["gaps"]:
        print(f"  OK  No gaps detected")
    results["self_check"] = check

    # 4. Confluence detection
    with _gconn() as conn:
        n_prices = conn.execute("SELECT COUNT(*) FROM nodes WHERE node_type='PriceSnapshot'").fetchone()[0]
        n_trends = conn.execute("SELECT COUNT(*) FROM nodes WHERE node_type='TrendSummary'").fetchone()[0]
    print(f"\n[CONFLUENCE] Scanning {n_prices} price x {n_trends} trend nodes...")
    confluences = detect_confluences()
    print(f"  Found {len(confluences)} confluences")

    buy  = [c for c in confluences if c["type"] == "BUY_CONFLUENCE"]
    sell = [c for c in confluences if c["type"] == "SELL_CONFLUENCE"]
    print(f"  BUY:  {len(buy)}  |  SELL: {len(sell)}")
    for c in confluences[:10]:
        print(f"  [{c['score']:.2f}] {c['ticker']:12s} {c['type']:20s} {c['reason'][:60]}")

    # 5. Write confluence nodes
    written = write_confluence_nodes(confluences)
    print(f"\n  Wrote {written} ConfluenceSignal nodes to graph")
    results["confluences"] = {
        "total": len(confluences),
        "buy":   len(buy),
        "sell":  len(sell),
        "written": written,
        "top": confluences[:5],
    }

    # 6. Publish to bus
    publish_confluences(confluences)
    print(f"  Published top {min(20, len(confluences))} to message bus")

    # 7. Heartbeat
    publish(
        sender="orchestrator",
        event_type="agent_health",
        payload={
            "status": "ok",
            "confluences_found": len(confluences),
            "inbox_processed": len(inbox),
            "ts": datetime.now().isoformat(),
        },
        recipient="all",
        ttl_hours=30,
    )

    elapsed = (datetime.now() - datetime.fromisoformat(results["ts"])).seconds
    print(f"\n{'='*60}")
    print(f"  Cycle complete in {elapsed}s")
    print(f"{'='*60}\n")

    return results


if __name__ == "__main__":
    run()
