"""
Acceptance tests for Task 20: S3, S4, and the basis sensor.

Every expected number is hand-computed from the config constants.

WHAT IS AND IS NOT PROVEN HERE, FOR S4: the arming window, the blackout belt
and the pullback arithmetic are tested exactly. Performance is NOT, and cannot
be at H1 — see the banner in pods/s4_newsburst.py. Nothing in this file
produces or asserts an S4 P&L number.

DB-backed tests run inside a transaction that is never committed, because
band() reads "the most recent N readings" across the whole table and the live
sensor writes into that same table. Seeding without isolating would make the
band depend on whether acceptance C had been run yet.
"""
import math
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

import config
from core.state import STATE
from pods.base import Intent
from pods.s3_basis import S3Basis
from pods.s4_newsburst import S4NewsBurst, armed_event, in_blackout
from sensors import basis as basis_mod
from sensors.calendar_agent import recent_high_events

UTC = timezone.utc
NOW = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")


@contextmanager
def scoped_conn():
    """
    A connection owning the whole table for one test, always rolled back.

    band() aggregates over the newest N rows table-wide — correct production
    behaviour — so pinning its arithmetic means owning the table. Ambient rows
    are deleted inside a transaction that is never committed; the real data is
    untouched the moment the block exits, including on failure.
    """
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM basis_readings")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def seed_basis(conn, values, start=NOW):
    with conn.cursor() as cur:
        for i, value in enumerate(values):
            cur.execute(
                "INSERT INTO basis_readings (ts, gc_price, spot_price, basis) "
                "VALUES (%s, %s, %s, %s)",
                (start + timedelta(minutes=i), 4000.0 + value, 4000.0, value),
            )


def tick(mid, ts=NOW):
    spread = config.SIM_SPREAD_USD
    return {"ts": ts, "mid": mid, "bid": mid - spread / 2, "ask": mid + spread / 2}


# ===========================================================================
# the basis sensor
# ===========================================================================


def test_basis_is_future_minus_spot(monkeypatch):
    prices = {config.YF_SYMBOL: 4300.5, config.SPOT_SYMBOL: 4298.2}
    monkeypatch.setattr(basis_mod, "_last_close", lambda symbol: prices[symbol])

    reading = basis_mod.fetch_basis()

    assert reading["gc_price"] == 4300.5
    assert reading["spot_price"] == 4298.2
    assert reading["basis"] == pytest.approx(2.3)


@pytest.mark.parametrize(
    "gc,spot", [(None, 4298.2), (4300.5, None), (None, None)]
)
def test_a_missing_leg_yields_no_reading(monkeypatch, gc, spot):
    """Half a spread is worse than none, because it looks like data."""
    prices = {config.YF_SYMBOL: gc, config.SPOT_SYMBOL: spot}
    monkeypatch.setattr(basis_mod, "_last_close", lambda symbol: prices[symbol])

    assert basis_mod.fetch_basis() is None


