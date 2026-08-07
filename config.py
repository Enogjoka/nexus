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

# ANALYSIS (ai/analysis.py — the signal spine)
ANALYSIS_MODEL = "claude-fable-5"
ANALYSIS_MAX_TOKENS = 1500
ANALYSIS_TIMEOUT_SECONDS = 60
ANALYSIS_MIN_INTERVAL_MINUTES = 30
# Exactly the anchors that build_anchor_map() can currently resolve. As of
# Task 10 that includes SESSION_HIGH/SESSION_LOW: data/gold_agent.py now tracks
# session high/low into AppState, so the resolver can finally fill them (they
# were withheld through Tasks 5-9 precisely because it could not).
OFFERED_ANCHORS = [
    "CURRENT_BID", "CURRENT_ASK", "CURRENT_MID",
    "H1_EMA20", "H1_EMA50", "H1_BB_UPPER", "H1_BB_LOWER", "H1_BB_MID",
    "H4_EMA20", "H4_EMA50", "H4_SWING_HIGH", "H4_SWING_LOW",
    "D1_EMA20", "D1_EMA50", "D1_SWING_HIGH", "D1_SWING_LOW",
    "SESSION_HIGH", "SESSION_LOW",
]
# Estimated USD cost per 1M tokens, used ONLY to accumulate the daily budget
# guard (STATE.budget_spent_today) — approximate, not a billing source of truth.
ANALYSIS_COST_PER_MTOK_INPUT = 3.0
ANALYSIS_COST_PER_MTOK_OUTPUT = 15.0

# PAPER ENGINE (exec_/paper_engine.py)
PAPER_TTL_HOURS = 24  # a PENDING signal older than this expires unfilled

# TELEGRAM (ops/telegram_bot.py) — the bot talks to the raw Telegram HTTP API
# via requests. The token and chat allowlist are SECRETS and live in the
# environment only (see below); nothing here is a credential.
TELEGRAM_POLL_SECONDS = 30
TELEGRAM_TIMEOUT_SECONDS = 10
TELEGRAM_API_BASE = "https://api.telegram.org"

# MACRO (sensors/fred.py) — real yields, breakevens, and the 2s10s curve.
# Numbers only: no interpretation of what a reading MEANS lives here or in
# the sensor — that belongs to the analyst prompt (a later task).
FRED_SERIES = ["DFII10", "DGS10", "DGS2", "T10YIE", "M2SL", "WALCL"]
FRED_POLL_MINUTES = 60
FRED_LOOKBACK_DAYS = 400
STALE_DATA_ALERT_HOURS = 26  # dead-man's-switch threshold for the macro sensor

# POSITIONING (sensors/positioning.py) — who is actually long gold.
# Numbers only: no interpretation lives here or in the sensor.
# Disaggregated Futures Only -- carries the Managed Money taxonomy
# (m_money_positions_*). The old 6dca-aqww (Legacy Futures Only) does not
# have Managed Money fields at all and stays gone -- see task 8 fix pass.
COT_SOCRATA_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
COT_COMMODITY_FILTER = "GOLD"
# commodity_name='GOLD' matches multiple contracts on the disaggregated
# dataset (e.g. standard 100oz COMEX gold AND e-micro gold), each reporting
# its own open_interest under the same report_date -- pinning to a single
# market_and_exchange_names value is what makes cot_reports one coherent
# time series instead of a blend of different contracts. See task 8 fix 2.
COT_MARKET_NAME = "GOLD - COMMODITY EXCHANGE INC."
POSITIONING_POLL_HOURS = 6
COT_PCTILE_LOOKBACK_WEEKS = 156  # 3 years for percentile rank

# CALENDAR (sensors/calendar_agent.py) — arms validator RULE 1's event blackout.
# The feed supplies USD events; the validator itself applies the 30-min block
# window (EVENT_BLOCK_MINUTES) inside the wider lookahead we hand it.
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_CURRENCIES = ["USD"]
CALENDAR_POLL_HOURS = 4
EVENT_LOOKAHEAD_MINUTES = 120  # window upcoming_events() returns to the validator

