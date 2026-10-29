"""
attchmntText-based triage — classifies each announcement before PDF download.

Three strategies:
  skip  — zero analytical value, don't download PDF
  light — structured fact fully in text, minimal extraction
  deep  — needs PDF for full extraction

Accuracy (2026-10-29 validation, 502 announcements):
  pass1 result: deep=214, light=80, skip=208
  pass2 errors: 6 (98.8% accuracy)
  corrected rules: Action(s) taken or orders passed → DEEP (was LIGHT)
                   Copy of Newspaper Publication with financial results text → DEEP (was SKIP)
"""
import re


def triage(category: str, attchmnt_text: str) -> tuple[str, str]:
    """Return (strategy, reason) given NSE category and attchmntText."""
    cat = category or ""
    t = (attchmnt_text or "").lower()

    # ── SKIP ─────────────────────────────────────────────────────────────────
    # Transcripts of earnings/analyst calls are DEEP — management commentary + guidance.
    # Only skip filings that are purely a URL/link to a recording, not the actual transcript.
    if any(p in t for p in [
        'transcript of earnings', 'transcript of conference call', 'transcript of analyst',
        'transcript of results', 'transcript of q2', 'transcript of q1', 'transcript of q3',
        'transcript of q4', 'transcript of the earnings', 'transcript of the conference',
        'transcript of the analyst',
    ]):
        return 'deep', 'earnings/analyst transcript'

    skip_txt = [
        'schedule of meet', 'schedule of analyst', 'schedule of earnings', 'schedule of q',
        'schedule of investor', 'schedule of conference',
        'link of recording', 'audio recording', 'link of audio recording',
        'link to the recording', 'link to recording',
        'newspaper publication', 'newspaper advertisement',
        'monitoring agency report', 'monitoring agencies',
        'loss of share certificates',
        'statement of deviation', 'reconciliation of share capital',
        'notice of postal ballot', 'dispatch of the notice of postal ballot',
        'intimation of earnings conference call',
        'revised intimation of earnings',
        'disclosure under regulation 31a',
        'certificate for payment of interest',
    ]
    for p in skip_txt:
        if p in t:
            return 'skip', p

    skip_cats = [
        'Monitoring Agency Report',
        'Loss of Share Certificates',
        'Certificate under SEBI (Depositories and Participants) Regulations, 2018',
        'Statement of deviation(s) or variation(s) under Reg. 32',
    ]
    if cat in skip_cats:
        return 'skip', f'skip_category'

    # Analysts/Institutional Investor Meet — skip if just a schedule/intimation,
    # but DEEP if it's the actual transcript or notes
    if cat == 'Analysts/Institutional Investor Meet/Con. Call Updates':
        if any(p in t for p in ['transcript', 'notes of the', 'key highlights', 'presentation']):
            return 'deep', 'analyst meet transcript/notes'
        return 'skip', 'skip_category'

    # Copy of Newspaper Publication — SKIP unless it contains actual financial results
    if cat == 'Copy of Newspaper Publication':
        if any(p in t for p in ['financial results', 'unaudited', 'audited', 'quarterly results']):
            return 'deep', 'newspaper: financial results'
        return 'skip', 'newspaper publication'

    if any(p in t for p in ['best employers', 'awards 2026 announced', 'banking innovation awards']):
        return 'skip', 'awards fluff'

    # ── LIGHT ────────────────────────────────────────────────────────────────
    m = re.search(r'dividend of rs\.?\s*[\d,.]+ ', t, re.I)
    if m:
        return 'light', f'dividend: {m.group()}'

    if 'interim dividend' in t and re.search(r'rs\.?\s*[\d,.]+', t, re.I):
        return 'light', 'interim dividend amount'

    if re.search(r'record date for the purpose of .{0,40} is \d{2}-\w{3}-\d{4}', t, re.I):
        return 'light', 'record date'

    m = re.search(r'allotment of ([\d,]+)\s+shares', t, re.I)
    if m:
        return 'light', f'esop allotment: {m.group(1)} shares'

    m = re.search(r'grant of ([\d,]+)\s+(employee stock options|options|esops)', t, re.I)
    if m:
        return 'light', f'esop grant: {m.group(1)}'

    m = re.search(r'(order|contract|epc).{0,80}(rs\.?|inr|₹)\s*[\d,.]+([ ]+crore| cr)', t, re.I)
    if m:
        return 'light', f'order with amount: {m.group()[:60]}'

    if re.search(r'(revenue|ebitda|pat|profit).{0,30}(crore|₹|rs\.?|mn|%|grew|grows|growth)', t, re.I) and \
       re.search(r'(q2|quarter|h1|fy2[0-9])', t, re.I):
        return 'light', 'key metrics in text'

    if re.search(r'aum.{0,20}(crore|₹)', t, re.I):
        return 'light', 'AUM metric in text'

    if 'appointment of' in t and any(r in t for r in [
        'non- executive', 'non-executive', 'independent director', 'company secretary',
        'compliance officer', 'joint company secretary', 'senior management',
        'group president', 'president,', 'chief financial officer'
    ]):
        return 'light', 'appointment name+role'

    if 'resignation of' in t and re.search(r'resignation of (mr|ms|mrs)\.?\s+\w+', t, re.I) and \
       not any(k in t for k in ['managing director', 'chief executive', 'ceo', 'md &', 'md&']):
        return 'light', 'resignation name+role'

    if cat in ('Credit Rating', 'Credit Rating- New'):
        return 'light', 'credit rating'

    if re.search(r'allotment.{0,40}(rs\.?|₹|crore|notes|ncd|commercial paper)', t, re.I):
        return 'light', 'debt issuance/allotment'

    if 'mclr' in t:
        return 'light', 'mclr change'

    if 'incorporation of' in t:
        return 'light', 'subsidiary incorporation'

    if 'corporate guarantee' in t and 'wholly owned subsidiary' in t:
        return 'light', 'corporate guarantee'

    if cat == 'ESOP/ESOS/ESPS':
        return 'light', 'esop (category)'

    if cat == 'Allotment of Securities' and re.search(r'allotment of \d+ securities pursuant to (esop|esps)', t, re.I):
        return 'light', 'esop allotment'

    if cat in (
        'Change in Director(s)',
        'Resignation of Director/KMP/Senior Management Personnel',
        'Cessation',
    ):
        return 'light', 'director change (generic)'

    # ── DEEP ─────────────────────────────────────────────────────────────────
    if any(p in t for p in [
        'financial results for the period ended', 'submitted to the exchange, the financial results',
        'unaudited financial results', 'audited standalone', 'financial results for the quarter',
        'financial results (standalone', 'financial results for the 2nd quarter',
        'q2 fy27', 'q2 & h1', 'quarter and half year ended', 'quarter ended september 30',
        'half year ended september 30', 'h1 fy27', 'quarter and half-year ended',
        '2nd quarter and half year', 'second quarter and half-year',
    ]):
        return 'deep', 'financial results'

    if cat == 'Investor Presentation' or 'investor presentation' in t:
        return 'deep', 'investor presentation'

    if 'press release' in t and any(p in t for p in [
        'financial results', 'result', 'q2', 'quarter', 'performance', 'revenue', 'profit'
    ]):
        return 'deep', 'results press release'

    if cat == 'Acquisition' or 'acquisition' in t or 'share purchase agreement' in t:
        return 'deep', 'acquisition'

    if any(p in t for p in [
        'capacity expansion', 'capacity addition', 'declared commercial', 'commercial operation',
        'commencement of commercial', 'commissioned', 'spiral facility', 'bess project',
        ' mw',
    ]):
        return 'deep', 'capacity/commissioning'

    if any(p in t for p in [
        'managing director', 'chief executive', 'chairman', 'ceo', 'md &', 'retirement of',
    ]):
        return 'deep', 'senior leadership change'

    if any(p in t for p in ['non-convertible debentures', 'ncd', 'issuance of']):
        return 'deep', 'NCD/capital raise'

    if cat in ('Bagging/Receiving of orders/contracts', 'Awarding of order(s)/contract(s)'):
        return 'deep', 'order (no size in text)'

    if cat == 'Scheme of Arrangement':
        return 'deep', 'scheme of arrangement'
    if cat == 'Disclosure of material issue':
        return 'deep', 'material disclosure'
    if cat == 'Outcome of Board Meeting':
        return 'deep', 'board meeting outcome'
    if cat == 'Arrangements for strategic, technical, manufacturing, or marketing tie up':
        return 'deep', 'strategic tie-up'
    if cat == 'Change in Management':
        return 'deep', 'management change'
    if cat == 'Integrated Filing- Financial':
        return 'deep', 'financial filing'
    if cat == 'Other Restructuring':
        return 'deep', 'restructuring'
    if cat in ('Resignation', 'Retirement'):
        return 'deep', 'senior leadership change'
    if cat == 'Action(s) taken or orders passed':
        return 'deep', 'regulatory order'
    if cat == 'Capacity addition':
        return 'deep', 'capacity/commissioning'
    if cat == 'Change in Auditors':
        return 'deep', 'auditor change'

    if 'ipo of' in t or 'subsidiary ipo' in t:
        return 'deep', 'subsidiary ipo'
    if 'e-auction' in t and re.search(r'rs\.?\s*[\d,]+', t, re.I):
        return 'deep', 'major transaction'
    if 'declaration of commercial operation' in t:
        return 'deep', 'commercial operation'

    if cat == 'Press Release':
        return 'deep', 'press release (default deep)'

    if any(p in t for p in ['earnings presentation', 'results of the company', 'result for the quarter']):
        return 'deep', 'results presentation'

    return 'deep', 'default deep'


