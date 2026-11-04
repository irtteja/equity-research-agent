"""
nselib MCP server — auto-exposes all NSE data methods as MCP tools.

New methods added to nselib are automatically available here — no manual
wiring required. The server introspects the NSE class at startup and
registers every public method, converting date parameters to strings
so the JSON schema stays clean.

Run on a residential IP (your laptop) to access all endpoints including
quote, option chain and futures (cloud-blocked from datacenter IPs):

    python -m nselib.mcp_server
    # or after pip install:
    nselib-mcp

Add to Claude Code:
    claude mcp add nselib -- python -m nselib.mcp_server
"""

import inspect
import json
import types
from datetime import date
from inspect import Parameter, Signature
from typing import Any, Union, get_args, get_origin, get_type_hints

try:
    from mcp.server.fastmcp import FastMCP as _MCP   # mcp < 2
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as _MCP  # mcp >= 2

from .nse import NSE

mcp = _MCP(
    "nselib",
    instructions=(
        "NSE India data server. Provides live quotes, indices, option chains, "
        "historical data, corporate events, F&O analytics, and bhavcopy downloads."
    ),
)

_nse = NSE()

# Methods that require Brain/Claude or accept raw data pipelines as input
_SKIP = {
    "ask", "analyze", "market_summary", "screen",  # need Claude brain
    "gainers", "losers",                            # take raw data list — wrapped below
    "guardian", "close",
}


# ── Helpers ───────────────────────────────────────────────────────────────

def _json(obj: Any) -> str:
    def _default(o):
        if isinstance(o, date):
            return o.isoformat()
        return str(o)
    return json.dumps(obj, default=_default, ensure_ascii=False)


def _union_args(ann) -> tuple:
    """Return the type args of any Union form (typing.Union or X | Y)."""
    origin = get_origin(ann)
    if origin is Union:
        return get_args(ann)
    # Python 3.10+ `X | Y` produces types.UnionType
    if isinstance(ann, types.UnionType):
        return ann.__args__
    return ()


def _is_date_ann(ann) -> bool:
    if ann is date:
        return True
    args = _union_args(ann)
    return bool(args) and date in args


def _str_ann(ann):
    """Replace date with str, preserving Optional."""
    if ann is date:
        return str
    args = _union_args(ann)
    if args and date in args:
        return str | None if type(None) in args else str
    return ann


def _convert(val: Any, ann: Any) -> Any:
    """Convert a string value to date when the original annotation requires it."""
    if val is None or not _is_date_ann(ann):
        return val
    try:
        return date.fromisoformat(str(val))
    except (ValueError, TypeError):
        return val


def _make_tool(method: Any, name: str):
    """
    Build a FastMCP-compatible wrapper for an NSE method.
    - Replaces date params with str so the JSON schema stays clean.
    - Returns None for methods whose first param takes a raw data structure
      (those aren't useful as standalone MCP tools).
    """
    try:
        hints = get_type_hints(method)
    except Exception:
        hints = {}

    sig    = inspect.signature(method)
    params = [p for p in sig.parameters.values() if p.name != "self"]

    # Skip pipeline methods whose first param is a raw list/dict
    if params:
        first_ann = hints.get(params[0].name)
        if get_origin(first_ann) in (list, dict) or first_ann in (list, dict):
            return None

    new_params: list[Parameter] = []
    conv_map:   dict[str, Any]  = {}
    for p in params:
        ann   = hints.get(p.name, Parameter.empty)
        new_p = p.replace(
            annotation=_str_ann(ann) if ann is not Parameter.empty else Parameter.empty
        )
        new_params.append(new_p)
        conv_map[p.name] = ann

    new_sig = Signature(new_params, return_annotation=str)
    doc     = inspect.getdoc(method) or f"NSE {name}"

    def wrapper(**kwargs: Any) -> str:
        converted = {k: _convert(v, conv_map.get(k)) for k, v in kwargs.items()}
        return _json(method(**converted))

    wrapper.__name__      = name
    wrapper.__qualname__  = name
    wrapper.__doc__       = doc
    wrapper.__signature__ = new_sig
    wrapper.__annotations__ = {
        p.name: p.annotation
        for p in new_params
        if p.annotation is not Parameter.empty
    }
    wrapper.__annotations__["return"] = str
    return wrapper


# ── Auto-register all public NSE methods ─────────────────────────────────

_registered: list[str] = []

for _name in sorted(dir(_nse)):
    if _name.startswith("_") or _name in _SKIP:
        continue
    _method = getattr(_nse, _name, None)
    if not callable(_method):
        continue
    _wrapper = _make_tool(_method, _name)
    if _wrapper is None:
        continue
    mcp.tool()(_wrapper)
    _registered.append(_name)


# ── Special wrappers for methods that need pre-fetched data ──────────────

@mcp.tool()
def top_gainers(index: str = "NIFTY 50", count: int = 10) -> str:
    """Top N gainers in an index sorted by % change."""
    return _json(_nse.gainers(_nse.list_equity_stocks_by_index(index), count))


@mcp.tool()
def top_losers(index: str = "NIFTY 50", count: int = 10) -> str:
    """Top N losers in an index sorted by % loss."""
    return _json(_nse.losers(_nse.list_equity_stocks_by_index(index), count))


# ── Entry point ───────────────────────────────────────────────────────────

def main() -> None:
    import sys
    print(f"[nselib-mcp] {len(_registered) + 2} tools registered: {_registered + ['top_gainers', 'top_losers']}", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
