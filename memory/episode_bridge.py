"""
Episode bridge — converts existing data structures to graph episodes and nodes.

Every significant event in the pipeline writes here. The bridge is the
entry point into the knowledge graph — it converts structured dicts
(facts, results, gaps, patches, task events) into:
  1. A natural-language episode text (human-readable audit trail)
  2. A typed node in the graph
  3. Initial edges where the relationship is obvious at write time

More complex edges (PRECEDED_BY, CORROBORATES, CONTRADICTS) are created
by the ContextAgent after it reads pending_enrichment() nodes.

Called from:
  memory/ingestion.py:save_extraction()  → fact_to_graph()
  memory/results_store.py:save_results() → result_to_graph()
  jobs/self_improve.py                   → gap_to_graph(), patch_to_graph()
  memory/task_log.py                     → task_event_to_graph()
"""
import re
from datetime import datetime, date
from pathlib import Path

REPO = Path(__file__).parent.parent


def _gs():
    from memory.graph_store import get_store
    return get_store()


# ── Fact → Graph ──────────────────────────────────────────────────────────────

def fact_to_graph(ticker: str, fact: dict) -> str | None:
    """
    Convert one extracted fact (from memory/ingestion.py schema) into a
    MetricValue node + episode. Returns node_id or None if fact is not numeric.

    A MetricValue node represents one specific measurement:
      HDFC — NIM — 3.4% — Q2FY27
    """
    gs = _gs()

    raw  = fact.get("raw", {})
    metric = (raw.get("metric") or "").strip().lower()
    value  = raw.get("value")
    period = (raw.get("period") or "").strip()

    # Only graph numeric facts with a known metric and period
    if not metric or value is None or not period:
        return None
    try:
        float(value)
    except (TypeError, ValueError):
        return None

    unit        = (raw.get("unit") or "").strip()
    period_date = (raw.get("period_date") or "").strip()
    doc_type    = (raw.get("doc_type") or "").strip()
    context     = (raw.get("context") or "")[:200]
    source      = (raw.get("source") or "").strip()
    date_accessed = raw.get("date_accessed", date.today().isoformat())

    # Stable node id — same filing re-run produces the same id
    node_id = f"{ticker.upper()}-{metric}-{period}".replace(" ", "_").replace("/", "_")

    # Episode text — natural language for future semantic search
    episode_text = (
        f"{ticker.upper()} reported {metric} of {value} {unit} for {period}"
        + (f" (period ending {period_date})" if period_date else "")
        + (f". Source: {source}" if source else "")
        + (f". Context: {context}" if context else "")
    )

    gs.add_episode(
        episode_type="fact",
        text=episode_text,
        source="memory/episode_bridge.py:fact_to_graph",
        ticker=ticker,
        metadata={"node_id": node_id, "fact_id": fact.get("fact_id", ""), "doc_type": doc_type},
    )

    gs.add_node(
        node_id=node_id,
        node_type="MetricValue",
        name=f"{ticker.upper()} {metric} {period}",
        ticker=ticker,
        properties={
            "metric":       metric,
            "value":        float(value),
            "unit":         unit,
            "period":       period,
            "period_date":  period_date,
            "doc_type":     doc_type,
            "date_accessed": date_accessed,
            "context":      context,
            "fact_id":      fact.get("fact_id", ""),
        },
    )

    # Ensure the Company node exists
    gs.add_node(
        node_id=f"company-{ticker.upper()}",
        node_type="Company",
        name=ticker.upper(),
        ticker=ticker,
        properties={"ticker": ticker.upper()},
    )
    gs.add_edge("HAS_VALUE", f"company-{ticker.upper()}", node_id)

    return node_id


def facts_to_graph(ticker: str, facts: list[dict]) -> list[str]:
    """Batch-convert extracted facts. Returns list of node_ids created."""
    node_ids = []
    for fact in facts:
        nid = fact_to_graph(ticker, fact)
        if nid:
            node_ids.append(nid)
    return node_ids


# ── XBRL Result → Graph ───────────────────────────────────────────────────────