def format_for_claude_review(items: list[dict], strategy_filter: tuple = ('skip', 'light')) -> str:
    """
    Format skip/light triage items compactly for Claude to review in-session.

    Claude reads this output and flags any items that look material despite
    being rule-classified as skip/light. Returns a plain text block.

    Each line: IDX | TICKER | STRATEGY | CATEGORY | attchmntText
    Claude outputs a list of indices to escalate to deep, with a one-line reason each.
    """
    filtered = [x for x in items if x.get('strategy') in strategy_filter or x.get('status') in strategy_filter]
    if not filtered:
        return "(no skip/light items to review)"

    lines = [
        f"[CLAUDE_DYNAMIC_REVIEW] {len(filtered)} skip/light items — scan for anything material.",
        f"Format: IDX | TICKER | STRATEGY | CATEGORY | attchmntText",
        f"Output escalations as: ESCALATE <idx> <idx> ... REASON: <one line>",
        f"Output nothing if all look correct.",
        "",
    ]
    for i, item in enumerate(filtered):
        ticker   = item.get('ticker', '?')
        strategy = item.get('strategy', item.get('status', '?'))
        category = item.get('category', '')[:40]
        text     = (item.get('attchmnt_text', '') or item.get('attchmntText', ''))[:200]
        lines.append(f"{i:4d} | {ticker:<12} | {strategy:<5} | {category:<40} | {text}")

    return "\n".join(lines)
