"""
Acceptance tests for Task 11 (RAG half): fusion/rag.py.

NO network (the embedding is computed locally by design, so there is nothing
to mock). DB-backed tests use FAR-FUTURE (year 2099) timestamps so they sort
ahead of and never collide with real rows; the autouse fixture purges them.
"""
import math
from datetime import datetime, timedelta, timezone

import pytest

import config
from core import database
from fusion import rag

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")

_FUTURE = datetime(2099, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    if not config.DATABASE_URL:
        return
    # signals first: it FKs state_vectors.
    database.execute("DELETE FROM signals WHERE symbol LIKE 'TST_RAG%'")
    database.execute(
        "DELETE FROM state_vectors WHERE ts >= %s", (datetime(2099, 1, 1, tzinfo=timezone.utc),)
    )


def make_sv_row(**overrides):
    """A fully-populated state-vector row dict."""
    row = {
        "rsi_h1": 50.0, "rsi_h4": 50.0, "rsi_d1": 50.0, "bb_position_h1": 50.0,
        "atr_h1": 25.0, "atr_h4": 50.0,
        "real_yield": 1.0, "real_yield_5d_delta": 0.0, "curve_2s10s": 0.5,
        "breakeven_10y": 2.5, "dxy": 105.0,
        "cot_mm_net_pctile": 50.0, "comex_coverage": 25.0,
        "news_heat": 0.5, "minutes_to_next_high_event": 60.0, "fix_window": False,
        "regime_h4": "TREND_UP", "session": "LONDON",
    }
    row.update(overrides)
    return row


def extreme_row(end: str):
    """A state with EVERY numeric dim pinned to one end of its configured
    range. Two of these are the most-opposed complete states expressible."""
    index = 0 if end == "low" else 1
    values = {field: bounds[index] for field, bounds in config.RAG_DIM_RANGES.items()}
    categoricals = (
        {"regime_h4": "TREND_UP", "session": "LONDON"}
        if end == "low"
        else {"regime_h4": "TREND_DOWN", "session": "ASIA"}
    )
    return make_sv_row(**values, **categoricals)


def _insert_sv(conn, ts, embedding=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO state_vectors (ts, rsi_h1, real_yield, news_heat, regime_h4, session) "
            "VALUES (%s, 50, 1.0, 0.5, 'TREND_UP', 'LONDON') RETURNING id",
            (ts,),
        )
        sv_id = cur.fetchone()[0]
        if embedding is not None:
            import json

            cur.execute(
                "UPDATE state_vectors SET embedding = %s::jsonb WHERE id = %s",
                (json.dumps(embedding), sv_id),
            )
    return sv_id


def _insert_signal(conn, ts, sv_id=None, status="TP2", outcome_r=2.0, symbol="TST_RAG"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO signals (ts, symbol, direction, grade, status, outcome_r, state_vector_id) "
            "VALUES (%s, %s, 'LONG', 'A', %s, %s, %s) RETURNING id",
            (ts, symbol, status, outcome_r, sv_id),
        )
        return cur.fetchone()[0]


# --------------------------------------------------------------------------
# vectorize: determinism, dim order, missing-vs-middle
# --------------------------------------------------------------------------


def test_vectorize_is_deterministic_and_matches_dim_order():
    row = make_sv_row()
    first, second = rag.vectorize(row), rag.vectorize(row)

    assert first == second                       # pure function
    assert len(first) == len(rag.RAG_DIM_ORDER)  # every dim accounted for
    assert len(first) == rag.RAG_DIM_COUNT


def test_vectorize_normalizes_against_fixed_ranges():
    vector = rag.vectorize(make_sv_row(rsi_h1=100.0, news_heat=0.0, dxy=105.0))
    index = {name: i for i, name in enumerate(rag.RAG_DIM_ORDER)}

    assert vector[index["rsi_h1"]] == 1.0        # top of 0-100
    assert vector[index["news_heat"]] == 0.0     # bottom of 0-1
    assert vector[index["dxy"]] == 0.5           # 105 is the midpoint of 90-120


def test_vectorize_clamps_out_of_range_values():
    vector = rag.vectorize(make_sv_row(rsi_h1=999.0, real_yield=-99.0))
    index = {name: i for i, name in enumerate(rag.RAG_DIM_ORDER)}
    assert vector[index["rsi_h1"]] == 1.0
    assert vector[index["real_yield"]] == 0.0


def test_vectorize_missing_is_distinguishable_from_middle():
    index = {name: i for i, name in enumerate(rag.RAG_DIM_ORDER)}

    missing = rag.vectorize(make_sv_row(rsi_h1=None))
    middle = rag.vectorize(make_sv_row(rsi_h1=50.0))

    # Both sit at 0.5 on the value dim...
    assert missing[index["rsi_h1"]] == 0.5
    assert middle[index["rsi_h1"]] == 0.5
    # ...and ONLY the presence mask tells them apart. That is its whole job.
    assert missing[index["rsi_h1__present"]] == 0.0
    assert middle[index["rsi_h1__present"]] == 1.0
    assert missing != middle


def test_vectorize_one_hots_categoricals_and_encodes_absence_as_all_zero():
    index = {name: i for i, name in enumerate(rag.RAG_DIM_ORDER)}

    vector = rag.vectorize(make_sv_row(regime_h4="VOLATILE", session="NY"))
    assert vector[index["regime_h4__VOLATILE"]] == 1.0
    assert vector[index["regime_h4__TREND_UP"]] == 0.0
    assert vector[index["session__NY"]] == 1.0

    absent = rag.vectorize(make_sv_row(regime_h4=None, session=None))
    for value in config.RAG_CATEGORICAL_VALUES["regime_h4"]:
        assert absent[index[f"regime_h4__{value}"]] == 0.0


def test_vectorize_treats_bools_as_values_not_missing():
    index = {name: i for i, name in enumerate(rag.RAG_DIM_ORDER)}
    on = rag.vectorize(make_sv_row(fix_window=True))
    off = rag.vectorize(make_sv_row(fix_window=False))

    assert on[index["fix_window"]] == 1.0
    assert off[index["fix_window"]] == 0.0
    assert on[index["fix_window__present"]] == 1.0  # a bool is data, not a gap


# --------------------------------------------------------------------------
# cosine similarity — hand-computed
# --------------------------------------------------------------------------


def test_cosine_similarity_hand_computed():
    assert rag.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0          # identical
    assert rag.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0          # orthogonal
    # 45 degrees apart -> cos(45) = sqrt(2)/2
    assert abs(rag.cosine_similarity([1.0, 0.0], [1.0, 1.0]) - math.sqrt(2) / 2) < 1e-12
    # magnitude must not matter, only direction
    assert abs(rag.cosine_similarity([1.0, 2.0], [5.0, 10.0]) - 1.0) < 1e-12


def test_embedding_similarity_range_is_compressed_by_construction():
    """
    Documents a real property of this embedding: every dim is non-negative
    (values in [0,1], presence masks 1.0 when data is present), so cosine can
    never approach 0 between two complete rows. These are the measured
    landmarks RAG_MIN_SIM is calibrated against -- anyone retuning it needs
    exactly these numbers.
    """
    low = rag.vectorize(extreme_row("low"))
    high = rag.vectorize(extreme_row("high"))
    mid = rag.vectorize(make_sv_row())
    all_missing = rag.vectorize(
        {field: None for field in list(config.RAG_DIM_RANGES) + list(config.RAG_CATEGORICAL_VALUES)}
    )

    assert rag.cosine_similarity(low, low) == pytest.approx(1.0)
    # Maximally-opposed COMPLETE states bottom out near 0.65, not 0.
    assert 0.60 < rag.cosine_similarity(low, high) < 0.70
    # A merely-DIFFERENT state (mid-range vs one extreme) sits near 0.88.
    assert 0.85 < rag.cosine_similarity(mid, high) < 0.90
    # An all-missing row is the furthest thing from a complete one (~0.40).
    assert 0.35 < rag.cosine_similarity(mid, all_missing) < 0.45

    # RAG_MIN_SIM=0.92 sits ABOVE the "merely different" landmark, so both
    # opposed AND merely-different states are excluded; only genuinely close
    # precedents recall. (At the previous 0.75 the middle case slipped through.)
    assert rag.cosine_similarity(low, high) < config.RAG_MIN_SIM
    assert rag.cosine_similarity(mid, high) < config.RAG_MIN_SIM
    assert rag.cosine_similarity(low, low) > config.RAG_MIN_SIM


def test_cosine_similarity_degenerate_inputs_are_zero():
    assert rag.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector
    assert rag.cosine_similarity([1.0], [1.0, 1.0]) == 0.0       # length mismatch


# --------------------------------------------------------------------------
# wilson_stats — hand-computed
# --------------------------------------------------------------------------


def test_wilson_stats_three_wins_of_four_hand_computed():
    recalls = [{"outcome_r": r} for r in (2.0, 1.5, 3.0, -1.0)]

    stats = rag.wilson_stats(recalls)

    assert stats["n"] == 4
    assert stats["wins"] == 3
    assert stats["win_rate"] == 0.75
    # Wilson 95% lower bound for 3/4 is ~0.3006 -- far below the naive 75%,
    # which is exactly the point of using it on small samples.
    assert abs(stats["wilson_lower"] - 0.30063) < 1e-4
    assert abs(stats["avg_r"] - (2.0 + 1.5 + 3.0 - 1.0) / 4) < 1e-12


def test_wilson_stats_empty_is_none():
    assert rag.wilson_stats([]) is None


def test_wilson_lower_bound_never_exceeds_win_rate():
    for wins, losses in ((1, 0), (5, 0), (3, 1), (50, 50)):
        recalls = [{"outcome_r": 1.0}] * wins + [{"outcome_r": -1.0}] * losses
        stats = rag.wilson_stats(recalls)
        assert 0.0 <= stats["wilson_lower"] <= stats["win_rate"]


# --------------------------------------------------------------------------
# recall: similarity floor + min-sample guard
# --------------------------------------------------------------------------


@requires_db
def test_recall_returns_empty_below_min_samples():
    target = rag.vectorize(make_sv_row())
    with database.get_conn() as conn:
        # only TWO identical precedents -> below RAG_MIN_SAMPLES (3)
        for offset in range(2):
            sv_id = _insert_sv(conn, _FUTURE + timedelta(hours=offset), embedding=target)
            _insert_signal(conn, _FUTURE + timedelta(hours=offset), sv_id=sv_id)
        results = rag.recall(conn, target)

    assert results == []  # thin recall is no recall


@requires_db
def test_recall_filters_by_similarity_floor_and_ranks_by_similarity():
    # The query is one extreme. TWO kinds of outlier must be excluded at
    # RAG_MIN_SIM=0.92: the exact opposite (~0.65) and a merely-DIFFERENT
    # mid-range state (~0.88) that the previous 0.75 floor would have admitted.
    target = rag.vectorize(extreme_row("low"))
    opposed = rag.vectorize(extreme_row("high"))
    merely_different = rag.vectorize(make_sv_row())
    assert rag.cosine_similarity(target, opposed) < config.RAG_MIN_SIM
    assert rag.cosine_similarity(target, merely_different) < config.RAG_MIN_SIM

    with database.get_conn() as conn:
        for offset in range(3):  # 3 near-identical precedents -> clears the guard
            sv_id = _insert_sv(conn, _FUTURE + timedelta(hours=offset), embedding=target)
            _insert_signal(conn, _FUTURE + timedelta(hours=offset), sv_id=sv_id, outcome_r=2.0)
        opposed_id = _insert_sv(conn, _FUTURE + timedelta(hours=9), embedding=opposed)
        _insert_signal(conn, _FUTURE + timedelta(hours=9), sv_id=opposed_id, outcome_r=-1.0)
        different_id = _insert_sv(conn, _FUTURE + timedelta(hours=10), embedding=merely_different)
        _insert_signal(conn, _FUTURE + timedelta(hours=10), sv_id=different_id, outcome_r=-1.0)

        results = rag.recall(conn, target)

    assert len(results) == 3  # only the near-identical precedents survive
    assert all(item["similarity"] >= config.RAG_MIN_SIM for item in results)
    recalled_ids = [item["state_vector_id"] for item in results]
    assert opposed_id not in recalled_ids
    assert different_id not in recalled_ids
    # sorted most-similar first
    assert results == sorted(results, key=lambda i: i["similarity"], reverse=True)


@requires_db
def test_recall_ignores_signals_without_terminal_outcomes():
    target = rag.vectorize(make_sv_row())
    with database.get_conn() as conn:
        for offset, status in enumerate(("PENDING", "OPEN", "EXPIRED")):
            sv_id = _insert_sv(conn, _FUTURE + timedelta(hours=offset), embedding=target)
            _insert_signal(
                conn, _FUTURE + timedelta(hours=offset), sv_id=sv_id, status=status, outcome_r=None
            )
        results = rag.recall(conn, target)

    assert results == []  # an un-resolved signal teaches nothing about outcomes


# --------------------------------------------------------------------------
# embed_and_store / link_latest / backfill
# --------------------------------------------------------------------------


@requires_db
def test_embed_and_store_writes_the_embedding():
    with database.get_conn() as conn:
        sv_id = _insert_sv(conn, _FUTURE)
        vector = rag.embed_and_store(conn, sv_id)

    assert len(vector) == rag.RAG_DIM_COUNT
    stored = database.fetch("SELECT embedding FROM state_vectors WHERE id = %s", (sv_id,))[0][0]
    assert stored == vector


@requires_db
def test_embed_and_store_missing_row_returns_none():
    with database.get_conn() as conn:
        assert rag.embed_and_store(conn, -1) is None


@requires_db
def test_link_latest_links_within_window_and_picks_the_newest():
    signal_ts = _FUTURE + timedelta(hours=3)
    with database.get_conn() as conn:
        older = _insert_sv(conn, signal_ts - timedelta(minutes=80))
        newest = _insert_sv(conn, signal_ts - timedelta(minutes=20))
        signal_id = _insert_signal(conn, signal_ts, sv_id=None)

        linked = rag.link_latest(conn, signal_id)

    assert linked == newest       # newest within the window wins
    assert linked != older
    stored = database.fetch("SELECT state_vector_id FROM signals WHERE id = %s", (signal_id,))
    assert stored[0][0] == newest


@requires_db
def test_link_latest_respects_the_window_boundary():
    signal_ts = _FUTURE + timedelta(hours=5)
    with database.get_conn() as conn:
        # 91 minutes back -> one minute outside RAG_LINK_WINDOW_MINUTES (90)
        _insert_sv(conn, signal_ts - timedelta(minutes=91))
        signal_id = _insert_signal(conn, signal_ts, sv_id=None)

        assert rag.link_latest(conn, signal_id) is None

    stored = database.fetch("SELECT state_vector_id FROM signals WHERE id = %s", (signal_id,))
    assert stored[0][0] is None  # left unlinked rather than attached to a stale state


@requires_db
def test_link_latest_never_links_a_future_state_vector():
    signal_ts = _FUTURE + timedelta(hours=7)
    with database.get_conn() as conn:
        _insert_sv(conn, signal_ts + timedelta(minutes=10))  # AFTER the signal
        signal_id = _insert_signal(conn, signal_ts, sv_id=None)
        assert rag.link_latest(conn, signal_id) is None


@requires_db
def test_backfill_is_idempotent():
    """
    NOTE: backfill() is global by design (it is the catch-up path), so it also
    touches ambient rows. Assertions therefore avoid depending on ambient
    counts: they check that OUR seeded rows got processed and that a second
    pass finds nothing left to do. Re-embedding is harmless — the embedding is
    derived, deterministic data.
    """
    signal_ts = _FUTURE + timedelta(hours=11)
    with database.get_conn() as conn:
        sv_id = _insert_sv(conn, signal_ts - timedelta(minutes=30))
        signal_id = _insert_signal(conn, signal_ts, sv_id=None)

        first = rag.backfill(conn)
        second = rag.backfill(conn)

    # Our seeded row was embedded and our seeded signal was linked...
    embedding = database.fetch("SELECT embedding FROM state_vectors WHERE id = %s", (sv_id,))[0][0]
    assert embedding is not None and len(embedding) == rag.RAG_DIM_COUNT
    linked = database.fetch("SELECT state_vector_id FROM signals WHERE id = %s", (signal_id,))[0][0]
    assert linked == sv_id
    assert first["embedded"] >= 1 and first["linked"] >= 1

    # ...and the second pass is a no-op: nothing left un-embedded or unlinked.
    assert second["embedded"] == 0
    assert second["linked"] == 0