# NEWS (sensors/news.py) — RSS pipeline + Stage-1 keyword prefilter. Stage-2 AI
# scoring is deferred to the fusion task (relevance/direction stay NULL here).
NEWS_FEEDS = {
    # Kitco: URL moved, restore when confirmed (kitco.com/rss/KitcoNews.xml and
    # trivial variants all 404 with no redirect; a dead feed is noise, not coverage).
    "Mining.com": "https://www.mining.com/feed/",
    "GoogleNews_Gold": "https://news.google.com/rss/search?q=gold+OR+XAUUSD&hl=en-US&gl=US&ceid=US:en",
    "Fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "BLS": "https://www.bls.gov/feed/news_release/bls_all.rss",
    "MarketWatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
}
# Sent as the User-Agent on every feed GET. Some sites (e.g. BLS) block all
# non-browser UAs regardless -- their 403 is then the honest logged artifact.
NEWS_USER_AGENT = "NEXUS/1.0 (+private research)"
NEWS_POLL_MINUTES = 15
NEWS_FETCH_TIMEOUT_SECONDS = 15  # requests per-call timeout for each feed GET
NEWS_HEAT_LOOKBACK_HOURS = 6
# Stage-1 keyword prefilter (case-insensitive substring match). An article
# passes if: >=1 GOLD_DIRECT hit, OR >=2 distinct GOLD_MACRO hits, OR >=2
# distinct GOLD_GEOPOLITICAL hits. Includes the v4 seed words.
GOLD_DIRECT = [
    "gold", "xauusd", "xau/usd", "xau", "bullion", "comex", "lbma", "gld",
    "comex gold", "spdr gold", "gold price", "gold futures",
]
GOLD_MACRO = [
    "fed", "fomc", "federal reserve", "powell", "interest rate", "rate cut",
    "rate hike", "inflation", "cpi", "pce", "treasury", "yield", "yields",
    "real yield", "dollar", "dxy", "monetary policy", "nonfarm", "payrolls",
    "jobs report", "unemployment", "gdp", "recession", "hawkish", "dovish",
]
GOLD_GEOPOLITICAL = [
    "war", "conflict", "sanctions", "geopolitical", "tension", "tensions",
    "military", "invasion", "crisis", "attack", "escalation", "safe haven",
    "safe-haven", "central bank buying", "de-dollarization", "ceasefire",
]

# RAG (fusion/rag.py) — recall similar past world-states, outcome-weighted.
# Every numeric dim is min-max normalized into [0,1] against a FIXED range, so
# a vector embedded today stays comparable to one embedded a year ago. Ranges
# are deliberately generous: values outside them clamp rather than distort.
RAG_DIM_RANGES = {
    "rsi_h1": (0.0, 100.0),
    "rsi_h4": (0.0, 100.0),
    "rsi_d1": (0.0, 100.0),
    "bb_position_h1": (0.0, 100.0),
    "atr_h1": (0.0, 50.0),
    "atr_h4": (0.0, 100.0),
    "real_yield": (-2.0, 4.0),
    "real_yield_5d_delta": (-1.0, 1.0),
    "curve_2s10s": (-2.0, 3.0),
    "breakeven_10y": (0.0, 5.0),
    "dxy": (90.0, 120.0),
    "cot_mm_net_pctile": (0.0, 100.0),
    "comex_coverage": (0.0, 50.0),
    "news_heat": (0.0, 1.0),
    "minutes_to_next_high_event": (0.0, 120.0),
    "fix_window": (0.0, 1.0),
}
# One-hot dims. An all-zero block encodes "absent" unambiguously, so these need
# no separate presence mask (unlike the numerics, where a neutral 0.5 would be
# indistinguishable from a genuine mid-range reading).
RAG_CATEGORICAL_VALUES = {
    "regime_h4": ["TREND_UP", "TREND_DOWN", "RANGE", "VOLATILE"],
    "session": ["ASIA", "LONDON", "OVERLAP", "NY", "OFF"],
}
RAG_K = 5
RAG_MIN_SAMPLES = 3          # fewer comparable precedents than this -> no recall at all
# Cosine floor for "comparable". Because every dim is non-negative (values in
# [0,1] plus presence masks), similarity is compressed into roughly [0.40, 1.00]
# rather than spanning [0,1]. Measured landmarks on this embedding:
#     1.000  identical states
#    ~0.880  a merely DIFFERENT state (e.g. mid-range vs one extreme)
#     0.647  maximally-opposed complete states
#     0.402  a complete state vs an all-missing one
# 0.92 therefore sits above "merely different" and admits only genuinely close
# precedents. PROVISIONAL: this is a geometric argument, not an empirical one —
# re-tune it against real outcome data once enough resolved signals exist for
# recall quality to be measured rather than reasoned about.
RAG_MIN_SIM = 0.92
RAG_LINK_WINDOW_MINUTES = 90  # how far back a signal may reach for its state vector
RAG_WILSON_Z = 1.96          # 95% confidence

