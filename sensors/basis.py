"""
The futures/spot basis sensor.

basis = GC=F (COMEX gold future) - XAUUSD=X (spot). Two prices for the same
metal, so the spread between them is small and mean-reverting; it wanders with
carry and funding and occasionally dislocates. S3 trades that reversion, and
this sensor is the pod's ENTIRE evidence base.

WHICH MAKES THE BAND THE DANGEROUS PART. mean and stdev over too few readings
describe the sensor's own startup noise, not the market's behaviour, and a pod
sized off that would fire constantly on a band that is simply too tight.
band() therefore returns None below config.S3_MIN_READINGS rather than a
number, and S3 stays silent. A thin band is not a weak signal to be discounted
later — it is not a signal at all.

Both legs are fetched independently and BOTH must succeed. A basis computed
from a fresh future and a stale spot is not a spread, it is a lag, and the pod
cannot tell the difference from the number alone.

NOT REGISTERED IN backend.py. run_basis_agent() is a loop-style agent and
belongs in the registry as kind="loop", but backend.py is out of scope for
this task — Task 21 adds the entry alongside the pod wiring.
"""
import argparse
import logging
import math
import statistics
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# CLI-only: `python3 -m sensors.basis --once` is a standalone entry point, so
# .env must be loaded HERE, before `import config` below reads the environment.
# A library import (or the agent loop started by backend.py) skips this branch
# and inherits the parent process's environment.
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

import config
from core import database
from core.state import STATE

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 20


def _last_close(symbol: str) -> Optional[float]:
    """
    Most recent close for `symbol` via yfinance, or None.

    INVARIANT 6: timeout-wrapped, try/except-guarded, failure logged. This
    never raises and never hangs the caller — a dead feed costs a reading, not
    the agent.
    """
    try:
        import yfinance as yf

        frame = yf.Ticker(symbol).history(
            period="1d", interval="5m", timeout=_FETCH_TIMEOUT_SECONDS
        )
    except Exception as exc:
        logger.error("basis: fetch failed for %s (%s)", symbol, exc)
        return None

    try:
        if frame is None or frame.empty:
            logger.error("basis: empty frame for %s", symbol)
            return None
        value = float(frame["Close"].iloc[-1])
    except Exception as exc:
        logger.error("basis: unreadable frame for %s (%s)", symbol, exc)
        return None

    if not math.isfinite(value) or value <= 0:
        logger.error("basis: implausible close %r for %s", value, symbol)
        return None
    return value


def fetch_basis() -> Optional[Dict[str, Any]]:
    """
    Both legs and their spread, or None.

    BOTH legs are required. A spread built from one live price and one missing
    price is not a spread at all, and half a reading is worse than none because
    it looks like data.
    """
    gc_price = _last_close(config.YF_SYMBOL)
    spot_price = _last_close(config.SPOT_SYMBOL)

    if gc_price is None or spot_price is None:
        logger.warning(
            "basis: incomplete legs (gc=%s spot=%s); no reading this cycle",
            gc_price, spot_price,
        )
        return None

    return {
        "ts": datetime.now(timezone.utc),
        "gc_price": gc_price,
        "spot_price": spot_price,
        "basis": gc_price - spot_price,
    }


