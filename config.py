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

# SIZING (risk/sizing.py)
CONTRACT_SIZE_OZ = 100  # 1.0 lot XAUUSD = 100 oz
LOT_STEP = 0.01
MIN_LOT = 0.01
MAX_LOT_HARD_CAP = 1.0  # absolute ceiling regardless of equity

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

# DATA AGENT
DATA_POLL_SECONDS = 300
CANDLE_LOOKBACK = {"1h": 500, "4h": 500, "1d": 400}
TIMEFRAMES = ["1h", "4h", "1d"]
ANOMALY_MAX_PCT_JUMP = {"1h": 3.0, "4h": 5.0, "1d": 8.0}

# PRICE RESOLVER (XAUUSD anchor-enum contract)
PIP_SIZE = 0.1  # XAUUSD: 1 pip = $0.10
MAX_ENTRY_DRIFT_PCT = 0.5
MIN_STOP_DISTANCE_PIPS = 15.0

# VALIDATOR (pre-flight gate — risk/validator.py)
# RSI extremes are dual-timeframe and direction-aware: BOTH h1 and h4 must be
# past their threshold before the rule fires (see risk/validator.py RULE 2).
RSI_EXTREME_LONG = {"h1": 78, "h4": 70}
RSI_EXTREME_SHORT = {"h1": 22, "h4": 30}
EVENT_BLOCK_MINUTES = 30
FIX_BLOCK_MINUTES = 20
LONDON_FIX_UTC = ["10:30", "15:00"]
RR_FLOOR_TP1 = 1.2
RR_WARN_TP2 = 2.0
CONFIDENCE_FLOOR = 40
MACRO_DIVERGENCE_PENALTY = 8
VOLATILE_PENALTY = 5

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
