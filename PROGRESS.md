# Progress Log

## 2026-10-31 — Recursive self-improvement loop closed; all 4 gaps addressed

### Done
- **Task 1 — 5 core scheduled tasks enabled**: equity-daily-monitor, NSE Daily Announcement Fetch,
  Results Fetch Nightly XBRL, Results Fetch 8 AM Retry, equity-watchdog. Also created new
  "ContextAgent Nightly Enrichment" task (2:30 AM IST Mon-Sat) running context_agent_runner + trend_agent_runner.
- **Task 2 — Graph backfill**: `jobs/backfill_graph.py` loads all stored XBRL results into graph.
  Fixed `episode_bridge.result_to_graph()` to handle both nselib API field names and stored JSONL
  field names (revenue_from_operations, pat, eps_basic vs revenue, netProfit, eps).
  Result: 121 MetricValue nodes, 121 HAS_VALUE edges, 2 tickers (BIRLACORPN, CDSL), 13 quarters.
- **Task 3 — TrendAgent**: `agents/trend_agent.py` walks PRECEDED_BY edge chains, computes
  momentum score [-1,1], direction streak, and acceleration (accelerating/decelerating/steady).
  Writes TrendSummary nodes with HAS_TREND + TREND_OF edges.
  `jobs/trend_agent_runner.py` — scheduled runner.
  First run: 20 (ticker,metric) pairs → 20 TrendSummary nodes → 5 strong trends.
  Strong signals: CDSL revenue +11.4% (steady), CDSL total_expenses +6.2% (accelerating).
- **Task 4 — Agent quality loop closed**: `_signal_agent_quality()` in self_improve.py scores
  the agents themselves, not just the data pipeline:
  - ContextAgent enrichment rate < 50% → gap
  - TrendSummary nodes absent or stale > 7 days → gap
  - CORROBORATES/CONTRADICTS accuracy < 60% → gap
- Commit: 60658a9

### Architecture changes
- **Recursive loop fully closed**: data → graph → ContextAgent → TrendAgent → self_improve
  scores agent quality → patches if degraded → repeat. Every layer now self-improves.
- **Pipeline verified end-to-end**: backfill → 100 PRECEDED_BY edges → 20 trend summaries →
  self_improve scan detects agent gaps → corrects.
- **New node types**: TrendSummary (momentum, streak, acceleration)
- **New edge types**: HAS_TREND (Company → TrendSummary), TREND_OF (TrendSummary → MetricValue)
- **Graph state**: 144 nodes, 261 edges, 251 episodes

### Pending / next sessions
- AlertAgent — reads TrendSummary nodes, decides what to surface to user (material signals)
- Outcome scoring wired to ContextAgent sentiment edges (accuracy feedback)
- Expand tracked universe beyond BIRLACORPN + CDSL (XBRL fetch nightly now running)

---

## 2026-10-31 — Knowledge graph layer + ContextAgent (MiroFish-inspired ripple architecture)

### Done
- `memory/graph_store.py` (NEW): SQLite knowledge graph — nodes, edges, episodes tables.
  GraphStore with upsert/idempotent semantics, `get_metric_history()`, CLI interface.
  DB: `memory_store/graph.db`. Module-level singleton `get_store()`.
- `memory/episode_bridge.py` (NEW): converts facts, XBRL results, management statements,
  gaps, patches, and task events into graph nodes + episodes. Entry point for all writes.
  `fact_to_graph()`, `result_to_graph()`, `statement_to_graph()`, `gap_to_graph()`, `patch_to_graph()`.
- `agents/__init__.py` (NEW): agents package.
- `agents/context_agent.py` (NEW): enriches MetricValue nodes with PRECEDED_BY edges
  (delta + direction between consecutive periods). Links ManagementStatement nodes via
  CORROBORATES/CONTRADICTS. Marks nodes enriched after processing.
