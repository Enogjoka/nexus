"""
Acceptance tests for Task 11 (regime half): fusion/regime.py.

NO network. The HMM is trained ONLY on synthetic in-test data (training on
synthetic data outside tests is forbidden). DB-backed tests use FAR-FUTURE
(year 2099) timestamps and purge them afterward.
"""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import config
from core import database
from core.state import STATE
from fusion import regime

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")

_FUTURE = datetime(2099, 6, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    if config.DATABASE_URL:
        database.execute(
            "DELETE FROM state_vectors WHERE ts >= %s", (datetime(2099, 1, 1, tzinfo=timezone.utc),)
        )


@pytest.fixture(autouse=True)
def _isolate_state():
    saved = STATE.market_data.get("macro_regime")
    regime._refused_logged = False  # the "log once per run" latch
    yield
    if saved is None:
        STATE.market_data.pop("macro_regime", None)
    else:
        STATE.update_market_data("macro_regime", saved)
    regime._refused_logged = False


# One planted cluster per HMM state, so every fitted state corresponds to a
# real cluster rather than an arbitrary split of one. Centroids follow
# config.REGIME_FEATURES: [real_yield_5d_delta, dxy_change, curve_2s10s, news_heat]
_CLUSTERS = {
    "YIELDS_FALLING": (-0.30, -0.10, 0.60, 0.10),  # yields falling, quiet
    "NEUTRAL": (0.00, 0.00, 0.40, 0.15),           # flat, quiet
    "YIELDS_RISING": (0.30, 0.15, 0.20, 0.20),     # yields rising, quiet
    "STRESS": (0.10, 0.25, -0.20, 0.95),           # mid yields, LOUD tape
}


def _planted_matrix(n_per_cluster=50, seed=7):
    """Four well-separated synthetic clusters, one per state."""
    rng = np.random.default_rng(seed)
    blocks = [
        np.column_stack([rng.normal(mu, 0.01, n_per_cluster) for mu in centroid])
        for centroid in _CLUSTERS.values()
    ]
    return np.vstack(blocks)


def _fit_on(matrix):
    from hmmlearn.hmm import GaussianHMM

    hmm = GaussianHMM(
        n_components=config.REGIME_N_STATES, covariance_type="diag",
        n_iter=100, random_state=regime._RANDOM_STATE,
    )
    hmm.fit(matrix)
    return hmm


def _seed_rows(conn, count, start=_FUTURE, complete=True):
    with conn.cursor() as cur:
        for i in range(count):
            cur.execute(
                "INSERT INTO state_vectors (ts, real_yield_5d_delta, dxy, curve_2s10s, news_heat) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    start + timedelta(hours=i),
                    0.1 if complete else None,
                    100.0 + i * 0.01,
                    0.5,
                    0.2,
                ),
            )


# --------------------------------------------------------------------------
# refuse-to-train
# --------------------------------------------------------------------------


@requires_db
def test_fit_refuses_below_min_rows(caplog):
    import logging

    with database.get_conn() as conn:
        _seed_rows(conn, 10)  # far below REGIME_MIN_ROWS (168)
        with caplog.at_level(logging.WARNING):
            model = regime.fit(conn)

    assert model is None
    assert "refusing to train" in caplog.text
    assert STATE.market_data.get("macro_regime") is None  # nothing published


@requires_db
def test_refuse_to_train_logs_once_per_run(caplog):
    import logging

    with database.get_conn() as conn:
        _seed_rows(conn, 5)
        with caplog.at_level(logging.WARNING):
            regime.fit(conn)
            regime.fit(conn)

    assert caplog.text.count("refusing to train") == 1


@requires_db
def test_rows_missing_features_do_not_count_toward_the_minimum(caplog):
    import logging

    with database.get_conn() as conn:
        # Plenty of rows, but every one is missing real_yield_5d_delta.
        _seed_rows(conn, config.REGIME_MIN_ROWS + 20, complete=False)
        with caplog.at_level(logging.WARNING):
            model = regime.fit(conn)

    assert model is None  # incomplete history is not history
    assert "refusing to train" in caplog.text


# --------------------------------------------------------------------------
# feature matrix construction
# --------------------------------------------------------------------------


def test_build_feature_matrix_computes_dxy_change_and_drops_the_first_row():
    rows = [
        (_FUTURE, 0.1, 100.0, 0.5, 0.2),
        (_FUTURE + timedelta(hours=1), 0.2, 100.5, 0.6, 0.3),
        (_FUTURE + timedelta(hours=2), 0.3, 100.2, 0.7, 0.4),
    ]
    matrix = regime.build_feature_matrix(rows)

    # First row has no predecessor -> dropped rather than given a fabricated 0.
    assert matrix.shape == (2, 4)
    assert abs(matrix[0][1] - 0.5) < 1e-9    # 100.5 - 100.0
    assert abs(matrix[1][1] - (-0.3)) < 1e-9  # 100.2 - 100.5


