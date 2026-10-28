import os
import anthropic
from config import ANTHROPIC_API_KEY, MODEL, GENERATED_TOOLS_DIR
from memory.research_store import save_generated_tool

IMPROVEMENT_PROMPT = """You are an equity research tool developer.

After a research session, the analyst identified these gaps — data or analysis that would have improved the research but was unavailable:

GAPS:
{gaps}

EXISTING TOOLS:
{existing_tools}

Your task:
1. Pick the most valuable gap to address
2. Write a single Python function that fills it
3. The function must:
   - Use only: yfinance, pandas, numpy, requests, ta (technical analysis), beautifulsoup4
   - Return a dict (never raise uncaught exceptions — catch and return {"error": ...})
   - Have a clear docstring explaining what it does
   - Be named descriptively (e.g., get_insider_transactions, get_sector_etf_flow)

Respond in this EXACT format:
TOOL_NAME: <function_name>
DESCRIPTION: <one line description>
CODE:
```python
<complete function code here>
```
DONE
"""


def run_self_improvement(gaps: list, existing_tools: list) -> dict:
    """Given identified gaps, generate a new tool to fill the most valuable one."""
    if not gaps:
        return {"status": "no_gaps", "message": "No gaps to address"}

    if not ANTHROPIC_API_KEY:
        return {"status": "no_key", "message": "ANTHROPIC_API_KEY not set"}

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = IMPROVEMENT_PROMPT.format(
        gaps="\n".join(f"- {g}" for g in gaps),
        existing_tools="\n".join(f"- {t}" for t in existing_tools),
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text

        # Parse response
        tool_name = None
        description = None
        code = None

        for line in text.split("\n"):
            if line.startswith("TOOL_NAME:"):
                tool_name = line.split(":", 1)[1].strip()
            elif line.startswith("DESCRIPTION:"):
                description = line.split(":", 1)[1].strip()

        if "```python" in text and "```" in text:
            code_start = text.find("```python") + len("```python")
            code_end = text.rfind("```")
            code = text[code_start:code_end].strip()

        if not tool_name or not code:
            return {"status": "parse_error", "raw": text[:500]}

        # Save to DB and to file
        save_generated_tool(tool_name, description or "", code, reason=str(gaps[:2]))

        fpath = os.path.join(GENERATED_TOOLS_DIR, f"{tool_name}.py")
        with open(fpath, "w") as f:
            f.write(f'"""{description or tool_name}"""\n')
            f.write("import yfinance as yf\nimport pandas as pd\nimport numpy as np\n\n")
            f.write(code)

        return {
            "status": "success",
            "tool_name": tool_name,
            "description": description,
            "file": fpath,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
