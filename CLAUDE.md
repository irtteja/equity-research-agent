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

## Every session must

1. Read `MISSION.md`
2. Read `PROGRESS.md` to know where things left off
3. Check `logs/tasks.log` for any incomplete tasks
4. Do the work
5. Write a checkpoint to `logs/tasks.log`
6. Update `PROGRESS.md`
7. Commit and push to GitHub before ending

## Key invariants

- Never lose work — commit to GitHub regularly, especially before long operations
- Log failures — a failed task logged is better than a silent gap
- Compress memory — summarise before storing; don't let the DB grow unbounded
- Self-improve — always end with: "what tool would have made this better?"
