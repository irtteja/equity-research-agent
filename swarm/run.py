"""
NSE Equity Research Agent Swarm — interactive runner.

Usage:
    cd /home/user/equity-research-agent
    python3 swarm/run.py

    # or single query:
    python3 swarm/run.py --query "What is the price of RELIANCE?"
    python3 swarm/run.py --agent market       # talk directly to MarketAnalyst
    python3 swarm/run.py --agent fundamentals # talk directly to FundamentalsAnalyst
"""
import sys, os, argparse, asyncio, traceback

# Paths — agency-swarm MUST come before the equity-research-agent root
# to avoid the local agents/ folder shadowing the openai-agents SDK.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
sys.path.insert(0, "/home/user/agency-swarm/src")           # agency_swarm package
sys.path.insert(1, "/usr/local/lib/python3.13/site-packages") # openai-agents SDK
# equity-research-agent root added AFTER so it doesn't shadow 'agents'
if _ROOT not in sys.path:
    sys.path.append(_ROOT)
if os.path.join(_ROOT, "vendor") not in sys.path:
    sys.path.append(os.path.join(_ROOT, "vendor"))

from agency_swarm import Agency, Runner, set_tracing_disabled
from agency_swarm.context import MasterContext
set_tracing_disabled(True)   # no OpenAI tracing

from swarm.agents.market_analyst       import build_market_analyst
from swarm.agents.fundamentals_analyst import build_fundamentals_analyst
from swarm.agents.orchestrator         import build_orchestrator


def build_agency():
    market      = build_market_analyst()
    fundamentals = build_fundamentals_analyst()
    orchestrator = build_orchestrator(market, fundamentals)

    agency = Agency(
        orchestrator,                           # entry point (positional)
        communication_flows=[
            (orchestrator, market),
            (orchestrator, fundamentals),
        ],
        shared_instructions=(
            "This is the NSE Equity Research swarm. "
            "Mainboard India equities only — no SME/Emerge. "
            "Always include prices in ₹ and dates in DD-Mon-YYYY format."
        ),
    )
    return agency, orchestrator, market, fundamentals


async def run_query(agent, query: str, verbose: bool = True, agency=None) -> str:
    """Run a single query through an agent and return the final text."""
    if verbose:
        print(f"\n{'─'*60}")
        print(f"→ {agent.name}: {query[:80]}")
        print(f"{'─'*60}")

    # Build a MasterContext so send_message runtime tools are visible
    ctx = None
    if agency:
        ctx = MasterContext(
            agents=agency.agents,
            thread_manager=agency.thread_manager,
            user_context={},
            agent_runtime_state=agency._agent_runtime_state,
        )
    result = await Runner.run(agent, query, context=ctx)
    text = result.final_output or "(no output)"
    if verbose:
        print(text)
    return text


async def repl(agency, entry_agent, verbose: bool = True):  # noqa: C901
    """Interactive REPL — type queries, get research output."""
    agents = {
        "o": entry_agent,
        "m": None,
        "f": None,
    }
    # find sub-agents by name
    for a in agency.agents:
        if a.name == "MarketAnalyst":
            agents["m"] = a
        elif a.name == "FundamentalsAnalyst":
            agents["f"] = a

    current = entry_agent
    print("\n" + "═"*60)
    print("  NSE EQUITY RESEARCH SWARM")
    print("  3 agents: Orchestrator | MarketAnalyst | FundamentalsAnalyst")
    print("═"*60)
    print("Commands: /market  /fundamentals  /orchestrator  /quit")
    print()

    while True:
        try:
            line = input(f"[{current.name}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not line:
            continue

        if line.lower() in ("/quit", "/exit", "quit", "exit"):
            print("Bye.")
            break
        if line.lower() in ("/market", "/m"):
            current = agents["m"] or current
            print(f"→ Switched to {current.name}")
            continue
        if line.lower() in ("/fundamentals", "/f"):
            current = agents["f"] or current
            print(f"→ Switched to {current.name}")
            continue
        if line.lower() in ("/orchestrator", "/o"):
            current = entry_agent
            print(f"→ Switched to {current.name}")
            continue
        if line.lower() == "/help":
            print("  /market        — talk to MarketAnalyst (prices, momentum, options)")
            print("  /fundamentals  — talk to FundamentalsAnalyst (earnings, announcements)")
            print("  /orchestrator  — let Orchestrator route and synthesize")
            print("  /quit          — exit")
            continue

        try:
            await run_query(current, line, verbose=verbose, agency=agency)
        except Exception as e:
            print(f"\n⚠ Error: {e}")
            if verbose:
                traceback.print_exc()


def main():
    parser = argparse.ArgumentParser(description="NSE Equity Research Swarm")
    parser.add_argument("--query",   "-q", help="Single query to run")
    parser.add_argument("--agent",   "-a", choices=["orchestrator","market","fundamentals"],
                        default="orchestrator", help="Starting agent")
    parser.add_argument("--verbose", "-v", action="store_true", default=True)
    args = parser.parse_args()

    agency, orchestrator, market, fundamentals = build_agency()
    entry = {
        "orchestrator": orchestrator,
        "market":       market,
        "fundamentals": fundamentals,
    }[args.agent]

    if args.query:
        asyncio.run(run_query(entry, args.query, verbose=True, agency=agency))
    else:
        asyncio.run(repl(agency, entry, verbose=True))


if __name__ == "__main__":
    main()
