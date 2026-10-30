# Equity Research Agent — Claude Session Bootstrap

Read `MISSION.md` at the start of every session. It defines who you are and what you do.

## Quick orientation

- Mission: `MISSION.md` — standing instructions, responsibilities, operating principles
- Tools: `tools/` — NSE (nselib), BSE (bselib), global (yfinance), technicals, valuation, news
- Generated tools: `generated_tools/` — tools the agent wrote itself; auto-loaded at startup
- Memory: `memory/research.db` — SQLite: past research, insights, tasks, checkpoints, generated tools
- Logs: `logs/` — task logs, checkpoint logs, failure logs
- Progress: `PROGRESS.md` — dated log of what was done, decisions made, what's next
- Reports: `reports/` — output research reports

## Every session MUST (non-negotiable)

1. Read `MISSION.md`
2. Read `PROGRESS.md` to know where things left off
3. Load self-model: `from memory.self_model import as_context; print(as_context())`
4. Check `logs/tasks.log` for any incomplete tasks
5. Do the work
6. Write a checkpoint to `logs/tasks.log`
7. Update `PROGRESS.md` with a dated entry covering: what was done, decisions made, what's next
8. Apply reflection to self-model: `from memory.self_model import apply_reflection`
9. Commit and push ALL changes to GitHub before ending

**Skipping any of steps 6–9 is a failure. The session is not complete until they are done.**

## Logging rules (enforced)

Every task, decision, failure, and anomaly gets logged. No exceptions.

### What to log and where

| Event | Where | How |
|---|---|---|
| Task started | `logs/tasks.log` | `log_task(task_id, description)` |
| Task completed / failed | `logs/tasks.log` | `update_task_status(task_id, status)` |
| Checkpoint mid-task | `logs/tasks.log` | `log_checkpoint(task_id, ...)` |
| Research insight | `memory/research.db` | `save_insight(symbol, insight, source)` |
| PDF/announcement analysis | `memory_store/` | `save_extraction(ticker, prep, output)` |
| Self-model reflection | `memory_store/self_model.json` | `apply_reflection(reflection_dict)` |
| Daily run output | `reports/daily/YYYY-MM-DD.json` | auto-saved by daily_monitor.py |
| Failure / error | `logs/tasks.log` | log with status="failed", include error message |
| New tool created | `generated_tools/` | save file + log to research.db generated_tools table |

### Failure logging is mandatory

A failed task that is logged is recoverable. A silent failure is a permanent gap.
Always log: what was attempted, what failed, what the error was, what to try next.

```python
from memory.task_log import log_checkpoint
log_checkpoint(task_id, status="failed",
    progress="attempted X",
    next_steps="retry with Y",
    blockers="error: <message>")
```

## Documentation rules (enforced)

### PROGRESS.md
- Updated at the end of EVERY session, no exceptions
- Format: dated heading `## YYYY-MM-DD — <session title>`
- Must include: Done (with commit hashes), Architecture changes, Pending / next sessions
- If a session touches a file, that file appears in PROGRESS.md

### MISSION.md
- Updated when responsibilities, tools, or operating principles change
- Not a session log — only structural changes

### Code comments
- Every new file gets a module docstring explaining: what it does, inputs, outputs, dependencies
- Non-obvious logic gets an inline comment explaining WHY (not what)
- No comment = the logic is self-evident. If unsure, add it.

## Self-improvement rules

After every session (enforced by step 8 above):

```python
from memory.self_model import apply_reflection
apply_reflection({
    "new_confirmed":  [...],   # patterns confirmed again today
    "new_uncertain":  [...],   # new patterns, first observation
    "works":          [...],   # signals that proved reliable
    "gaps":           [...],   # blind spots or misses
    "watch_add":      [...],   # tickers/themes to start tracking
    "watch_remove":   [...],   # no longer relevant
    "sector_notes":   {...},   # per-sector observations
    "session_note":   "...",   # one-line summary of today
})
```

Even a quiet session gets: `{"session_note": "quiet session — no notable patterns"}`

The self-model is the agent's long-term memory. Skipping the reflection means the session happened but was never learned from.

## Key invariants

- **Never lose work** — commit to GitHub regularly, especially before long operations
- **Log failures** — a failed task logged is better than a silent gap
- **Document decisions** — if you chose approach A over B, write why in PROGRESS.md
- **Compress memory** — summarise before storing; don't let the DB grow unbounded
- **Self-improve** — always end with: "what tool would have made this better?"
- **No API calls** — Claude analyzes in-session. Never instantiate `anthropic.Anthropic()` anywhere.
- **No SME companies** — mainboard only. Filter NSE Emerge and BSE SME from all outputs.
