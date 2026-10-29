#!/usr/bin/env python3
"""
Two-pass ingestion triage — accuracy test.

Pass 1: Batch attchmntText triage (skip/light/deep) on all today's announcements
Pass 2: "Analyze all" full re-read — Claude looks at every non-skip with full context
Compare: see what Pass 1 missed or over-triggered on.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.expanduser("~/irtteja/nselib"))

import json
import anthropic
from datetime import date
from collections import defaultdict

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANN_PATH = os.path.join(REPO_DIR, "memory_store", "announcements", f"{date.today().isoformat()}.json")

BATCH_TRIAGE_PROMPT = """\
You are an equity research triage agent for Indian markets.

For each announcement, decide the ingestion strategy based ONLY on the text provided:

"skip"  — zero analytical value: meeting schedules, transcript/recording links, newspaper publication ads,
          monitoring agency reports, compliance certificates, loss of share certs, record date reminders (no amount)
"light" — a useful structured fact fully contained in the text: dividend amount + record date, ESOP allotment
          count, appointment/resignation name+role, order win WITH size stated in text, credit rating change
"deep"  — needs PDF for full extraction: financial results, acquisitions (deal size not clear from text alone),
          capacity expansion, major restructuring, management commentary, investor presentations with guidance,
          CEO/MD level resignation or appointment

Return ONLY a JSON array — no markdown, no explanation outside the array:
[{"id":"<id>","strategy":"skip|light|deep","reason":"<5 words>","key_fact":"<core fact or null>"},...]

Announcements:
{items}
"""


def load_announcements() -> dict:
    if os.path.exists(ANN_PATH):
        with open(ANN_PATH) as f:
            data = json.load(f)
        return data.get("announcements", {})
    # Re-fetch if not saved
    print("  No saved file — fetching live...")
    from jobs.fetch_announcements import fetch_announcements_parallel, save_announcements
    result = fetch_announcements_parallel(to_date=date.today(), days_back=1, verbose=True)
    save_announcements(result)
    return result["announcements"]


def flatten(announcements: dict) -> list[dict]:
    items = []
    for symbol, rows in announcements.items():
        for row in rows:
            items.append({
                "id": f"{symbol}__{row.get('seq_id', len(items))}",
                "symbol": symbol,
                "category": (row.get("desc") or "").strip(),
                "text": (row.get("attchmntText") or "")[:300],
            })
    return items


def run_batch_triage(items: list[dict], client: anthropic.Anthropic) -> list[dict]:
    """Pass 1: batch triage on attchmntText."""
    print(f"\n[Pass 1] Batch triage — {len(items)} announcements...")
    # Build payload: id + category + text (keep it lean)
    payload = [{"id": x["id"], "cat": x["category"], "txt": x["text"]} for x in items]
    prompt = BATCH_TRIAGE_PROMPT.replace("{items}", json.dumps(payload, ensure_ascii=False))

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    # Strip accidental fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    decisions = json.loads(raw.strip())
    by_id = {d["id"]: d for d in decisions}
    # Attach back to items
    for item in items:
        d = by_id.get(item["id"], {})
        item["strategy"] = d.get("strategy", "skip")
        item["reason"]   = d.get("reason", "")
        item["key_fact"] = d.get("key_fact")
    print(f"  Done. usage: {msg.usage.input_tokens} in / {msg.usage.output_tokens} out tokens")
    return items


VALIDATE_PROMPT = """\
You are an equity research analyst. Review these announcements that a triage system marked as "{strategy}".
For each one, answer: was the strategy CORRECT or did the system make an error?

An error is:
- Marked "skip" but contains a material fact (financial result, acquisition, CEO change, order win)
- Marked "light" but actually needs PDF for full context
- Marked "deep" but is actually noise (transcript link, schedule, ad)

Return ONLY a JSON array:
[{{"id":"<id>","verdict":"correct|wrong","correction":"skip|light|deep|ok","note":"<reason if wrong>"}}]

Announcements:
{items}
"""


def run_accuracy_check(items: list[dict], strategy: str, client: anthropic.Anthropic) -> list[dict]:
    """Pass 2: validate a strategy bucket."""
    subset = [x for x in items if x["strategy"] == strategy]
    if not subset:
        return []
    print(f"\n[Pass 2] Validating {len(subset)} '{strategy}' decisions...")
    payload = [{"id": x["id"], "cat": x["category"], "txt": x["text"],
                "triage_reason": x["reason"], "key_fact": x["key_fact"]} for x in subset]
    prompt = VALIDATE_PROMPT.replace("{strategy}", strategy).replace("{items}", json.dumps(payload, ensure_ascii=False))
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=6000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    verdicts = json.loads(raw.strip())
    print(f"  Done. usage: {msg.usage.input_tokens} in / {msg.usage.output_tokens} out tokens")
    return verdicts


def main():
    client = anthropic.Anthropic()

    print("Loading announcements...")
    announcements = load_announcements()
    items = flatten(announcements)
    print(f"  {len(items)} announcements across {len(announcements)} symbols")

    # Pass 1
    items = run_batch_triage(items, client)

    # Summarise triage
    by_strategy = defaultdict(list)
    for item in items:
        by_strategy[item["strategy"]].append(item)

    print("\n── Triage Summary ──")
    for s in ["deep", "light", "skip"]:
        bucket = by_strategy[s]
        print(f"  {s:5s}: {len(bucket):3d}")

    print("\n── Deep ──")
    for x in sorted(by_strategy["deep"], key=lambda x: x["symbol"]):
        print(f"  {x['symbol']:18s}  [{x['category'][:30]}]  {x['reason']}  |  {x['key_fact'] or ''}")

    print("\n── Light ──")
    for x in sorted(by_strategy["light"], key=lambda x: x["symbol"]):
        print(f"  {x['symbol']:18s}  [{x['category'][:30]}]  {x['reason']}  |  {x['key_fact'] or ''}")

    # Pass 2: accuracy check on all three buckets
    errors = []
    for strategy in ["skip", "light", "deep"]:
        verdicts = run_accuracy_check(items, strategy, client)
        wrong = [v for v in verdicts if v.get("verdict") == "wrong"]
        errors.extend(wrong)
        correct = len(verdicts) - len(wrong)
        print(f"  [{strategy}] {correct}/{len(verdicts)} correct, {len(wrong)} errors")

    # Accuracy report
    total = len(items)
    total_wrong = len(errors)
    total_correct = total - total_wrong
    print(f"\n── Accuracy ──")
    print(f"  Total       : {total}")
    print(f"  Correct     : {total_correct}  ({100*total_correct/total:.1f}%)")
    print(f"  Errors      : {total_wrong}")

    if errors:
        print("\n── Errors ──")
        for e in errors:
            item = next((x for x in items if x["id"] == e["id"]), {})
            print(f"  {item.get('symbol','?'):18s}  was={item.get('strategy')}  should={e.get('correction')}  note={e.get('note')}")
            print(f"    [{item.get('category','')}]  {item.get('text','')[:120]}")

    # Save full results
    out = os.path.join(REPO_DIR, "reports", "triage_test.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump({
            "date": date.today().isoformat(),
            "total": total,
            "correct": total_correct,
            "accuracy_pct": round(100*total_correct/total, 1),
            "by_strategy": {
                s: len(by_strategy[s]) for s in ["deep", "light", "skip"]
            },
            "items": items,
            "errors": errors,
        }, f, indent=2, default=str)
    print(f"\nFull results saved to {out}")


if __name__ == "__main__":
    main()