def result_to_graph(ticker: str, quarter: dict) -> list[str]:
    """
    Convert one XBRL quarter dict (from memory/results_store.py schema) into
    MetricValue nodes for all numeric fields. Returns list of node_ids.

    XBRL fields we care about: revenue, pat, ebitda, total_income, eps,
    total_expenses, finance_costs, depreciation, tax_expense, etc.
    """
    gs = _gs()

    period_end = (quarter.get("period_end") or quarter.get("toDate") or "").strip()[:10]
    if not period_end:
        return []

    # Derive period label from period_end date
    try:
        from datetime import date as _date
        pe = _date.fromisoformat(period_end)
        month = pe.month
        fy_year = pe.year if month > 3 else pe.year - 1
        quarter_map = {
            4: "Q4", 5: "Q1", 6: "Q1", 7: "Q2", 8: "Q2", 9: "Q2",
            10: "Q3", 11: "Q3", 12: "Q3", 1: "Q4", 2: "Q4", 3: "Q4",
        }
        q_label = quarter_map.get(month, "Q?")
        fy_label = f"FY{str(fy_year+1)[-2:]}"
        period = f"{q_label}-{fy_label}"
    except Exception:
        period = period_end

    # Numeric fields to graph — each entry: list of possible raw keys → (metric_name, unit)
    # Handles both nselib API format and stored JSONL format
    metric_fields = [
        (["revenue", "revenue_from_operations"],       "revenue",        "crore"),
        (["totalIncome", "total_income"],               "total_income",   "crore"),
        (["netProfit", "pat"],                          "pat",            "crore"),
        (["profitBeforeTax", "pbt"],                    "pbt",            "crore"),
        (["ebitda"],                                    "ebitda",         "crore"),
        (["ebit"],                                      "ebit",           "crore"),
        (["eps", "eps_basic"],                          "eps",            "rs"),
        (["totalExpenses", "total_expenses"],           "total_expenses", "crore"),
        (["finCost", "finance_costs"],                  "finance_costs",  "crore"),
        (["depreciationAmortization", "depreciation"],  "depreciation",   "crore"),
        (["taxExpense", "tax"],                         "tax_expense",    "crore"),
    ]

    node_ids = []
    for raw_keys, metric_name, unit in metric_fields:
        val = next((quarter.get(k) for k in raw_keys if quarter.get(k) is not None), None)
        if val is None:
            continue
        try:
            val = float(str(val).replace(",", ""))
        except (ValueError, TypeError):
            continue

        node_id = f"{ticker.upper()}-{metric_name}-{period}".replace(" ", "_")

        episode_text = (
            f"{ticker.upper()} XBRL: {metric_name} = {val} {unit} for {period} "
            f"(period ending {period_end})"
        )

        gs.add_episode(
            episode_type="xbrl_result",
            text=episode_text,
            source="memory/episode_bridge.py:result_to_graph",
            ticker=ticker,
            metadata={"node_id": node_id, "period_end": period_end},
        )

        gs.add_node(
            node_id=node_id,
            node_type="MetricValue",
            name=f"{ticker.upper()} {metric_name} {period}",
            ticker=ticker,
            properties={
                "metric":       metric_name,
                "value":        val,
                "unit":         unit,
                "period":       period,
                "period_date":  period_end,
                "doc_type":     "financial_results",
                "source":       "xbrl",
            },
        )

        gs.add_node(
            node_id=f"company-{ticker.upper()}",
            node_type="Company",
            name=ticker.upper(),
            ticker=ticker,
            properties={"ticker": ticker.upper()},
        )
        gs.add_edge("HAS_VALUE", f"company-{ticker.upper()}", node_id)
        node_ids.append(node_id)

    return node_ids


# ── Management Statement → Graph ──────────────────────────────────────────────

