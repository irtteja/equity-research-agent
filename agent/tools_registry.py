import sys
import os
import importlib.util
import inspect
from tools import CORE_TOOLS
from memory.research_store import get_all_generated_tools, increment_tool_usage
from config import GENERATED_TOOLS_DIR


def _func_to_anthropic_tool(func) -> dict:
    """Convert a Python function to an Anthropic tool definition via its signature and docstring."""
    sig = inspect.signature(func)
    params = {}
    required = []
    for name, param in sig.parameters.items():
        annotation = param.annotation
        if annotation == inspect.Parameter.empty:
            ptype = "string"
        elif annotation == int or annotation == float:
            ptype = "number"
        elif annotation == bool:
            ptype = "boolean"
        elif annotation == list:
            ptype = "array"
        else:
            ptype = "string"

        params[name] = {"type": ptype, "description": name}
        if param.default == inspect.Parameter.empty:
            required.append(name)

    return {
        "name": func.__name__,
        "description": func.__doc__ or func.__name__,
        "input_schema": {
            "type": "object",
            "properties": params,
            "required": required,
        },
    }


class ToolsRegistry:
    def __init__(self):
        self._tools: dict = {}
        self._load_core_tools()
        self._load_generated_tools()

    def _load_core_tools(self):
        for func in CORE_TOOLS:
            self._tools[func.__name__] = func

    def _load_generated_tools(self):
        """Load agent-generated tools from DB and from generated_tools/ directory."""
        # Load from DB
        for tool_record in get_all_generated_tools():
            name = tool_record["name"]
            code = tool_record["code"]
            try:
                namespace = {}
                exec(code, namespace)
                if name in namespace and callable(namespace[name]):
                    self._tools[name] = namespace[name]
            except Exception as e:
                print(f"[registry] Failed to load generated tool '{name}': {e}")

        # Also load .py files from generated_tools/ dir
        if os.path.isdir(GENERATED_TOOLS_DIR):
            for fname in os.listdir(GENERATED_TOOLS_DIR):
                if fname.endswith(".py") and not fname.startswith("_"):
                    fpath = os.path.join(GENERATED_TOOLS_DIR, fname)
                    try:
                        spec = importlib.util.spec_from_file_location(fname[:-3], fpath)
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        for attr in dir(mod):
                            obj = getattr(mod, attr)
                            if callable(obj) and not attr.startswith("_"):
                                self._tools[attr] = obj
                    except Exception as e:
                        print(f"[registry] Failed to load {fpath}: {e}")

    def get_anthropic_tools(self) -> list:
        return [_func_to_anthropic_tool(f) for f in self._tools.values()]

    def call(self, name: str, inputs: dict):
        if name not in self._tools:
            return {"error": f"Tool '{name}' not found in registry"}
        try:
            increment_tool_usage(name)
            return self._tools[name](**inputs)
        except Exception as e:
            return {"error": f"Tool '{name}' raised: {str(e)}"}

    def register(self, name: str, func):
        self._tools[name] = func

    def list_tools(self) -> list:
        return list(self._tools.keys())