- `jobs/context_agent_runner.py` (NEW): scheduled runner — `python jobs/context_agent_runner.py`.
- `memory/ingestion.py`: wired `facts_to_graph()` + `statement_to_graph()` into `save_extraction()`.
  Every new numeric fact and management statement auto-writes to graph on ingestion.
- `memory/results_store.py`: wired `result_to_graph()` into `save_results()`.
  Every new XBRL quarter auto-writes MetricValue nodes to graph.
- `jobs/self_improve.py`: wired `gap_to_graph()` into `run_improvement_scan()`.
  Every detected gap becomes a Gap node in the graph.
- Commit: cf66343

### Architecture changes
- **Ripple architecture**: every significant event (fact ingested, XBRL result saved, gap detected)
  now writes to the shared knowledge graph as an episode. Agents interact via the graph, not
  direct calls. ContextAgent reads unenriched MetricValue nodes → adds temporal edges → future
  agents (TrendAgent, AlertAgent) traverse edge chains for acceleration/deceleration signals.
- **Recursive self-improvement**: graph grows → agents see more context → better analysis →
  more precise gaps → more precise patches → repeat. Modeled on MiroFish ripple propagation.
- **Node types active**: Company, MetricValue, ManagementStatement, Gap, Patch
- **Edge types active**: HAS_VALUE, PRECEDED_BY, CORROBORATES, CONTRADICTS, FIXED_BY

### Pending / next sessions
- Enable 5 core scheduled tasks (equity-daily-monitor, NSE fetch, XBRL fetch, watchdog)
- TrendAgent: detect metric acceleration/deceleration from PRECEDED_BY edge chains
- AlertAgent: surface high-delta + management statement confirmation signals
- Outcome scoring Phase 2: reconcile triage decisions against price moves
- Backfill graph with existing stored XBRL results + facts (one-shot script)

---

## 2026-10-30 — Metacognitive self_improve scan: data-driven with persistence tracking

### Done
- `jobs/self_improve.py`: replaced hardcoded gap list with 6 data-driven signal readers:
  - `_signal_triage_health()` — reads outcome_log.jsonl; flags if >90% skip/deep rate or accuracy <70%
  - `_signal_test_failures()` — reads self_improve_log.jsonl; flags tests failing in 2+ of last 5 runs
  - `_signal_task_errors()` — reads tasks.log; flags jobs with ≥50% failure rate
  - `_signal_pipeline_freshness()` — checks staleness of bhavcopy/indices/announcements/event_calendar
  - `_signal_data_coverage()` — XBRL coverage: flags if <30% of tracked symbols have results
  - `_gap_persistence()` — compares against last 5 scans; medium→high after 3 consecutive; "STUCK" at 5+
- Gap resolution detection: areas in last scan absent from current scan logged as resolved → self_model `new_confirmed`
- First run: 3 gaps resolved (bse_integration, document_swarm, scheduled_tasks), 2 remaining (pipeline_never_run:event_calendar, xbrl_coverage)

### Architecture changes
- Self-improve scan is now fully metacognitive: runtime data drives gap discovery, persistence tracking
  escalates chronic issues, resolved gaps reinforce the self-model. No hardcoded assertions remain.

### Pending / next sessions
- Enable 14 scheduled tasks (highest priority — system not live; event_calendar pipeline has never run)
- XBRL coverage: 2/873 symbols (0%) — needs `fetch_results.py` to run live to accumulate data
- Document swarm: only 2 docs ingested — will grow as daily concall ingestion runs

## 2026-10-30 — Self-iterate: results pipeline fix + outcome logging

### Done
- `jobs/fetch_results.py`: fixed date-parsing bug — NSE event_calendar returns "31-Oct-2026"
  not ISO "2026-10-31". `date.fromisoformat()` silently failed on every event, so no results
  were ever fetched. Fixed with `strptime("%d-%b-%Y")` fallback. Verified: CDSL 7q, BIRLACORPN
  6q fetched and stored correctly.