def statement_to_graph(ticker: str, fact: dict) -> str | None:
    """
    Convert a management statement fact (speaker, context, forward-looking)
    into a ManagementStatement node. Returns node_id or None.
    """
    gs = _gs()

    raw     = fact.get("raw", {})
    context = (raw.get("context") or "").strip()
    speaker = (raw.get("speaker") or "").strip()
    period  = (raw.get("period") or "").strip()

    if not context or len(context) < 20:
        return None
    # Only graph statements with a speaker (management commentary, not disclosures)
    if not speaker:
        return None

    node_id = f"stmt-{ticker.upper()}-{fact.get('fact_id','')}"

    gs.add_episode(
        episode_type="management_statement",
        text=f"{ticker.upper()} — {speaker}: \"{context[:200]}\" ({period})",
        source="memory/episode_bridge.py:statement_to_graph",
        ticker=ticker,
        metadata={"node_id": node_id, "fact_id": fact.get("fact_id", "")},
    )

    gs.add_node(
        node_id=node_id,
        node_type="ManagementStatement",
        name=f"{ticker.upper()} {speaker} {period}",
        ticker=ticker,
        properties={
            "text":         context[:500],
            "speaker":      speaker,
            "speaker_role": raw.get("speaker_role", ""),
            "period":       period,
            "period_date":  raw.get("period_date", ""),
            "sentiment":    fact.get("tone", {}).get("sentiment", ""),
        },
    )

    gs.add_node(
        node_id=f"company-{ticker.upper()}",
        node_type="Company",
        name=ticker.upper(),
        ticker=ticker,
        properties={"ticker": ticker.upper()},
    )

    return node_id


# ── Self-Improve Gap → Graph ──────────────────────────────────────────────────

def gap_to_graph(gap: dict) -> str:
    """Convert a self-improve gap to a Gap node. Returns node_id."""
    gs = _gs()

    area     = gap.get("area", "unknown")
    finding  = gap.get("finding", "")[:300]
    priority = gap.get("priority", "medium")
    node_id  = f"gap-{area}".replace(":", "-").replace(" ", "_")

    gs.add_episode(
        episode_type="self_improve_gap",
        text=f"Gap detected [{priority}]: {area} — {finding}",
        source="jobs/self_improve.py",
        metadata={"node_id": node_id, "area": area, "priority": priority,
                  "consecutive_scans": gap.get("consecutive_scans", 1)},
    )

    gs.add_node(
        node_id=node_id,
        node_type="Gap",
        name=area,
        properties={
            "area":              area,
            "finding":           finding,
            "priority":          priority,
            "consecutive_scans": gap.get("consecutive_scans", 1),
            "action":            gap.get("action", ""),
            "stuck":             gap.get("stuck", False),
        },
    )
    return node_id


def patch_to_graph(patch: dict, gap_area: str = "") -> str:
    """Convert an applied patch to a Patch node, optionally linking to a Gap. Returns node_id."""
    gs = _gs()

    node_id = f"patch-{patch.get('file','').replace('/','_')}-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    gs.add_episode(
        episode_type="patch_applied",
        text=f"Patch applied to {patch.get('file','?')}: {patch.get('description','')[:200]}",
        source="jobs/self_improve.py",
        metadata={"node_id": node_id, "file": patch.get("file", ""),
                  "confidence": patch.get("confidence", "")},
    )

    gs.add_node(
        node_id=node_id,
        node_type="Patch",
        name=f"patch:{patch.get('file','?')}",
        properties={
            "file":        patch.get("file", ""),
            "description": patch.get("description", "")[:300],
            "confidence":  patch.get("confidence", ""),
            "applied_at":  datetime.now().isoformat(),
        },
    )

    # Link patch to the gap it fixed
    if gap_area:
        gap_node_id = f"gap-{gap_area}".replace(":", "-").replace(" ", "_")
        if gs.get_node(gap_node_id):
            gs.add_edge("FIXED_BY", gap_node_id, node_id)

    return node_id


# ── Task Event → Graph ────────────────────────────────────────────────────────

def task_event_to_graph(task_id: str, status: str, description: str = "",
                         error: str = "") -> str:
    """Convert a task log event to a graph episode (for self-improve agents)."""
    gs = _gs()

    text = f"Task '{task_id}' {status}"
    if error:
        text += f": {error[:200]}"

    gs.add_episode(
        episode_type="task_event",
        text=text,
        source="memory/task_log.py",
        metadata={"task_id": task_id, "status": status,
                  "description": description[:200], "error": error[:200]},
    )
    return task_id
