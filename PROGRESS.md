# Progress Log

## 2026-10-28 — Initial Build

### Done
- Built equity research agent with 33 tools: NSE (nselib), BSE (bselib), global (yfinance), technicals, valuation, news
- MCP server wired so tools run natively inside Claude
- Self-improvement loop: agent identifies gaps → writes new tools → saves to generated_tools/ → hot-loads next session
- SQLite memory: past research, insights, generated tools, task log
- Pushed to irtteja/equity-research-agent on GitHub
- Saved standing mission in MISSION.md and CLAUDE.md

### Tools available
- NSE: equity quote, price history, option chain, corporate actions, financial results, shareholding, index data, lookup
- BSE: quote, weekly H/L, meta, 12m price/volume, trading stats, advance/decline, gainers/losers, bhav copy, announcements, corporate actions, result calendar, results snapshot, scrip lookup
- Global: price history, quote, financial statements, key ratios, technical analysis, news/sentiment, DCF valuation, peer comparison

### Next sessions should
- Start macro monitoring: FII/DII flows, global indices, USD/INR, crude, gold
- Build announcement reader: pull BSE/NSE announcements daily and analyse impact
- Begin building tracked universe with sector-level views
- Add macro data tools (RBI, CPI, IIP)

### Known gaps / tools to build
- Macro indicator fetcher (RBI, MOSPI, Fed data)
- FII/DII flow tracker (daily)
- BSE announcement bulk reader + impact classifier
- Insider trading tracker
- Block deal / bulk deal monitor
- Promoter pledge tracker
- Sector rotation monitor

---

## 2026-10-29 — Daily Monitor

### Done
- Ran daily monitor job for 2026-10-29
- Macro snapshot captured (global indices, commodities, currencies)
- FII/DII flows for Oct 28: FII net +₹871.83 Cr, DII net +₹643.35 Cr — both net buyers
- Block deals: STLTECH — 6.02L shares at ₹887 (Session 2)
- 0 BSE announcements today
- Report saved to reports/daily/2026-10-29.json
- Git commit created (push pending — repo not in session's authorized set)

### Key observations
- Natural Gas spiked +12.24% — watch downstream impact on fertilisers, city gas distrib (IGL, MGL, Gujarat Gas)
- Crude Brent fell -3.11% to $110.34 — positive for India macro (current account, OMCs)
- USD/INR at 95.21, DXY -0.23% — slight INR stability
- Both FII and DII net buyers on Oct 28 — positive market breadth signal
- NIFTY BANK +0.54%, SENSEX -0.48% — banking outperforming broader market

### Next sessions should
- Push pending commit to GitHub (need repo added to session sources)
- Build BSE announcement impact classifier (NLP-based: positive/negative/neutral)
- Add insider trading tracker
- Build sector rotation monitor using NIFTY sectoral indices
- Start tracked universe build: at minimum 1-2 quality names per sector
- Investigate Natural Gas price spike and downstream stock impact
- Add RBI / macro data tools (CPI, IIP, policy rate)

---

## 2026-10-29 — Phase 1 Memory System

### Done
- **Investigated announcement sources**:
  - BSE website API (`api.bseindia.com`): Blocked from cloud IPs (Akamai 403)
  - `pip install bse` library: Works — `bse.announcements(scripcode=)` returns filings, `bse.session.get()` can download PDFs
  - NSE via nselib (`nse.announcements(symbol=)`): Works — returns full history with PDF URLs, `nse.download_document()` downloads PDFs reliably
  - **Decision**: NSE as primary source (we have symbols, no scrip code mapping needed)

- **Built Phase 1 memory system**:
  - `tools/pdf_parser.py`: NSE announcement fetch (date-filtered) + PDF download via nselib + pymupdf text extraction
  - `memory/store.py`: Append-only JSONL fact store under `memory_store/`, git-committed as durable truth
  - `memory/ingestion.py`: Full pipeline — hard-ignore list → PDF download → text extract → Claude triage + extraction → batch save
  - `memory_store/index/`: Initialized companies, events, contradictions indexes
  - Added pymupdf to requirements.txt

- **Key design decisions**:
  - Triage happens **after** PDF parse — never on headline alone (per design doc)
  - Hard-ignore only for provably-noise filings (trading window, compliance certs, etc.)
  - All facts append-only JSONL, never deleted
  - `memory/research.db` (SQLite) excluded from git — binary breaks on `git pull`

- **Daily monitor updated**: NSE announcement ingestion step wired in (step 3 of 5)

- **Pushed to GitHub**: commits b4f47b3, b89061a

### Known limitations (Phase 1)
- No semantic recall yet (no Kuzu graph, no LanceDB vectors) — facts saved but not queryable beyond raw file reads
- No BSE-to-NSE scrip code mapping (deferred, not needed since NSE works)
- `weekly_research.py` trigger still paused (analysis layer not ready)

### Next sessions should
- Run first real ingestion test on 1-2 companies with actual results filings
- Build `memory/recall.py` — simple fact retrieval by ticker + metric + period (Phase 1 can use JSON scan)
- Add Kuzu graph index (Phase 2) once facts start accumulating
- Add LanceDB vector index (Phase 2) for semantic recall
- Build `tools/recall_tool.py` to expose memory to the MCP server (so Claude can query it during analysis)
