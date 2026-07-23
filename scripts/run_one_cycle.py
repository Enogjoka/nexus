"""One-shot: fetch fresh gold data, run a single analysis cycle, print the outcome."""
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()  # must precede the app imports below — config reads env at import time

import config  # noqa: E402
from ai.analysis import run_analysis_cycle  # noqa: E402
from core.state import STATE  # noqa: E402
from data.gold_agent import run_cycle  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

run_cycle()  # populate STATE.market_data

try:
    if config.FRED_API_KEY:
        import sensors.fred as fred
        fred.run_fred_cycle(datetime.now(timezone.utc))  # so the driver shows macro too
except Exception:
    logging.getLogger(__name__).exception("run_one_cycle: fred fetch skipped")

print(run_analysis_cycle(STATE.market_data, datetime.now(timezone.utc)))
