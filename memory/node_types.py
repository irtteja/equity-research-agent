"""
Typed node writers for the knowledge graph.

Each writer wraps GraphStore.add_node() with a deterministic node_id
and type-safe properties, keeping the calling code clean.

Node types:
  SurveillanceFlag  — ASM/GSM/T2T list entries
  DeliveryMetric    — NSE delivery bhavcopy (delivery %)
  ParticipantOI     — FII/DII/Client/Pro open-interest snapshot
  InsiderTrade      — SEBI insider trading disclosure
  PledgeData        — Promoter pledge filing
  SASTFiling        — SEBI SAST Reg 29 disclosure
  CorporateAction   — Dividend, bonus, split, rights

All writers are idempotent (upsert on deterministic id).
"""
import hashlib
from memory.graph_store import get_store


def _short_hash(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:8]


def write_surveillance_flag(
    ticker: str,
    list_type: str,       # "asm", "gsm", "t2t", "slb"
    stage: str,
    trade_date: str,      # YYYY-MM-DD
    reason: str = "",
    extra: dict = None,
) -> str:
    node_id = f"surv-{ticker}-{list_type}-{trade_date}"
    get_store().add_node(
        node_id=node_id,
        node_type="SurveillanceFlag",
        name=f"{ticker} — {list_type.upper()} {stage} ({trade_date})",
        ticker=ticker,
        properties={
            "list_type":  list_type,
            "stage":      stage,
            "trade_date": trade_date,
            "reason":     reason,
            **(extra or {}),
        },
    )
    return node_id


def write_delivery_metric(
    ticker: str,
    trade_date: str,      # YYYY-MM-DD
    delivery_qty: float,
    delivery_pct: float,
    traded_qty: float = 0,
    turnover_lacs: float = 0,
) -> str:
    node_id = f"delivery-{ticker}-{trade_date}"
    get_store().add_node(
        node_id=node_id,
        node_type="DeliveryMetric",
        name=f"{ticker} delivery {trade_date}: {delivery_pct:.1f}%",
        ticker=ticker,
        properties={
            "trade_date":    trade_date,
            "delivery_qty":  delivery_qty,
            "delivery_pct":  delivery_pct,
            "traded_qty":    traded_qty,
            "turnover_lacs": turnover_lacs,
        },
    )
    return node_id


def write_participant_oi(
    trade_date: str,
    rows: list[dict],     # list from nse.participant_oi()
) -> str:
    node_id = f"partoi-{trade_date}"
    # Summarise into net positions per participant
    summary = {}
    for row in rows:
        p = row.get("participant", "")
        summary[p] = {
            "total_long":  row.get("total_long", 0),
            "total_short": row.get("total_short", 0),
            "net":         (row.get("total_long", 0) or 0) - (row.get("total_short", 0) or 0),
        }
    get_store().add_node(
        node_id=node_id,
        node_type="ParticipantOI",
        name=f"Participant OI {trade_date}",
        ticker="",
        properties={
            "trade_date": trade_date,
            "summary":    summary,
            "raw_rows":   rows,
        },
    )
    return node_id


def write_insider_trade(
    ticker: str,
    trade_date: str,
    person: str,
    transaction_type: str,   # "Buy", "Sell"
    qty: float,
    value: float = 0,
    mode: str = "",
    extra: dict = None,
) -> str:
    unique = _short_hash(f"{ticker}{trade_date}{person}{transaction_type}{qty}")
    node_id = f"insider-{ticker}-{trade_date}-{unique}"
    get_store().add_node(
        node_id=node_id,
        node_type="InsiderTrade",
        name=f"{ticker} insider {transaction_type} {qty:,.0f} shares ({trade_date})",
        ticker=ticker,
        properties={
            "trade_date":       trade_date,
            "person":           person,
            "transaction_type": transaction_type,
            "qty":              qty,
            "value":            value,
            "mode":             mode,
            **(extra or {}),
        },
    )
    return node_id


def write_pledge_data(
    ticker: str,
    record_date: str,
    promoter: str,
    pledged_qty: float,
    total_holding_pct: float = 0,
    pledged_pct: float = 0,
    extra: dict = None,
) -> str:
    unique = _short_hash(f"{ticker}{record_date}{promoter}{pledged_qty}")
    node_id = f"pledge-{ticker}-{record_date}-{unique}"
    get_store().add_node(
        node_id=node_id,
        node_type="PledgeData",
        name=f"{ticker} pledge {record_date}: {pledged_pct:.1f}% pledged",
        ticker=ticker,
        properties={
            "record_date":       record_date,
            "promoter":          promoter,
            "pledged_qty":       pledged_qty,
            "total_holding_pct": total_holding_pct,
            "pledged_pct":       pledged_pct,
            **(extra or {}),
        },
    )
    return node_id


def write_sast_filing(
    ticker: str,
    filing_date: str,
    acquirer: str,
    shares_acquired: float,
    post_acquisition_pct: float = 0,
    mode: str = "",
    extra: dict = None,
) -> str:
    unique = _short_hash(f"{ticker}{filing_date}{acquirer}{shares_acquired}")
    node_id = f"sast-{ticker}-{filing_date}-{unique}"
    get_store().add_node(
        node_id=node_id,
        node_type="SASTFiling",
        name=f"{ticker} SAST {filing_date}: {acquirer} acquired {shares_acquired:,.0f} ({post_acquisition_pct:.2f}%)",
        ticker=ticker,
        properties={
            "filing_date":          filing_date,
            "acquirer":             acquirer,
            "shares_acquired":      shares_acquired,
            "post_acquisition_pct": post_acquisition_pct,
            "mode":                 mode,
            **(extra or {}),
        },
    )
    return node_id


def write_corporate_action(
    ticker: str,
    action_date: str,
    action_type: str,     # "dividend", "bonus", "split", "rights", "buyback"
    details: str,
    ex_date: str = "",
    record_date: str = "",
    extra: dict = None,
) -> str:
    unique = _short_hash(f"{ticker}{action_date}{action_type}{details}")
    node_id = f"corpaction-{ticker}-{action_date}-{unique}"
    get_store().add_node(
        node_id=node_id,
        node_type="CorporateAction",
        name=f"{ticker} {action_type} {action_date}: {details}",
        ticker=ticker,
        properties={
            "action_date":  action_date,
            "action_type":  action_type,
            "details":      details,
            "ex_date":      ex_date,
            "record_date":  record_date,
            **(extra or {}),
        },
    )
    return node_id
