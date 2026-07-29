"""
NEXUS regime brain — a Hidden Markov Model over the MACRO state, not price.

Price-based regime detection already exists per timeframe in the data agent
(TREND_UP / RANGE / VOLATILE). This is a different question: what *world* are
we in — falling real yields, rising real yields, or genuine stress? Those are
persistent, slow-moving states with sticky transitions, which is exactly the
shape an HMM is for.

Features (config.REGIME_FEATURES), all read from state_vectors history:
    real_yield_5d_delta   the direction real yields are moving
    dxy_change            change in DXY between consecutive rows
    curve_2s10s           the 2s10s slope
    news_heat             how loud the tape is

Refusal to train is a feature: with fewer than config.REGIME_MIN_ROWS complete
rows (one week of hourly history) the model would be fitting noise and naming
it a regime, so it declines, logs once, and leaves macro_regime None.

Label assignment (deterministic, so labels stay stable across retrains):
  1. STRESS  = the state with the highest mean news_heat, regardless of where
     its yield mean ranks. Stress is defined by the tape being loud, not by
     the direction of yields.
  2. The remaining states are sorted ASCENDING by mean real_yield_5d_delta and
     take the remaining labels in order: YIELDS_FALLING, NEUTRAL, YIELDS_RISING.

The validator's vocabulary is NOT this module's vocabulary. risk/validator.py
RULE 5 reacts to "RISK_OFF" and is UNTOUCHABLE; renaming its values is
forbidden. config.REGIME_TO_VALIDATOR is the single bridge between the two,
and it is CONFIG, not code — a label that maps to None means the validator
sees no macro_regime and RULE 5 simply skips.
"""
import argparse
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np

# CLI-only: `python3 -m fusion.regime --once` is a standalone entry point, so
# .env must be loaded here, BEFORE `import config` below reads the environment
# (config.py reads env vars at module-import time). A library import of this
# module does NOT hit this branch -- mirrors the sensors.
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

import config
from core import database
from core.state import BUS, STATE

logger = logging.getLogger(__name__)

_RANDOM_STATE = 42  # fixed so a given history always trains to the same model
_STRESS_LABEL = "STRESS"

# "log once per run" bookkeeping for the refuse-to-train path.
_refused_logged = False


class RegimeModel:
    """A fitted HMM plus the label assignment for its states. Carrying both
    together is what lets current_regime(model, row) return a stable label
    rather than a meaningless state index."""

    def __init__(self, hmm, labels: Dict[int, str], trained_at: datetime, n_rows: int):
        self.hmm = hmm
        self.labels = labels
        self.trained_at = trained_at
        self.n_rows = n_rows

    def label_for(self, state_index: int) -> Optional[str]:
        return self.labels.get(int(state_index))


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def build_feature_matrix(rows: List[tuple]) -> np.ndarray:
    """
    Turn ordered (ts, real_yield_5d_delta, dxy, curve_2s10s, news_heat) rows
    into the HMM's feature matrix.

    dxy_change is the difference between consecutive rows' dxy — at the
    table's hourly cadence that is an hourly change, computed exactly as
    specified ("from consecutive rows"). The first row has no predecessor and
    is therefore dropped rather than assigned a fabricated 0.

    Any row missing a feature is dropped outright: an HMM cannot consume NaN,
    and imputing one would invent macro history.
    """
    matrix: List[List[float]] = []
    previous_dxy: Optional[float] = None

    for _ts, real_yield_5d_delta, dxy, curve_2s10s, news_heat in rows:
        dxy_value = _num(dxy)
        delta = _num(real_yield_5d_delta)
        curve = _num(curve_2s10s)
        heat = _num(news_heat)

        dxy_change = None if (dxy_value is None or previous_dxy is None) else dxy_value - previous_dxy
        if dxy_value is not None:
            previous_dxy = dxy_value

        if None in (delta, dxy_change, curve, heat):
            continue
        matrix.append([delta, dxy_change, curve, heat])

    return np.asarray(matrix, dtype=float)


