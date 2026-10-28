import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = os.getenv("RESEARCH_MODEL", "claude-opus-4-5")
MAX_TOKENS = 8192

DATA_CACHE_DIR = os.path.join(os.path.dirname(__file__), "data", "cache")
GENERATED_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "generated_tools")
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
MEMORY_DB = os.path.join(os.path.dirname(__file__), "memory", "research.db")