# REGIME (fusion/regime.py) — an HMM over the MACRO state, not price.
REGIME_N_STATES = 4
REGIME_MIN_ROWS = 168        # 1 week of hourly rows; below this we refuse to train
REGIME_RETRAIN_HOURS = 24
REGIME_FEATURES = ["real_yield_5d_delta", "dxy_change", "curve_2s10s", "news_heat"]
REGIME_LABELS = ["YIELDS_FALLING", "NEUTRAL", "YIELDS_RISING", "STRESS"]
# Mapping is CONFIG, not code: risk/validator.py RULE 5 owns the vocabulary it
# reacts to ("RISK_OFF") and is UNTOUCHABLE. Regime labels are this module's own
# vocabulary; this dict is the only place the two are bridged. A None means the
# validator sees no macro_regime and RULE 5 skips.
REGIME_TO_VALIDATOR = {
    "STRESS": "RISK_OFF",
    "YIELDS_FALLING": None,
    "NEUTRAL": None,
    "YIELDS_RISING": None,
}

# LEARNING LOOP (fusion/learning_loop.py) — the system studying itself.
LEARN_MIN_SAMPLES = 10        # terminal signals required before ranking any dim
LEARN_NIGHTLY_UTC_HOUR = 2    # nightly job runs at 02:00 UTC
LEARN_WEEKLY_DAY = 6          # 6 = Sunday (datetime.weekday(): Mon=0)
LEARN_REPORT_WINDOW_DAYS = 7  # lookback for the weekly report
REPORTS_DIR = "./reports"

# BACKEND SUPERVISOR (backend.py) — the process that runs every agent loop.
SUPERVISOR_POLL_SECONDS = 30   # thread-liveness poll interval
MAX_RESTARTS_PER_HOUR = 4      # per agent; on the next failure the supervisor gives up
HEARTBEAT_TELEGRAM_HOURS = 12  # /status-style summary cadence; 0 disables

# STAGE GOVERNOR (risk/stage.py) — the ladder as enforceable machinery.
# The Stage enum and get_stage() at the top of this file are the mechanism;
# these are the knobs the governor reads. Nothing here mutates a stage.
#
# A demotion is signalled by the presence of this FILE, not by a database row
# or an env var, because a file survives a crash, is trivially auditable, and
# can only be cleared by a human deleting it and restarting the process.
DEMOTED_FLAG_PATH = "./DEMOTED"

# Per-stage execution policy. The ladder tightens as real money appears:
# PAPER/SHADOW are simulated so the caps are nominal; MICRO is the first stage
# with live money and therefore the tightest caps in the table; SCALED is the
# only stage permitted to size off the risk budget.
# max_concurrent_positions stays 2 at every stage — correlation risk on a
# single instrument does not care which stage you are in.
STAGE_POLICY = {
    "PAPER": {
        "fill_mode": "MODELED",
        "max_lot_per_order": 0.10,
        "max_total_lots": 0.10,
        "max_concurrent_positions": 2,
    },
    "SHADOW": {
        "fill_mode": "SHADOW_REAL_BIDASK",
        "max_lot_per_order": 0.10,
        "max_total_lots": 0.10,
        "max_concurrent_positions": 2,
    },
    "MICRO": {
        "fill_mode": "LIVE_FIXED_MICRO",
        "max_lot_per_order": 0.01,
        "max_total_lots": 0.02,
        "max_concurrent_positions": 2,
    },
    "SCALED": {
        "fill_mode": "LIVE_RISK_SIZED",
        "max_lot_per_order": 1.00,
        "max_total_lots": 1.00,
        "max_concurrent_positions": 2,
    },
}