def persist_reading(conn, reading: Dict[str, Any]) -> bool:
    """Append one reading. ON CONFLICT DO NOTHING makes a re-poll idempotent."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO basis_readings (ts, gc_price, spot_price, basis) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (ts) DO NOTHING",
                (
                    reading["ts"],
                    reading["gc_price"],
                    reading["spot_price"],
                    reading["basis"],
                ),
            )
        return True
    except Exception:
        logger.exception("basis: persist failed; the reading is lost")
        return False


def band(conn) -> Optional[Dict[str, Any]]:
    """
    mean/stdev/n over the most recent config.S3_BAND_LOOKBACK readings.

    None below config.S3_MIN_READINGS. Refusing to answer is the feature: a
    band over a handful of readings is a description of this sensor's first few
    minutes, and S3 would size its entire trigger off it.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT basis FROM basis_readings WHERE basis IS NOT NULL "
                "ORDER BY ts DESC LIMIT %s",
                (config.S3_BAND_LOOKBACK,),
            )
            values = [float(row[0]) for row in cur.fetchall()]
    except Exception:
        logger.exception("basis: band query failed")
        return None

    if len(values) < config.S3_MIN_READINGS:
        logger.debug(
            "basis: %d reading(s), need %d for a band", len(values), config.S3_MIN_READINGS
        )
        return None

    mean = statistics.fmean(values)
    # Population stdev: these ARE all the readings in the window, not a sample
    # drawn from it.
    stdev = statistics.pstdev(values)
    return {"mean": mean, "stdev": stdev, "n": len(values)}


def run_basis_cycle(conn=None) -> Dict[str, Any]:
    """
    One fetch-persist-publish pass. Returns a summary for the CLI and tests.

    Network happens OUTSIDE the database checkout so a slow feed never ties up
    a pooled connection (the calendar sensor's precedent).
    """
    reading = fetch_basis()
    if reading is None:
        return {"status": "no_reading", "basis": None, "band": None}

    def _work(active_conn):
        persist_reading(active_conn, reading)
        return band(active_conn)

    try:
        if conn is not None:
            current_band = _work(conn)
        else:
            with database.get_conn() as owned:
                current_band = _work(owned)
    except Exception:
        logger.exception("basis: database work failed; publishing the raw reading only")
        current_band = None

    published = {
        "basis": reading["basis"],
        "gc_price": reading["gc_price"],
        "spot_price": reading["spot_price"],
        "mean": current_band["mean"] if current_band else None,
        "stdev": current_band["stdev"] if current_band else None,
        "n": current_band["n"] if current_band else 0,
        "fetched_at": reading["ts"],
    }
    STATE.update_market_data("basis", published)

    logger.info(
        "basis: gc=%.2f spot=%.2f basis=%+.3f band=%s",
        reading["gc_price"], reading["spot_price"], reading["basis"],
        (
            f"mean {current_band['mean']:+.3f} stdev {current_band['stdev']:.3f} "
            f"n={current_band['n']}"
            if current_band
            else f"unavailable (need {config.S3_MIN_READINGS} readings)"
        ),
    )
    return {"status": "ok", "basis": reading["basis"], "band": current_band, "published": published}


def run_basis_agent() -> None:
    """
    Poll forever. Survival contract: any cycle failure is logged and the loop
    continues (INVARIANT 6).

    Registered in backend.py as kind="loop" by Task 21 — this module does not
    touch the registry.
    """
    logger.info("basis agent starting: every %s minutes", config.BASIS_POLL_MINUTES)
    while True:
        try:
            run_basis_cycle()
        except Exception:
            logger.exception("basis agent: cycle failed; continuing")
        time.sleep(config.BASIS_POLL_MINUTES * 60)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NEXUS futures/spot basis sensor")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    if not args.once:
        parser.error("nothing to do; pass --once")

    result = run_basis_cycle()
    print()
    if result["status"] != "ok":
        print("NO READING — one or both legs failed to fetch (see the log above).")
        return

    published = result["published"]
    print(f"  GC=F        : {published['gc_price']:.2f}")
    print(f"  XAUUSD=X    : {published['spot_price']:.2f}")
    print(f"  basis       : {published['basis']:+.3f}")
    if result["band"]:
        print(
            f"  band        : mean {published['mean']:+.3f} "
            f"stdev {published['stdev']:.3f} over n={published['n']}"
        )
    else:
        print(
            f"  band        : unavailable — need {config.S3_MIN_READINGS} readings, "
            f"S3 stays silent until then"
        )
    print()


if __name__ == "__main__":
    main()
