import os
import json
import anthropic
from rich.console import Console
from rich.markdown import Markdown
from config import ANTHROPIC_API_KEY, MODEL, MAX_TOKENS
from agent.tools_registry import ToolsRegistry
from agent.self_improver import run_self_improvement
from memory.research_store import (
    init_db, save_research, get_past_research,
    save_insight, get_insights
)
from memory.task_log import init_task_tables, log_task, update_task_status, log_checkpoint, get_incomplete_tasks

console = Console()

SYSTEM_PROMPT = """You are an expert equity research analyst with access to financial data tools covering both Indian and global markets.

Tools available:
- NSE India: nse_equity_quote, nse_price_history, nse_option_chain, nse_corporate_actions, nse_financial_results, nse_shareholding, nse_index_data, nse_lookup
- BSE India: bse_equity_quote, bse_price_history, bse_search_scrip
- Global (yfinance): get_price_history, get_current_quote, get_financial_statements, get_key_ratios, run_technical_analysis, get_news_sentiment, run_dcf_valuation, get_peer_comparison

For Indian stocks: prefer NSE tools first (use symbol like RELIANCE, HDFCBANK, TCS). Use BSE tools for scrip code lookups or cross-verification.
For global stocks: use yfinance tools (ticker like AAPL, MSFT, or RELIANCE.NS for NSE-listed on Yahoo).

For each research request:
1. Use available tools to gather price data, fundamentals, technicals, news, corporate actions, and valuation
2. For Indian stocks: pull NSE quote + price history + financial results + shareholding + corporate actions
3. Synthesize findings into a comprehensive research report
4. Identify gaps — what additional data or analysis would have improved your research
5. End your report with a section: ## Research Gaps\n(list what you wish you had)

Always be:
- Specific with numbers and dates
- Clear about what you know vs. what you're inferring
- Honest about uncertainty and data limitations

Past research context will be provided when available.
"""


def _extract_gaps(report: str) -> list:
    """Extract gap bullets from the Research Gaps section of a report."""
    gaps = []
    if "## Research Gaps" not in report:
        return gaps
    section = report.split("## Research Gaps")[1]
    for line in section.split("\n"):
        line = line.strip()
        if line.startswith("-") or line.startswith("*"):
            gaps.append(line.lstrip("-* ").strip())
        if len(gaps) >= 5:
            break
    return gaps


class ResearchOrchestrator:
    def __init__(self):
        if not ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is not set. Add it to .env or export it.")
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.registry = ToolsRegistry()
        init_db()
        init_task_tables()
        self._load_mission()

    def _load_mission(self):
        mission_path = os.path.join(os.path.dirname(__file__), "..", "MISSION.md")
        try:
            with open(mission_path) as f:
                self.mission = f.read()
        except Exception:
            self.mission = ""

    def resume(self):
        """Print incomplete tasks from last session so work isn't lost."""
        tasks = get_incomplete_tasks()
        if tasks:
            console.print("\n[bold yellow]Resuming incomplete tasks:[/bold yellow]")
            for t in tasks:
                console.print(f"  [{t['status']}] {t['task_id']}: {t['title']}")

    def research(self, query: str, ticker: str = None, improve: bool = True) -> str:
        """Run a full research session on a stock."""
        console.print(f"\n[bold cyan]Starting research:[/bold cyan] {query}")

        # Build context from past research
        context = ""
        if ticker:
            past = get_past_research(ticker)
            insights = get_insights(ticker)
            if past:
                context += f"\n\nPast research on {ticker}:\n"
                for p in past:
                    context += f"- [{p['created_at'][:10]}] {p['query'][:100]}\n"
            if insights:
                context += f"\nKnown insights on {ticker}:\n"
                for ins in insights:
                    context += f"- {ins['insight']}\n"

        mission_block = f"\n\n[MISSION]\n{self.mission}" if self.mission else ""

        messages = [
            {
                "role": "user",
                "content": query + (f"\n\n[Context]{context}" if context else "") + mission_block
            }
        ]

        tools = self.registry.get_anthropic_tools()
        tools_used = []
        report = ""

        # Agentic loop
        while True:
            console.print("[dim]Thinking...[/dim]")
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )

            # Collect text
            text_blocks = [b.text for b in response.content if hasattr(b, "text")]
            if text_blocks:
                report = "\n".join(text_blocks)

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                tool_calls = [b for b in response.content if b.type == "tool_use"]
                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for tc in tool_calls:
                    name = tc.name
                    inputs = tc.input
                    console.print(f"[yellow]  -> calling tool:[/yellow] {name}({list(inputs.keys())})")
                    tools_used.append(name)
                    result = self.registry.call(name, inputs)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": json.dumps(result, default=str)[:8000],
                    })

                messages.append({"role": "user", "content": tool_results})
            else:
                break

        if report:
            console.print("\n")
            console.print(Markdown(report))

            # Save research
            gaps = _extract_gaps(report)
            if ticker:
                save_research(ticker, query, report, tools_used, gaps)

            # Self-improvement: generate a new tool to fill the top gap
            if improve and gaps:
                console.print(f"\n[bold magenta]Self-improvement:[/bold magenta] Found {len(gaps)} gaps. Generating new tool...")
                result = run_self_improvement(gaps, self.registry.list_tools())
                if result["status"] == "success":
                    console.print(f"[green]New tool created:[/green] {result['tool_name']} — {result['description']}")
                    # Hot-reload the new tool
                    try:
                        import importlib.util
                        spec = importlib.util.spec_from_file_location(result["tool_name"], result["file"])
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        fn = getattr(mod, result["tool_name"], None)
                        if fn:
                            self.registry.register(result["tool_name"], fn)
                            console.print(f"[green]Tool hot-loaded into registry.[/green]")
                    except Exception as e:
                        console.print(f"[red]Hot-load failed:[/red] {e}")
                else:
                    console.print(f"[dim]Self-improvement: {result}[/dim]")

        return report