def test_build_feature_matrix_drops_incomplete_rows():
    rows = [
        (_FUTURE, 0.1, 100.0, 0.5, 0.2),
        (_FUTURE + timedelta(hours=1), None, 100.5, 0.6, 0.3),   # missing yield delta
        (_FUTURE + timedelta(hours=2), 0.3, 100.2, None, 0.4),   # missing curve
        (_FUTURE + timedelta(hours=3), 0.4, 100.4, 0.7, 0.5),    # complete
    ]
    matrix = regime.build_feature_matrix(rows)
    assert matrix.shape == (1, 4)  # only the last row survives


# --------------------------------------------------------------------------
# label assignment rule
# --------------------------------------------------------------------------


def test_label_states_assigns_stress_to_loudest_news_then_ranks_by_yield():
    hmm = _fit_on(_planted_matrix())
    labels = regime.label_states(hmm)

    assert set(labels.values()) <= set(config.REGIME_LABELS)
    assert len(labels) == config.REGIME_N_STATES

    means = np.asarray(hmm.means_)
    heat_index = config.REGIME_FEATURES.index("news_heat")
    yield_index = config.REGIME_FEATURES.index("real_yield_5d_delta")

    # STRESS is the loudest state, whatever its yield rank.
    stress_state = int(np.argmax(means[:, heat_index]))
    assert labels[stress_state] == "STRESS"

    # The rest are ordered by mean yield delta, ascending.
    others = sorted((s for s in labels if s != stress_state), key=lambda s: means[s, yield_index])
    assert [labels[s] for s in others] == ["YIELDS_FALLING", "NEUTRAL", "YIELDS_RISING"]


def test_label_assignment_is_stable_across_identical_refits():
    matrix = _planted_matrix()
    assert regime.label_states(_fit_on(matrix)) == regime.label_states(_fit_on(matrix))


# --------------------------------------------------------------------------
# classification + STATE publication
# --------------------------------------------------------------------------


def test_current_regime_writes_state_key():
    hmm = _fit_on(_planted_matrix())
    model = regime.RegimeModel(hmm, regime.label_states(hmm), datetime.now(timezone.utc), 200)

    # Feed the STRESS cluster's own centroid back in.
    centroid = dict(zip(config.REGIME_FEATURES, _CLUSTERS["STRESS"]))
    label = regime.current_regime(model, centroid)

    assert label in config.REGIME_LABELS
    assert STATE.get_market_data("macro_regime") == label
    assert label == "STRESS"  # the loudest cluster classifies as STRESS


def test_current_regime_classifies_each_planted_cluster_to_its_own_label():
    hmm = _fit_on(_planted_matrix())
    model = regime.RegimeModel(hmm, regime.label_states(hmm), datetime.now(timezone.utc), 200)

    for expected_label, centroid in _CLUSTERS.items():
        row = dict(zip(config.REGIME_FEATURES, centroid))
        assert regime.current_regime(model, row) == expected_label


def test_current_regime_refuses_incomplete_features():
    hmm = _fit_on(_planted_matrix())
    model = regime.RegimeModel(hmm, regime.label_states(hmm), datetime.now(timezone.utc), 200)

    label = regime.current_regime(
        model, {"real_yield_5d_delta": 0.3, "dxy_change": None, "curve_2s10s": 0.5, "news_heat": 0.9}
    )

    assert label is None
    assert STATE.market_data.get("macro_regime") is None  # nothing published


def test_current_regime_with_no_model_is_none():
    assert regime.current_regime(None, {}) is None


# --------------------------------------------------------------------------
# the validator bridge is CONFIG, not code
# --------------------------------------------------------------------------


def test_regime_to_validator_mapping_is_config_driven():
    # Only STRESS reaches the validator, and only as its own vocabulary word.
    assert regime.to_validator_value("STRESS") == "RISK_OFF"
    for label in ("YIELDS_FALLING", "NEUTRAL", "YIELDS_RISING"):
        assert regime.to_validator_value(label) is None  # RULE 5 skips
    assert regime.to_validator_value(None) is None


def test_regime_labels_never_collide_with_validator_vocabulary():
    # The validator owns "RISK_OFF"; this module must never emit it directly.
    assert "RISK_OFF" not in config.REGIME_LABELS


def test_mapping_respects_a_config_change(monkeypatch):
    # Proving the bridge is config: change the dict, behavior follows, no code edit.
    monkeypatch.setitem(config.REGIME_TO_VALIDATOR, "NEUTRAL", "RISK_OFF")
    assert regime.to_validator_value("NEUTRAL") == "RISK_OFF"