def load_history(conn) -> np.ndarray:
    """Read the macro columns from state_vectors in time order and build the
    feature matrix."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, real_yield_5d_delta, dxy, curve_2s10s, news_heat "
            "FROM state_vectors ORDER BY ts"
        )
        rows = cur.fetchall()
    return build_feature_matrix(rows)


def label_states(hmm) -> Dict[int, str]:
    """
    Assign stable labels to fitted states per the rule in the module docstring:
    STRESS goes to the loudest-news state; the rest are ranked by mean
    real_yield_5d_delta ascending.
    """
    means = np.asarray(hmm.means_, dtype=float)
    yield_index = config.REGIME_FEATURES.index("real_yield_5d_delta")
    heat_index = config.REGIME_FEATURES.index("news_heat")

    stress_state = int(np.argmax(means[:, heat_index]))
    labels: Dict[int, str] = {stress_state: _STRESS_LABEL}

    remaining = [s for s in range(means.shape[0]) if s != stress_state]
    remaining.sort(key=lambda s: means[s, yield_index])
    ordered_labels = [label for label in config.REGIME_LABELS if label != _STRESS_LABEL]

    for state, label in zip(remaining, ordered_labels):
        labels[state] = label
    return labels


def fit(conn) -> Optional[RegimeModel]:
    """
    Train the HMM on whatever complete history exists. Returns None — and logs
    once per run — when there are fewer than config.REGIME_MIN_ROWS usable
    rows, or when the fit itself fails. A refusal leaves macro_regime None
    rather than publishing a guess.
    """
    global _refused_logged

    matrix = load_history(conn)
    if matrix.shape[0] < config.REGIME_MIN_ROWS:
        if not _refused_logged:
            logger.warning(
                "regime: refusing to train — %d complete macro rows, need %d "
                "(macro_regime stays None)",
                matrix.shape[0],
                config.REGIME_MIN_ROWS,
            )
            _refused_logged = True
        return None

    try:
        from hmmlearn.hmm import GaussianHMM

        hmm = GaussianHMM(
            n_components=config.REGIME_N_STATES,
            covariance_type="diag",
            n_iter=100,
            random_state=_RANDOM_STATE,
        )
        hmm.fit(matrix)
    except Exception:
        logger.exception("regime: HMM fit failed; macro_regime stays None")
        return None

    model = RegimeModel(
        hmm=hmm,
        labels=label_states(hmm),
        trained_at=datetime.now(timezone.utc),
        n_rows=matrix.shape[0],
    )
    logger.info("regime: trained on %d rows; labels=%s", model.n_rows, model.labels)
    return model


def _emission_state(hmm, features: List[float]) -> int:
    """
    Which state's Gaussian best explains this single observation.

    Deliberately NOT hmm.predict(): on a length-1 sequence Viterbi is dominated
    by startprob_, which reflects nothing but the order the training rows
    happened to arrive in — every lone observation would then decode toward
    whichever regime the history began in. Scoring by emission likelihood
    alone answers the question actually being asked ("which regime does the
    present look like"), independent of training-set ordering.
    """
    x = np.asarray(features, dtype=float)
    means = np.asarray(hmm.means_, dtype=float)

    covars = np.asarray(hmm.covars_, dtype=float)
    # hmmlearn exposes covars_ as full matrices for some covariance types;
    # reduce to per-feature variances either way.
    variances = np.array([np.diag(c) for c in covars]) if covars.ndim == 3 else covars
    variances = np.maximum(variances, 1e-12)  # guard a degenerate fit

    log_likelihood = -0.5 * np.sum(
        ((x - means) ** 2) / variances + np.log(2.0 * np.pi * variances), axis=1
    )
    return int(np.argmax(log_likelihood))


def current_regime(model: RegimeModel, latest_row: Dict[str, Any]) -> Optional[str]:
    """
    Classify the latest macro row and publish the label to
    STATE.market_data["macro_regime"].

    `latest_row` must carry every key in config.REGIME_FEATURES (including the
    derived dxy_change). A missing or non-finite feature yields None and
    publishes nothing — an unclassifiable present is not a regime.
    """
    if model is None:
        return None

    features = [_num(latest_row.get(name)) for name in config.REGIME_FEATURES]
    if any(value is None for value in features):
        logger.info("current_regime: incomplete macro features %s; not classifying", latest_row)
        return None

    try:
        state = _emission_state(model.hmm, features)
    except Exception:
        logger.exception("current_regime: predict failed")
        return None

    label = model.label_for(state)
    STATE.update_market_data("macro_regime", label)
    logger.info("current_regime: state=%d label=%s", state, label)
    return label


def to_validator_value(label: Optional[str]) -> Optional[str]:
    """Translate a regime label into the validator's own vocabulary via the
    config bridge. None means RULE 5 sees no macro_regime and skips."""
    if label is None:
        return None
    return config.REGIME_TO_VALIDATOR.get(label)


def latest_feature_row(conn) -> Optional[Dict[str, float]]:
    """Build the current feature row (including dxy_change) from the two most
    recent state_vectors rows. None when there is not enough history."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, real_yield_5d_delta, dxy, curve_2s10s, news_heat "
            "FROM state_vectors ORDER BY ts DESC LIMIT 2"
        )
        rows = cur.fetchall()
    if len(rows) < 2:
        return None

    newest, previous = rows[0], rows[1]
    dxy_now, dxy_prev = _num(newest[2]), _num(previous[2])
    return {
        "real_yield_5d_delta": _num(newest[1]),
        "dxy_change": None if (dxy_now is None or dxy_prev is None) else dxy_now - dxy_prev,
        "curve_2s10s": _num(newest[3]),
        "news_heat": _num(newest[4]),
    }


def run_regime_agent() -> None:
    """
    Subscribe to BUS "market_update": retrain at most every
    config.REGIME_RETRAIN_HOURS, and classify the latest row on every event.
    Any exception is logged and swallowed so the agent — and the data agent
    publishing to it — survive (INVARIANT 6).
    """
    state: Dict[str, Any] = {"model": None, "trained_at": None}

    def _on_market_update(_payload) -> None:
        try:
            now = datetime.now(timezone.utc)
            stale = (
                state["trained_at"] is None
                or (now - state["trained_at"]) >= timedelta(hours=config.REGIME_RETRAIN_HOURS)
            )
            with database.get_conn() as conn:
                if stale:
                    state["model"] = fit(conn)
                    state["trained_at"] = now
                if state["model"] is None:
                    return
                row = latest_feature_row(conn)
            if row is not None:
                current_regime(state["model"], row)
        except Exception:
            logger.exception("run_regime_agent: market_update handler raised; continuing")

    BUS.subscribe("market_update", _on_market_update)
    logger.info("regime agent subscribed to market_update")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NEXUS macro regime brain (HMM)")
    parser.add_argument(
        "--once", action="store_true", help="Train (if possible), classify the latest row, and exit"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.once:
        with database.get_conn() as conn:
            model = fit(conn)
            if model is None:
                print({"trained": False, "macro_regime": None, "validator_value": None})
                return
            row = latest_feature_row(conn)
        label = current_regime(model, row) if row is not None else None
        print(
            {
                "trained": True,
                "rows": model.n_rows,
                "labels": model.labels,
                "macro_regime": label,
                "validator_value": to_validator_value(label),
            }
        )
        return

    parser.error("nothing to do; pass --once")


if __name__ == "__main__":
    main()