- `memory/outcome_log.py`: new module — logs every triage decision (skip/light/deep) with
  timestamp, symbol, rationale, confidence. `reconcile()` and `summary()` for Phase 2 scoring.
  Storage: `memory_store/outcome_log.jsonl`.
- `memory/ingestion.py`: wired `log_outcome()` into `prepare_announcement()` (skip + light
  paths) and `save_extraction()` (Claude's final triage call). Every decision now logged.
- Self-improve scan: 5 gaps → 3. `results_pipeline` and `outcome_logging` resolved.

### Architecture changes
- Triage decisions are now persistent: every skip/light/deep call writes to outcome_log.jsonl
  with timestamp. Future: reconcile against price moves to score accuracy.

### Pending / next sessions
- Enable 14 scheduled tasks (highest priority — system not live)
- Document swarm: only 2 docs ingested — needs annual reports or more concall runs
- BSE announcements: not necessary — universe is NSE mainboard, same filings cross-listed on both exchanges (duplicate data). Deprioritised permanently.

## 2026-10-30 — Concall auto-ingestion wired end-to-end

### Done
- `jobs/ingest_concalls.py`: reads saved announcement JSON, finds concall PDFs
  (category "Analysts/Institutional Investor Meet/Con. Call Updates"), filters to
  tracked-universe symbols and actual content PDFs (transcripts/recordings vs schedule
  notices), downloads, and feeds to document_swarm. Dedup by content hash — safe to re-run.
- `jobs/fetch_announcements.py`: wired to call `ingest_concalls()` automatically after
  `--save` — every daily run now ingests concall transcripts with zero manual steps.
- Two-stage filter: (1) content keywords (transcript/recording/audio/concall) vs schedule
  keywords (intimation/invite/schedule); (2) tracked-universe only (873 mainboard symbols).
  Results-season days: ~90 PDFs. Normal days: 5–15 PDFs.

### Architecture changes
- Daily flow: fetch_announcements --save → save JSON → ingest_concalls → download PDFs
  → document_swarm → memory_store/documents/ → searchable via recall_search_documents

### Pending / next sessions
- Monitor first live run to validate ingest quality
- `memory/outcome_log.py` — log triage decisions for ground-truth reconciliation
- BSE announcements integration
- Enable 14 scheduled tasks when ready to go live

## 2026-10-30 — Concall transcript access + memory compaction

### Done
- `tools/announcements.py`: added `get_concall_transcripts(symbol, days_back)` — fetches NSE
  announcements with category "Analysts/Institutional Investor Meet/Con. Call Updates"; each
  filing carries `pdf_url` pointing to the actual transcript/presentation PDF on NSE archives.
  Confirmed 8 filings for RELIANCE in last 90 days alone.
- `tools/__init__.py`: exported `get_concall_transcripts`; registered in `CORE_TOOLS` (now 44 tools)
- `jobs/prune_memory.py` (af6da42): operational memory compaction — prunes tasks.log, self_improve_log,
  research.db rows (tasks/checkpoints/macro >90d, knowledge_base >365d), reports/daily >90d,
  stale index files. Scheduled monthly 1st of month 01:47 IST (trig_01QityzjMGJM8PGBuS5JZ5bs).
- `CLAUDE.md` (2bbc733): hardened — logging/documentation/self-improvement now mandatory rules,
  not aspirational. Steps 6–9 explicitly non-negotiable.

### Architecture changes
- `get_concall_transcripts` is the discovery layer. Use it to find PDFs, then pass URLs to
  `document_swarm.py` to ingest, chunk, and index them into memory_store/documents/.
- Two retention jobs now run monthly: `cleanup_store.py` (market data) + `prune_memory.py` (operational)

### Pending / next sessions
- Wire concall auto-ingestion into `jobs/fetch_announcements.py` — when a new concall filing
  appears, auto-download PDF and feed to document_swarm (currently manual)
- `memory/outcome_log.py` — log triage decisions + timestamps for ground-truth reconciliation
- BSE announcements integration (currently NSE-only pipeline)
- Enable 14 scheduled tasks when ready to go live
- Run `fetch_results.py` against a day with known results to verify XBRL pipeline end-to-end

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

---

## 2026-10-29 — Bhavcopy + Universe Enrichment

### Done
- **BSE scrip code mapping**: Used ISIN as key — nselib bhavcopy bulk → ISIN, `pip install bse` listSecurities → scrip code. Matched 745/748 companies (3 NSE-only: DIACABS, E2E, MBAPL)
- **Removed REITs**: Dropped EMBASSY, BIRET, BAGMANE from universe (748 companies remain)
- **Universe enriched**: `data/tracked_universe.json` now has `isin`, `bse_code`, `bse_group` per company
- **jobs/save_bhavcopy.py**: Downloads NSE equity bhavcopy, filters EQ+BE (no SME), keeps tracked universe only, saves to `memory_store/bhavcopy/YYYY-MM-DD.csv`. CLI supports `--backfill N` and `--date YYYY-MM-DD`
- **daily_monitor.py updated**: Bhavcopy save wired as step 4/5

### Next sessions should
- Run backfill for past ~1 year of trading days: `python jobs/save_bhavcopy.py --backfill 365`
- Build `memory/recall.py` — simple fact retrieval by ticker + metric + period (Phase 1 JSON scan)
- Run first real ingestion test on 1-2 companies with actual results filings
- Add Kuzu graph + LanceDB vector index (Phase 2) once facts accumulate

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

---

## 2026-10-29 — Index Constituents, Universe Expansion, Holiday Fix

### Done

- **Index constituent lists**: Built `jobs/save_index_constituents.py` — downloads constituent lists for 86 of 139 NSE indices (53 skipped: factor/strategy, ESG, leverage/inverse, bonds, SME, Tata-branded). Any new index NSE adds that doesn't match skip patterns is auto-included.
  - `data/index_constituents/` — 86 JSON files, one per included index
  - `data/symbol_to_indices.json` — reverse lookup: 944 symbols → index membership list
  - `tracked_universe.json` enriched: every company now has `indices` field

- **Universe expansion 748 → 873 companies** (+125):
  - NIFTY IPO constituents (~100 recently listed stocks)
  - NIFTY WAVES constituents (~30 media/entertainment stocks)
  - PSB (public sector banks) — BANKBARODA, BANKINDIA, CANARABANK, UNIONBANK, etc.
  - New entries have `isin`, `bse_code`, `bse_group`, `indices` — 2 NSE-only (CRESTO, GRADIENTE) noted with `bse_note`

- **INDIA VIX in macro snapshot**: Added `get_india_vix()` to `tools/macro.py`, wired into `get_full_macro_snapshot()` return. Daily monitor prints VIX with 52-week range.

- **NSE indices bhavcopy**: Built `jobs/save_indices_bhavcopy.py` — downloads OHLCV + P/E, P/B, DivYield for all 167 NSE indices per day. Saves to `memory_store/indices_bhavcopy/YYYY-MM-DD.csv`. Backfilled 245 trading days (2025-10-30 → 2026-10-28, holidays correctly absent).

- **Holiday data quality fix**: NSE equity bhavcopy API serves previous day's data on market holidays. Deleted 16 stale holiday files from backfill. Added `TradDt` date-validation in `fetch_and_save()` — if downloaded data's trade date ≠ requested date, returns `{status: "holiday"}` and skips file creation. Both equity and indices bhavcopy now have 245 clean trading-day files.

- **Bhavcopy counts**: equity bhavcopy 245 files, indices bhavcopy 245 files — aligned.

### Key design decisions
- Holiday detection: self-healing via `TradDt` validation (no static holiday calendar needed)
- Skip patterns: regex-based auto-exclude for non-equity indices; new NSE indices auto-included
- INDIA VIX: no equity constituents — kept as macro signal only (live quote via `index_quote`)

### Next sessions should
- Run first real ingestion test on 1-2 companies with actual results filings
- Build `memory/recall.py` — fact retrieval by ticker + metric + period (Phase 1 JSON scan)
- Add Kuzu graph + LanceDB vector index (Phase 2) once facts accumulate
- Build `tools/recall_tool.py` to expose memory to MCP server (so Claude can query it during analysis)
- Consider adding RBI / macro data tools (CPI, IIP, policy rate)

---

## 2026-10-29 — Parallel Announcement Fetch

### Done
- **Benchmarked parallel announcement fetch**: 873 symbols, 5 workers, 179s, 216 symbols with announcements, 418 total records, 0 errors
- **Built `jobs/fetch_announcements.py`**: `fetch_announcements_parallel()` — 5 workers, full universe, saves to `memory_store/announcements/YYYY-MM-DD.json`. CLI supports `--date`, `--days-back`, `--save`.
- **Refactored daily_monitor.py step 3**: Parallel fetch first (fast, all symbols), then Claude ingestion only for the ~200 symbols that actually filed — avoids 873-symbol serial scan
- **Key findings on NSE data sources**:
  - NSE market-wide API hardcapped at 20 records (ignores date params) — useless for daily scan
  - NSE RSS (`nsearchives.nseindia.com/content/RSS/Online_announcements.xml`): 1,498+ items/day, refreshes every 5 min, accessible from cloud IPs — good for intraday monitoring
  - Per-symbol API (nselib): accurate, all fields, best for history and ingestion
  - BSE wins only for category-specific short-window queries (server-side filtering); nselib wins overall
- **Pushed**: commit 197b877

### Next sessions should
- Build `memory/recall.py` — fact retrieval by ticker + metric + period (Phase 1 JSON scan)
- Run first real ingestion test on 2-3 companies with results filings (check memory_store/announcements/ for today's batch)
- Add Kuzu graph + LanceDB vector index (Phase 2) once facts accumulate
- Build `tools/recall_tool.py` to expose memory to MCP server

---

## 2026-10-29 — Daily Announcement Pipeline

### Done
- **Scheduled task**: `NSE Daily Announcement Fetch` — runs at 1:00 AM IST every day
  - Step 1: parallel fetch (873 symbols, 5 workers, ~3 min) for yesterday → `memory_store/announcements/YYYY-MM-DD.json`
  - Step 2: Claude ingestion on ~200 symbols that actually filed → facts saved to memory store
- **Deleted**: hourly fetch (redundant), overnight fetch (merged into single 1 AM job), RSS monitor + loop files

### Next sessions should
- Build `memory/recall.py` — fact retrieval by ticker + metric + period (Phase 1 JSON scan)
- Run first real ingestion test — check tonight's 1 AM run output
- Add Kuzu graph + LanceDB vector index (Phase 2) once facts accumulate
- Build `tools/recall_tool.py` to expose memory to MCP server

---

## 2026-10-29 — Two-Pass Triage Validation

### Done
- **Two-pass in-session triage on today's 502 NSE announcements** (2026-10-29):
  - Read all 502 items' attchmntText directly (no API calls, Claude reads in-session)
  - Pass 1: rule-based attchmntText triage → deep=214, light=80, skip=208
  - Pass 2: cross-check all items for errors → **98.8% accuracy (496/502 correct)**
  - 6 errors identified: 5 × `Action(s) taken or orders passed` misclassified LIGHT→DEEP, 1 × newspaper publication with financial results text misclassified SKIP→DEEP
  - All 6 errors fixed in the corrected rule set

- **Key finding validated**: attchmntText catches 55 material items hidden in "General Updates"/"Updates" categories that pure category-label routing would miss:
  - ASIANPAINT interim dividend Rs.5.75 (under "Updates")
  - NTPC BESS 250MW commercial operation (under "Updates")
  - MOTILALOFS NCD ₹3,000 Cr board approval (under "General Updates")
  - LAURUSLABS acquisition of Galenvs Sciences (under "General Updates")
  - JWL order ₹1,140 Cr wagons (under "General Updates")
  - NBCC E-auction ₹5,191 Cr (under "Updates")
  - NTPCGREEN commercial operation declaration (under "General Updates")
  - ... and 48 more

- **`memory/triage.py`**: New module with corrected attchmntText rules (validates at 98.8%)
- **`memory/ingestion.py`**: Wired triage pre-filter — SKIP items now return early before PDF download
  - Saves ~40% of PDF download cost (208/502 = 41% are skip)
- **`reports/triage_test.json`**: Full triage validation report (502 items, all decisions, 6 errors)

### Triage stats (2026-10-29)
| Strategy | Count | % | What it means |
|----------|-------|---|---------------|
| deep     | 214   | 43%| Download PDF + full extraction |
| light    | 80    | 16%| Key fact already in text, light extraction |
| skip     | 208   | 41%| Zero analytical value, skip entirely |

### Next sessions should
- Build `memory/recall.py` — fact retrieval by ticker + metric + period (Phase 1 JSON scan)
- Wire `ANTHROPIC_API_KEY` into environment secrets so scheduled ingestion tasks can call Claude API
- Run first real ingestion test on 2-3 deep items (AMBUJACEM results, EQUITASBNK results)
- Add `rationale` block to deep extraction prompt: significance, vs_expectations, key_takeaway, watch_for
- Build `tools/recall_tool.py` to expose memory to MCP server

---

## 2026-10-30 — XBRL Results Pipeline

### Done

- **Tesseract OCR fallback in `tools/pdf_parser.py`** (commit aacd1f7):
  - `_extract_text_tesseract()`: renders each page at 2x resolution (pymupdf.Matrix), runs pytesseract
  - Auto-triggers when pymupdf returns <50 chars (scanned PDF detection)
  - Graceful degradation if pytesseract not installed; returns `ocr_error` key
  - `memory/ingestion.py` updated to distinguish `scanned_pdf_ocr_failed` (retryable) from `pdf_empty_or_image_only` (skip)

- **`memory/results_store.py`** — JSONL-backed XBRL quarterly results store (commit f38e6bb):
  - `STORE_DIR = memory_store/results/`; one `<TICKER>.jsonl` per company, one line per quarter
  - `save_results(symbol, quarters)` — idempotent (period_end as unique key), appends, updates `_index.json`
  - `get_results(symbol)` — all quarters oldest-first
  - `get_latest(symbol)` — most recent quarter
  - `get_deltas(symbol)` — QoQ deltas for revenue_from_operations, pat, ebit, total_income, total_expenses, eps_basic
  - `get_index()` / `list_symbols()` — summary index helpers

- **`jobs/fetch_results.py`** — nightly parallel results fetch (commit f38e6bb):
  - Reads `memory_store/event_calendar/YYYY-MM-DD.json` → filters financial results events
  - Parallel `financial_results_detail()` at 10 workers (~5s for 50 companies)
  - Saves via `results_store.save_results()`
  - Writes `_pending_retry.json` for companies that haven't filed by 1 AM
  - `--retry` flag for 8 AM pass (reads retry file, cleans up after)

- **`jobs/daily_monitor.py` step 0** — event_calendar save (commit f38e6bb):
  - New step 0/5: fetches `nse.event_calendar()`, saves to `memory_store/event_calendar/YYYY-MM-DD.json`
  - Counts upcoming results events — gives full results season visibility 2-3 weeks ahead

- **Two scheduled tasks created**:
  - `Results Fetch — Nightly XBRL` (trig_01FuxqGR8MtthLn6fgdtB6Pc): 1:15 AM IST Mon-Sat
  - `Results Fetch — 8 AM Retry` (trig_011qbj3PujDhjKBz11wx5tom): 7:40 AM IST Mon-Sat

### Data flow
```
daily_monitor (4:31 PM IST) → memory_store/event_calendar/YYYY-MM-DD.json
fetch_results (1:15 AM IST) → reads calendar → parallel XBRL fetch → memory_store/results/<TICKER>.jsonl
fetch_results --retry (7:40 AM IST) → retries not-yet-filed companies
```

### Next sessions should
- Build `memory/recall.py` — fact retrieval layer: results_store QoQ deltas + qualitative facts search
- Build `tools/recall_tool.py` — expose memory to MCP so Claude can query during analysis
- Test: run `python jobs/fetch_results.py` manually against a day with known results
- Add Kuzu graph + LanceDB vector index (Phase 2) once facts accumulate

---

## 2026-10-30 — Recall, Ingestion Wiring, Self-Model, Doc Swarm, E2E Test

### Done

#### Retrieval layer — `memory/recall.py` (commit bfba60c)
Every function returns `{data, text}` — text is pre-formatted for direct Claude injection.
- `get_price_series(symbol, days)` — reads bhavcopy CSVs, last N trading days
- `get_results(symbol, n)` — reads `memory_store/results/<TICKER>.jsonl`, last N quarters
- `get_announcements(symbol, days)` — SQLite first, raw JSON fallback
- `get_insights(symbol)` — knowledge_base table in research.db
- `get_documents(symbol, doc_type)` — doc_store search_index
- `search_documents(symbol, query)` — keyword regex across chunks
- `get_macro(date)` — macro_snapshots table
- `search_facts(query, symbol)` — cross-source: announcements + knowledge_base
- `company_snapshot(symbol)` — assembles all of above into one formatted block

#### Context-aware analysis — `memory/ingestion.py` (commit 31c119b)
Before building the extraction prompt, now injects:
- `_build_company_context(ticker)` — last 30d prices + last 4 quarters results + prior insights
- `_get_self_model_context()` — agent's accumulated patterns from self_model
Both fail gracefully and never break the pipeline.
Extraction prompt template now leads with `{self_model_context}` and `What we already know about {ticker}`.

#### Document ingestion swarm — `agents/document_swarm.py` + `memory/doc_store.py` (commit 4630307)
Multi-format parallel worker pool. Supported: PDF, XLSX/CSV, DOCX, TXT/MD/HTML.
- `agents/workers/classifier.py` — rule-based symbol + doc_type + period detection (no LLM)
- `agents/workers/pdf_worker.py` — pymupdf direct + tesseract OCR fallback
- `agents/workers/excel_worker.py` — openpyxl + csv
- `agents/workers/docx_worker.py` — python-docx
- `agents/workers/text_worker.py` — plain text + HTML strip
- SHA-256 content dedup: same file ingested twice = silently skipped
- Storage: `memory_store/documents/{SYMBOL}/{doc_type}/YYYY-MM-DD_{slug}_{hash}.json`
- `_doc_index.jsonl` as master index; `search_index()`, `load_document()`, `summary()`
- CLI: `python agents/document_swarm.py /path/ --workers 16 --symbol RELIANCE`

#### Self-model layer — `memory/self_model.py` + `jobs/daily_monitor.py` + `MISSION.md` (commit b987771)
Persistent context layer: agent writes observations → injected into every future session.
- Storage: `memory_store/self_model.json` — confirmed/uncertain patterns, calibration, watch list, sector notes, session log (last 30)
- `apply_reflection(reflection_dict)` — merges Claude's post-run JSON into model
- `as_context()` — formatted block for system prompt injection
- `build_reflection_prompt(session_results)` — end-of-day prompt that tells Claude what to output
- `daily_monitor.py` step 6 now prints reflection prompt + current model after every run
- `MISSION.md` updated: load self-model at session start, apply reflection at end of daily run

#### E2E test suite — `tests/test_e2e.py` (commit c35e6ad)
4-layer smoke test using real data already in memory_store — no live API calls.
- Layer 1 (Recall): prices, results, announcements, snapshot
- Layer 2 (Ingestion): real announcement + PDF download + context-injected prompt
- Layer 3 (Self-model): apply_reflection, as_context, cleanup
- Layer 4 (Doc Swarm): ingest, dedup, search, retrieve
- All 4 layers pass on RELIANCE
- Run: `python tests/test_e2e.py --symbol RELIANCE --verbose`

#### Bug fix — `tools/__init__.py` lazy import (commit c35e6ad)
`pandas_ta` imports `numba` which requires NumPy ≤ 2.2 (system has 2.5).
Wrapped `run_technical_analysis` in a lazy loader to defer the import until actual call.

### Architecture state (as of 2026-10-30)

```
Data fetch layer (complete)
  daily_monitor.py → macro, FII/DII, announcements, bhavcopy, block/bulk deals
  fetch_results.py → XBRL quarterly results (1:15 AM + 7:40 AM retry)
  fetch_announcements.py → parallel NSE announcement fetch (873 symbols)

Storage layer (complete)
  memory_store/bhavcopy/        — daily equity prices (CSV)
  memory_store/results/         — XBRL quarterly results (JSONL per symbol)
  memory_store/announcements/   — raw daily announcement JSON
  memory_store/documents/       — doc swarm output (PDF/XLSX/DOCX/TXT)
  memory_store/self_model.json  — agent self-model
  memory/research.db            — SQLite: insights, tasks, macro snapshots

Retrieval layer (complete)
  memory/recall.py              — unified query interface, {data, text} responses

Analysis layer (wired, but Claude does extraction in-session)
  memory/ingestion.py           — prepare_announcement() with context injection
  memory/triage.py              — rule-based deep/light/skip pre-filter
  memory/self_model.py          — accumulated agent patterns

Document ingestion (complete)
  agents/document_swarm.py      — parallel multi-format ingestor
  memory/doc_store.py           — storage + retrieval + dedup
```

#### Recall MCP tools — `tools/recall_tool.py` (commit 866808c)
10 recall functions registered as MCP tools in `mcp_server.py`:
`recall_company_snapshot`, `recall_prices`, `recall_results`, `recall_announcements`,
`recall_insights`, `recall_documents`, `recall_search_documents`, `recall_macro`,
`recall_search_facts`, `recall_self_model`.
Claude can now pull any fact from memory mid-session on demand — not just pre-loaded context at prompt-build time. Pull vs push.

#### Self-improvement module — `jobs/self_improve.py` (commit 866808c)
Three-phase cycle: **HEAL → TEST → IMPROVE**
- HEAL: 5 auto-healers (missing dirs, broken doc index, stale results index, empty logs, self-model schema repair)
- TEST: runs e2e suite, logs pass/fail counts to `memory_store/self_improve_log.jsonl`
- IMPROVE: scans for real gaps, writes high-priority findings directly into self-model as calibration gaps
- Scheduled: every Sunday 9 AM IST (trig_01Tzzrex6wf75k5fti7ec9ZP)
- Run anytime: `python jobs/self_improve.py`

#### Retention cleanup — `jobs/cleanup_store.py` (commit b2e94ab)
Monthly pruner (1st of month, 2 AM IST). Enforces:
- bhavcopy/ + indices_bhavcopy/ + announcements/ → 365-day rolling window
- documents/ → 3-year rolling window
- results/, self_model.json, research.db → permanent

### Pending / next sessions
- Run `fetch_results.py` manually against a day with known results — verify XBRL pipeline end-to-end
- BSE announcements integration — currently NSE-only
- Annual reports auto-fetch — URL pattern known, pipeline not yet built
- `tools/recall_tool.py` — expose recall as MCP tool for in-session queries
- Phase 2 memory: Kuzu graph + LanceDB vector index (once facts accumulate)
- Weekly research job (`jobs/weekly_research.py`) — still disabled
- Re-enable all scheduled tasks when ready to go live (14 tasks currently disabled)