# KERNEL (risk/kernel.py) — Ring 0, the sovereign risk kernel.
# These are hard limits, not preferences. Every one of them only ever DENIES;
# no value here can cause an order to be permitted that would otherwise be
# refused. Loosening a number here loosens the last line of defence — the
# kernel is the ring that assumes everything upstream of it is already wrong.
DAILY_LOSS_CAP_PCT = 1.5      # % of day-start equity; breach halts until UTC midnight
MAX_DRAWDOWN_PCT = 5.0        # % below observed peak equity; breach demotes a rung
SPREAD_CEILING_USD = 0.45     # XAUUSD spread above which no entry is worth taking
SLIPPAGE_ANOMALY_MULT = 2.0   # observed slippage over expected before it is anomalous
STALE_TICK_SECONDS = 30       # a quote older than this is not a price, it is a memory
RECONCILE_INTERVAL_SECONDS = 30
KERNEL_EQUITY_FLOOR = 100.0   # below this something is deeply wrong

# DOCTRINE (ai/doctrine.py) — Ring 2's output cage.
# The head-of-desk emits a Doctrine (enums + bounded floats), never an order.
# Every failure path degrades to FLAT: silence is safe, guessing is not.
POD_NAMES = ["S1_FIXFADE", "S2_VWAPSNAP", "S3_BASIS", "S4_NEWSBURST"]
DOCTRINE_MODEL = "claude-fable-5"
DOCTRINE_MAX_TOKENS = 1000
DOCTRINE_TIMEOUT_SECONDS = 60
# Cadence follows the clock: pods only matter when the book is active.
DOCTRINE_CADENCE_ACTIVE_MIN = 15   # LONDON / OVERLAP / NY
DOCTRINE_CADENCE_QUIET_MIN = 60    # ASIA / OFF
# Sonnet scores 0-1 whether a state change is worth waking Fable for.
TRIAGE_THRESHOLD = 0.5
TRIAGE_MAX_TOKENS = 200

# BRIDGE (data/mt5_bridge.py) — the broker seam.
# MetaTrader5's Python package is Windows-only, so the broker sits behind a
# protocol with two implementations: SimBridge runs everywhere, RealMT5Bridge
# activates only where the package imports. Nothing above the seam knows which
# one it is talking to.
SIM_SPREAD_USD = 0.35     # inside the 0.45 kernel ceiling, so SIM can trade
SIM_SLIPPAGE_USD = 0.05   # modeled adverse slippage per side; never favourable
# Read at boot and never mutated at runtime, the same discipline as STAGE.
# There is no setter and no code path that writes this back.
BRIDGE_KIND = "SIM"       # "SIM" | "MT5"

# LINK (link/channel.py, link/messages.py) — the nervous system between the
# brain box and the exec box. Today both ends loopback on one machine; later
# the brain is an Ubuntu VPS and exec is a Windows box beside the broker.
# HMAC_SECRET (below, from the environment) signs every frame; without it the
# link refuses to start.
LINK_HOST = "127.0.0.1"
LINK_PORT = 8765
LINK_HEARTBEAT_SECONDS = 10
LINK_STALE_SECONDS = 35              # 3 missed heartbeats + slack
LINK_MAX_CLOCK_SKEW_SECONDS = 30

# PODS (pods/base.py) — the scalp-pod framework and the cost gate.
#
# COST_MULT is the single number separating micro-scalping from donating the
# account to the broker in 30-cent increments. A scalp must expect to earn a
# MULTIPLE of what it costs to put on, not merely to beat it: at 1.0x a
# strategy that is right slightly more often than not still bleeds, because the
# cost is certain and the edge is a forecast. The floor of 2.0 is enforced in
# code (see pods/base.py) and is not a preference — lowering it is how a
# profitable-looking scalper turns into a fee pump.
COST_MULT = 2.0
COMMISSION_USD_PER_LOT = 7.0      # EBC round-turn per 1.0 lot; scaled by lots
SLIPPAGE_P75_FALLBACK_USD = 0.10  # used until the fills table has >= 20 rows

# Per-pod circuit breakers. These bound how much one misbehaving strategy can
# cost before a human looks at it.
POD_MAX_CONSECUTIVE_LOSSES = 3    # then disabled until the next doctrine re-enables it
POD_DAILY_LOSS_CAP_USD = 50.0     # then disabled until UTC midnight
POD_MAX_TRADES_PER_DAY = 10       # then disabled until UTC midnight