def test_last_close_never_raises_on_a_dead_feed(monkeypatch):
    """INVARIANT 6: a dead feed costs a reading, not the agent."""

    class Boom:
        def Ticker(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setitem(__import__("sys").modules, "yfinance", Boom())
    assert basis_mod._last_close("GC=F") is None


@requires_db
def test_band_math_is_hand_computed_over_thirty_readings():
    """
    Seeded basis values 1..30.
      mean  = 15.5
      var   = (n^2 - 1)/12 = 899/12 = 74.9167    (population, uniform ints)
      stdev = 8.65544
    """
    with scoped_conn() as conn:
        seed_basis(conn, [float(v) for v in range(1, 31)])
        band = basis_mod.band(conn)

    assert band is not None
    assert band["n"] == 30
    assert band["mean"] == pytest.approx(15.5)
    assert band["stdev"] == pytest.approx(math.sqrt(899 / 12))


@requires_db
def test_a_thin_history_produces_no_band():
    """Refusing to answer is the feature — S3 stays silent rather than
    trading a band that describes the sensor's own startup."""
    with scoped_conn() as conn:
        seed_basis(conn, [float(v) for v in range(1, config.S3_MIN_READINGS)])
        assert basis_mod.band(conn) is None


@requires_db
def test_band_uses_only_the_lookback_window():
    with scoped_conn() as conn:
        # 120 readings; only the newest S3_BAND_LOOKBACK (100) may count.
        seed_basis(conn, [float(v) for v in range(1, 121)])
        band = basis_mod.band(conn)

    assert band["n"] == config.S3_BAND_LOOKBACK
    # The newest 100 are values 21..120 -> mean 70.5
    assert band["mean"] == pytest.approx(70.5)


@requires_db
def test_a_reading_is_persisted_and_republished(monkeypatch):
    prices = {config.YF_SYMBOL: 4300.0, config.SPOT_SYMBOL: 4297.5}
    monkeypatch.setattr(basis_mod, "_last_close", lambda symbol: prices[symbol])

    with scoped_conn() as conn:
        result = basis_mod.run_basis_cycle(conn=conn)
        with conn.cursor() as cur:
            cur.execute("SELECT gc_price, spot_price, basis FROM basis_readings")
            rows = cur.fetchall()

    assert result["status"] == "ok"
    assert len(rows) == 1
    assert float(rows[0][2]) == pytest.approx(2.5)
    assert STATE.get_market_data("basis")["basis"] == pytest.approx(2.5)


# ===========================================================================
# S3 BASIS-DISLOC
# ===========================================================================


S3_STATE = {
    "atr_h1": 10.0,
    "basis": {"basis": 10.0, "mean": 2.0, "stdev": 2.0, "n": 50},
}


def test_s3_hand_computed_rich_basis_goes_short():
    """
    basis 10.0, mean 2.0, stdev 2.0, n 50, atr 10, mid 4000
      deviation = +8.0 > 2.5 * 2.0 = 5.0   -> rich -> SHORT
      entry = 4000
      stop  = 4000 + 0.7*10 = 4007
      tp    = 4000 - (8.0 * 0.7) = 3994.4
      edge  = 5.6 * 100 * 0.01 * 0.5 = 2.80
    """
    intent = S3Basis().evaluate(tick(4000.0), dict(S3_STATE))

    assert intent is not None
    assert intent.pod == "S3_BASIS"
    assert intent.direction == "SHORT"
    assert intent.entry_price == pytest.approx(4000.0)
    assert intent.stop_price == pytest.approx(4007.0)
    assert intent.tp_price == pytest.approx(3994.4)
    assert intent.expected_edge_usd == pytest.approx(2.80)


def test_s3_hand_computed_cheap_basis_goes_long():
    state = {**S3_STATE, "basis": {"basis": -6.0, "mean": 2.0, "stdev": 2.0, "n": 50}}
    intent = S3Basis().evaluate(tick(4000.0), state)

    assert intent.direction == "LONG"
    assert intent.stop_price == pytest.approx(3993.0)
    assert intent.tp_price == pytest.approx(4005.6)


def test_s3_ignores_a_deviation_inside_the_band():
    # 2.5 * 2.0 = 5.0; exactly 5.0 is not strictly greater.
    inside = {**S3_STATE, "basis": {"basis": 7.0, "mean": 2.0, "stdev": 2.0, "n": 50}}
    assert S3Basis().evaluate(tick(4000.0), inside) is None

    outside = {**S3_STATE, "basis": {"basis": 7.01, "mean": 2.0, "stdev": 2.0, "n": 50}}
    assert S3Basis().evaluate(tick(4000.0), outside) is not None


def test_s3_is_silent_on_a_thin_history():
    thin = {
        **S3_STATE,
        "basis": {"basis": 100.0, "mean": 2.0, "stdev": 2.0, "n": config.S3_MIN_READINGS - 1},
    }
    assert S3Basis().evaluate(tick(4000.0), thin) is None, "a huge deviation must not rescue a thin band"


@pytest.mark.parametrize(
    "reading",
    [
        None,
        {},
        {"basis": None, "mean": 2.0, "stdev": 2.0, "n": 50},
        {"basis": 10.0, "mean": None, "stdev": 2.0, "n": 50},
        {"basis": 10.0, "mean": 2.0, "stdev": None, "n": 50},
        {"basis": 10.0, "mean": 2.0, "stdev": 0.0, "n": 50},
        {"basis": 10.0, "mean": 2.0, "stdev": -1.0, "n": 50},
        {"basis": float("nan"), "mean": 2.0, "stdev": 2.0, "n": 50},
    ],
)
def test_s3_declines_on_a_broken_band(reading):
    assert S3Basis().evaluate(tick(4000.0), {"atr_h1": 10.0, "basis": reading}) is None


def test_s3_declines_without_atr():
    assert S3Basis().evaluate(tick(4000.0), {**S3_STATE, "atr_h1": None}) is None


# ===========================================================================
# S4 ARMING — the heart of what H1 can actually prove
# ===========================================================================


S4_STATE = {
    "atr_h1": 10.0,
    "recent_high_events": [{"minutes_since": 5.0, "name": "TST_S4 CPI"}],
    "upcoming_events": [],
    "burst_bar": {"open": 4000.0, "high": 4025.0, "low": 3998.0, "close": 4020.0},
}


@pytest.mark.parametrize(
    "minutes_since,expect_intent",
    [(1.9, False), (2.0, True), (8.0, True), (15.0, True), (15.1, False), (60.0, False)],
)
def test_s4_arms_only_inside_its_window(minutes_since, expect_intent):
    """
    Opens at T+2m so the pod is never in the market for the spike itself,
    closes at T+15m when the reverberation is over.
    """
    state = {
        **S4_STATE,
        "recent_high_events": [{"minutes_since": minutes_since, "name": "TST_S4 CPI"}],
    }
    intent = S4NewsBurst().evaluate(tick(4020.0), state)
    assert (intent is not None) is expect_intent


def test_s4_is_silent_without_a_recent_release():
    assert S4NewsBurst().evaluate(tick(4020.0), {**S4_STATE, "recent_high_events": []}) is None
    assert S4NewsBurst().evaluate(tick(4020.0), {**S4_STATE, "recent_high_events": None}) is None


def test_s4_blackout_belt_overrides_a_valid_arm():
    """
    Both conditions present at once: a release 3 minutes ago (armed) AND
    another HIGH event 10 minutes out (blackout). Being right about the first
    event is no defence against walking into the second.
    """
    state = {
        **S4_STATE,
        "recent_high_events": [{"minutes_since": 3.0, "name": "TST_S4 CPI"}],
        "upcoming_events": [
            {"minutes_until": 10.0, "impact": "HIGH", "name": "TST_S4 FOMC"}
        ],
    }
    assert S4NewsBurst().evaluate(tick(4020.0), state) is None


def test_s4_blackout_ignores_non_high_and_distant_events():
    """The belt must not be so wide it never lets the pod trade."""
    state = {
        **S4_STATE,
        "recent_high_events": [{"minutes_since": 3.0, "name": "TST_S4 CPI"}],
        "upcoming_events": [
            {"minutes_until": 10.0, "impact": "MEDIUM", "name": "medium"},
            {"minutes_until": config.EVENT_BLOCK_MINUTES + 1, "impact": "HIGH", "name": "far"},
        ],
    }
    assert S4NewsBurst().evaluate(tick(4020.0), state) is not None


def test_in_blackout_boundaries():
    assert in_blackout([{"minutes_until": config.EVENT_BLOCK_MINUTES, "impact": "HIGH"}]) is True
    assert in_blackout([{"minutes_until": config.EVENT_BLOCK_MINUTES + 0.1, "impact": "HIGH"}]) is False
    assert in_blackout([{"minutes_until": -1.0, "impact": "HIGH"}]) is False
    assert in_blackout([]) is False
    assert in_blackout(None) is False


def test_armed_event_returns_the_matching_event():
    events = [
        {"minutes_since": 40.0, "name": "too old"},
        {"minutes_since": 5.0, "name": "TST_S4 CPI"},
    ]
    assert armed_event(events)["name"] == "TST_S4 CPI"
    assert armed_event([{"minutes_since": 40.0, "name": "too old"}]) is None


# --- pullback arithmetic ----------------------------------------------------


def test_s4_hand_computed_long_pullback():
    """
    burst bar open 4000 -> close 4020 (move +20), atr 10
      pullback = 20 * 0.3 = 6
      entry    = 4020 - 6 = 4014
      stop     = 4014 - 1.0*10 = 4004
      tp       = 4014 + 1.5*10 = 4029
      edge     = 15 * 100 * 0.01 * 0.5 = 7.50
    """
    intent = S4NewsBurst().evaluate(tick(4020.0), dict(S4_STATE))

    assert intent.pod == "S4_NEWSBURST"
    assert intent.direction == "LONG"
    assert intent.entry_price == pytest.approx(4014.0)
    assert intent.stop_price == pytest.approx(4004.0)
    assert intent.tp_price == pytest.approx(4029.0)
    assert intent.expected_edge_usd == pytest.approx(7.50)


def test_s4_hand_computed_short_pullback():
    """Mirrored: open 4020 -> close 4000, entry 4006, stop 4016, tp 3991."""
    state = {
        **S4_STATE,
        "burst_bar": {"open": 4020.0, "high": 4022.0, "low": 3995.0, "close": 4000.0},
    }
    intent = S4NewsBurst().evaluate(tick(4000.0), state)

    assert intent.direction == "SHORT"
    assert intent.entry_price == pytest.approx(4006.0)
    assert intent.stop_price == pytest.approx(4016.0)
    assert intent.tp_price == pytest.approx(3991.0)


def test_s4_declines_on_a_flat_burst_bar():
    """No burst, no direction to continue."""
    state = {**S4_STATE, "burst_bar": {"open": 4000.0, "close": 4000.0}}
    assert S4NewsBurst().evaluate(tick(4000.0), state) is None


@pytest.mark.parametrize(
    "override",
    [
        {"burst_bar": None},
        {"burst_bar": {}},
        {"burst_bar": {"open": None, "close": 4020.0}},
        {"burst_bar": {"open": 4000.0, "close": None}},
        {"atr_h1": None},
        {"atr_h1": 0},
    ],
)
def test_s4_declines_on_broken_inputs(override):
    assert S4NewsBurst().evaluate(tick(4020.0), {**S4_STATE, **override}) is None


def test_s4_docstring_forbids_treating_replay_as_evidence():
    """The banner is load-bearing: it is the only thing stopping a future
    session from quoting an H1 P&L for a tick-native pod."""
    import pods.s4_newsburst as module

    doc = module.__doc__ or ""
    assert "TICK-NATIVE" in doc
    assert "NOT EVIDENCE" in doc


# ===========================================================================
# neither pod may ever raise
# ===========================================================================


GARBAGE = [
    (None, None),
    ({}, {}),
    ({"ts": "nonsense"}, {"atr_h1": "nope"}),
    ({"ts": NOW, "mid": None}, S3_STATE),
    ({"ts": NOW, "mid": float("nan")}, S3_STATE),
    ({"ts": NOW, "mid": 4000.0}, {"basis": "not a dict", "atr_h1": 10.0}),
    ({"ts": NOW, "mid": 4000.0}, {"basis": {"basis": [1], "mean": {}, "stdev": 2.0, "n": 50}}),
    ({"ts": NOW, "mid": 4020.0}, {"recent_high_events": "nope", "atr_h1": 10.0}),
    ({"ts": NOW, "mid": 4020.0}, {"recent_high_events": [None, 5], "atr_h1": 10.0}),
    ({"ts": NOW, "mid": 4020.0}, {"recent_high_events": [{"minutes_since": "soon"}]}),
]


@pytest.mark.parametrize("bad_tick,bad_state", GARBAGE)
def test_pods_never_raise_on_garbage(bad_tick, bad_state):
    for pod in (S3Basis(), S4NewsBurst()):
        result = pod.evaluate(bad_tick, bad_state)
        assert result is None or isinstance(result, Intent)


# ===========================================================================
# recent_high_events
# ===========================================================================


@contextmanager
def scoped_events_conn():
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def seed_events(conn, rows, base=NOW):
    with conn.cursor() as cur:
        for minutes_ago, impact, name in rows:
            cur.execute(
                "INSERT INTO econ_events (event_ts, name, currency, impact) "
                "VALUES (%s, %s, 'USD', %s)",
                (base - timedelta(minutes=minutes_ago), name, impact),
            )


@requires_db
def test_recent_high_events_window_math_is_exact():
    with scoped_events_conn() as conn:
        seed_events(
            conn,
            [
                (5.0, "HIGH", "TST_S4 five"),
                (19.9, "HIGH", "TST_S4 just inside"),
                (20.1, "HIGH", "TST_S4 just outside"),
                (-5.0, "HIGH", "TST_S4 not yet released"),
            ],
        )
        events = recent_high_events(conn, NOW, lookback_min=20)

    names = {e["name"] for e in events}
    assert "TST_S4 five" in names
    assert "TST_S4 just inside" in names
    assert "TST_S4 just outside" not in names
    assert "TST_S4 not yet released" not in names, "a future event can never arm a pod"

    by_name = {e["name"]: e["minutes_since"] for e in events}
    assert by_name["TST_S4 five"] == pytest.approx(5.0)
    assert by_name["TST_S4 just inside"] == pytest.approx(19.9)


@requires_db
def test_recent_high_events_excludes_medium_and_low():
    """A MEDIUM print is not a weaker HIGH, it is a different thing."""
    with scoped_events_conn() as conn:
        seed_events(
            conn,
            [
                (5.0, "HIGH", "TST_S4 high"),
                (5.0, "MEDIUM", "TST_S4 medium"),
                (5.0, "LOW", "TST_S4 low"),
            ],
        )
        events = recent_high_events(conn, NOW, lookback_min=20)

    names = {e["name"] for e in events}
    assert names == {"TST_S4 high"}


@requires_db
def test_recent_high_events_is_empty_when_nothing_released():
    with scoped_events_conn() as conn:
        assert recent_high_events(conn, NOW, lookback_min=20) == []
