"""
Morning Research Brief — 4 swarm agents run in parallel, Orchestrator synthesizes.

Agents:
  1. MarketAnalyst   → NIFTY overview + top movers
  2. MarketAnalyst   → Deep dive on 3 high-interest tickers (RELIANCE, INFY, HDFCBANK)
  3. FundamentalsAnalyst → Today's NSE announcements + results
  4. FundamentalsAnalyst → Corporate actions (dividends, splits, board meetings)

Orchestrator synthesizes all 4 into a morning brief.
"""
import sys, os, asyncio, time

sys.path.insert(0, "/home/user/agency-swarm/src")
sys.path.insert(1, "/usr/local/lib/python3.13/site-packages")
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/../vendor")

from agency_swarm import Agency, Runner, set_tracing_disabled
from agency_swarm.context import MasterContext
from swarm.agents.market_analyst        import build_market_analyst
from swarm.agents.fundamentals_analyst  import build_fundamentals_analyst
from swarm.agents.orchestrator          import build_orchestrator

set_tracing_disabled(True)


def _banner(title: str):
    w = 64
    print(f"\n╔{'═'*(w-2)}╗")
    print(f"║  {title:<{w-4}}║")
    print(f"╚{'═'*(w-2)}╝")


async def run_task(agent, query: str, label: str, ctx: MasterContext) -> tuple[str, str]:
    t0 = time.time()
    result = await Runner.run(agent, query, context=ctx)
    elapsed = time.time() - t0
    text = result.final_output or "(no output)"
    print(f"  ✓ {label} ({elapsed:.1f}s)")
    return label, text


async def main():
    _banner("NSE EQUITY RESEARCH SWARM  —  MORNING BRIEF")
    print(f"  Building agents...")

    market      = build_market_analyst()
    fundamentals = build_fundamentals_analyst()
    orchestrator = build_orchestrator(market, fundamentals)

    agency = Agency(
        orchestrator,
        communication_flows=[
            (orchestrator, market),
            (orchestrator, fundamentals),
        ],
    )

    ctx = MasterContext(
        agents=agency.agents,
        thread_manager=agency.thread_manager,
        user_context={},
        agent_runtime_state=agency._agent_runtime_state,
    )

    print(f"  Launching 4 parallel agent tasks...\n")
    t_start = time.time()

    # 4 tasks run concurrently
    tasks = await asyncio.gather(
        run_task(market, "Give me NIFTY 50 index overview and today's market status", "MarketAnalyst:NIFTY", ctx),
        run_task(market, "What are the prices and momentum for RELIANCE, INFY, HDFCBANK? Compare them.",
                 "MarketAnalyst:Tickers", ctx),
        run_task(fundamentals, "What are today's most important NSE announcements? Board meetings and results only.",
                 "FundamentalsAnalyst:Announcements", ctx),
        run_task(fundamentals, "Show me corporate actions this week — dividends, splits, bonus.",
                 "FundamentalsAnalyst:CorpActions", ctx),
    )

    elapsed_total = time.time() - t_start
    print(f"\n  All agents done in {elapsed_total:.1f}s\n")

    # Print individual outputs
    for label, output in tasks:
        _banner(label)
        print(output)

    # Synthesize with orchestrator
    combined_input = "\n\n---\n\n".join(
        f"[{label}]\n{output}" for label, output in tasks
    )

    _banner("ORCHESTRATOR  —  SYNTHESIS")
    synthesis_query = (
        "You have received research from 4 agents. Synthesize into a crisp morning brief:\n\n"
        "1. Market Pulse (NIFTY + key tickers)\n"
        "2. Key Events Today (announcements + corporate actions)\n"
        "3. Watch List (1-2 names worth watching and why)\n\n"
        f"Agent outputs:\n\n{combined_input[:3000]}"
    )

    t2 = time.time()
    result = await Runner.run(orchestrator, synthesis_query, context=ctx)
    print(result.final_output or "(no synthesis output)")
    print(f"\n  Synthesis: {time.time()-t2:.1f}s  |  Total: {time.time()-t_start:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
