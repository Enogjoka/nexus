"""
NEXUS configuration.

Hard architectural rule: config knobs live here as plain constants; secrets
live in the environment only (never as literals, never in git, never in the
database). See core/database.py for the corresponding rule that the database
never stores configuration or feature flags.
"""
import os
from enum import Enum
from functools import lru_cache


class Stage(str, Enum):
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    MICRO = "MICRO"
    SCALED = "SCALED"


# STAGE is read from the environment exactly once, at import time. There is
# no setter, no runtime mutation path, and no way to change it without a
# process restart (or, in tests, an explicit importlib.reload of this
# module). This is invariant #1 in CLAUDE.md — do not add one.
_STAGE = Stage(os.environ.get("NEXUS_STAGE", Stage.PAPER.value))


@lru_cache(maxsize=1)
def get_stage() -> Stage:
    """Return the trading stage. Fixed at process start; no setter exists."""
    return _STAGE


# ACCOUNT
ACCOUNT_SIZE = 5000
MAX_RISK_PCT = 1.0

# MODELS
CLAUDE_MODEL = "claude-fable-5"
TRIAGE_MODEL = "claude-sonnet-4-6"
OPENAI_MODEL = "gpt-4.1"
GEMINI_MODEL = "gemini-2.5-flash"

# THRESHOLDS
MIN_CONFIDENCE = 55
MAX_DAILY_COST = 10.0

# SYMBOLS
YF_SYMBOL = "GC=F"
DXY_SYMBOL = "DX-Y.NYB"

KILL_FILE_PATH = "./KILL"

# SECRETS — always os.environ.get, never literals. Nothing here is ever
# written back to the environment, to the database, or to git.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FRED_API_KEY = os.environ.get("FRED_API_KEY")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_IDS = os.environ.get("TELEGRAM_CHAT_IDS")
DATABASE_URL = os.environ.get("DATABASE_URL")
HMAC_SECRET = os.environ.get("HMAC_SECRET")