# PODS.S1 — FIX-FADE. The 15:00 UTC PM gold fix drags price away from the
# session mean; the pod fades the stretch back toward VWAP. Armed only around
# the fix, because outside that window the same stretch means something else.
S1_STRETCH_ATR_MULT = 1.2   # stretch vs session VWAP, measured in ATR(h1)
S1_WINDOW_BEFORE_MIN = 45   # arm window before 15:00 UTC
S1_WINDOW_AFTER_MIN = 30    # and after
S1_STOP_ATR_MULT = 0.8
S1_TP_ATR_MULT = 1.0
S1_LOTS = 0.01

# PODS.S2 — VWAP-SNAP. A range-bound session that displaces far from VWAP on
# FALLING volume is a move without participation; the pod trades the snap back.
S2_SIGMA_MULT = 2.0          # displacement vs session VWAP, in session stdev
S2_STOP_ATR_MULT = 0.7
S2_TP_VWAP_FRACTION = 0.8    # target 80% of the way back to VWAP, never past it
S2_LOTS = 0.01

# A pod's stated edge is a FORECAST and the cost gate treats it as a claim, so
# it is discounted before being claimed. Half is deliberately blunt: the point
# is that no pod may present its best case as its expected case.
EDGE_HAIRCUT = 0.5           # expected_edge = |tp-entry| * oz * lots * HAIRCUT

# BASIS (sensors/basis.py) — the futures/spot spread that S3 trades.
SPOT_SYMBOL = "XAUUSD=X"
BASIS_POLL_MINUTES = 5

# PODS.S3 — BASIS-DISLOC. Trade the reversion when the future/spot spread
# leaves its own recent band. The band is the whole thesis, so a band built on
# too little history is worse than no signal: S3_MIN_READINGS is the point
# below which the pod stays silent rather than trading its own noise.
S3_BAND_LOOKBACK = 100        # rolling readings used to build the band
S3_BAND_SIGMA = 2.5           # dislocation = |basis - mean| > sigma * stdev
S3_MIN_READINGS = 30          # thinner history -> pod silent
S3_STOP_ATR_MULT = 0.7
S3_TP_REVERT_FRACTION = 0.7   # target 70% of the way back to the band mean
S3_LOTS = 0.01

# PODS.S4 — NEWS-BURST. Enter the pullback after a high-impact USD release,
# never into it. The arm window opens AFTER the print (S4_ARM_AFTER_MIN) so the
# pod is never in the market for the spike itself.
S4_ARM_AFTER_MIN = 2          # armed from T+2m ...
S4_ARM_UNTIL_MIN = 15         # ... to T+15m after a HIGH USD release
S4_PULLBACK_FRACTION = 0.3    # entry on a 30% retrace of the burst bar
S4_STOP_ATR_MULT = 1.0
S4_TP_ATR_MULT = 1.5
S4_LOTS = 0.01

# POSITION ENGINE (exec_/position_engine.py) — one manager, both sources.
POSITION_TP1_FRACTION = 0.5    # fraction of the position closed at tp1
TRAIL_ATR_MULT_SWING = 1.0     # trail distance for swing positions, in ATR(h1)
# The only implemented policy. A doctrine that flips against an open SWING
# position tightens its stop to break-even rather than closing it outright:
# the doctrine is a view, and a view that changed is a reason to stop risking
# NEW money, not a reason to pay the spread to exit a position that may still
# be right. Adding a value here means implementing it.
DOCTRINE_FLIP_POLICY = "TIGHTEN_BE"
EOW_FLAT_ENABLED = True        # flatten pods before the weekend gap
EOW_FLAT_WEEKDAY = 4           # 4 = Friday (datetime.weekday(): Mon=0)
EOW_FLAT_HOUR_UTC = 20
EOW_FLAT_MINUTE_UTC = 30

# Observed slippage, read from the fills ledger and injected into the kernel's
# cost gate (risk/kernel.py takes this as a callable — it never queries).
SLIPPAGE_P75_LOOKBACK = 200    # newest FILLED rows considered
SLIPPAGE_P75_MIN_ROWS = 20     # below this the fallback constant stands in

# COMMAND DECK (ops/telegram_bot.py) — the human's handles.
# How long a dangerous command waits for its CONFIRM. Long enough to type it,
# short enough that a forgotten confirmation cannot be completed by someone
# who picks the phone up later.
COMMAND_CONFIRM_SECONDS = 60

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