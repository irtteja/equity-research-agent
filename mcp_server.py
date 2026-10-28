#!/usr/bin/env python3
"""
Equity Research MCP Server — exposes all research tools to Claude directly.
Add to Claude Code config and use in any session without a separate API key.
"""
import json
import sys
import os

# Make nselib importable
_NSELIB_PATH = os.path.expanduser("~/irtteja/nselib")
if _NSELIB_PATH not in sys.path:
    sys.path.insert(0, _NSELIB_PATH)

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
import mcp.types as types

from tools import CORE_TOOLS

app = Server("equity-research")

# Build tool registry from CORE_TOOLS
import inspect

def _build_schema(func) -> dict:
    sig = inspect.signature(func)
    props = {}
    required = []
    for name, param in sig.parameters.items():
        ann = param.annotation
        if ann in (int, float) or ann == inspect.Parameter.empty and name in ("days", "years"):
            ptype = "number"
        elif ann == bool:
            ptype = "boolean"
        elif ann == list:
            ptype = "array"
        else:
            ptype = "string"
        desc = name.replace("_", " ")
        if param.default != inspect.Parameter.empty:
            desc += f" (default: {param.default!r})"
        props[name] = {"type": ptype, "description": desc}
        if param.default == inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "object",
        "properties": props,
        **({"required": required} if required else {}),
    }

_REGISTRY = {fn.__name__: fn for fn in CORE_TOOLS}


@app.list_tools()
async def list_tools() -> list[Tool]:
    tools = []
    for fn in CORE_TOOLS:
        tools.append(Tool(
            name=fn.__name__,
            description=fn.__doc__ or fn.__name__,
            inputSchema=_build_schema(fn),
        ))
    return tools


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name not in _REGISTRY:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    try:
        result = _REGISTRY[name](**arguments)
        return [TextContent(type="text", text=json.dumps(result, default=str, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e), "tool": name}))]


def main():
    import asyncio
    asyncio.run(stdio_server(app))


if __name__ == "__main__":
    main()
