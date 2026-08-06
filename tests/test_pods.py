"""
Acceptance tests for Task 18: the scalp-pod framework.

The clock is injected everywhere, so the midnight rollover and the breaker
expiries are tested by moving a variable rather than by waiting.

Note what is NOT tested here: the cost gate. A pod states its expected edge;
deciding whether that edge is worth the cost belongs to the kernel, and its
tests live in tests/test_kernel.py.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import config
from pods.base import Intent, Pod, PodSupervisor

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
POD_A, POD_B = config.POD_NAMES[0], config.POD_NAMES[1]


def make_intent(pod=None, **overrides):
    # `pod or POD_A` would quietly rescue an empty-string pod, hiding exactly
    # the case one of the rejection tests is trying to prove.
    fields = dict(
        pod=POD_A if pod is None else pod,
        direction="LONG",
        lots=0.05,
        entry_price=4000.0,
        stop_price=3995.0,
        tp_price=4010.0,
        expected_edge_usd=12.0,
        reason="test intent",
    )
    fields.update(overrides)
    return Intent(**fields)


class ScriptedPod(Pod):
    """Returns whatever it was told to, and counts how often it was asked."""

    def __init__(self, name, intent=None, raises=None):
        self.name = name
        self._intent = intent
        self._raises = raises
        self.calls = 0

    def evaluate(self, tick, state):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._intent


class Doctrine:
    """Duck-typed stand-in — the supervisor only reads enabled_pods."""

    def __init__(self, enabled_pods):
        self.enabled_pods = enabled_pods


# The COST_MULT floor guard lives with the gate that uses it — see
# tests/test_kernel.py::test_kernel_refuses_to_import_with_a_lowered_cost_mult.


# ===========================================================================
# Intent contract
# ===========================================================================


def test_a_valid_intent_round_trips():
    intent = make_intent()
    assert intent.pod == POD_A
    assert intent.expected_edge_usd == 12.0


def test_intent_is_frozen():
    """A supervisor must not be able to widen a pod's own stated edge."""
    intent = make_intent()
    with pytest.raises(ValidationError):
        intent.expected_edge_usd = 999.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"pod": "S9_MOONSHOT"},
        {"pod": ""},
        {"direction": "SIDEWAYS"},
        {"lots": 0},
        {"lots": -0.1},
        {"entry_price": 0},
        {"stop_price": -1},
        {"tp_price": 0},
        {"expected_edge_usd": 0},
        {"expected_edge_usd": -5.0},
        {"surprise": True},
    ],
)
def test_malformed_intents_are_rejected(overrides):
    with pytest.raises(ValidationError):
        make_intent(**overrides)


def test_every_configured_pod_name_is_accepted():
    for name in config.POD_NAMES:
        assert make_intent(pod=name).pod == name


# ===========================================================================
# doctrine drives enablement
# ===========================================================================


def test_nothing_runs_before_a_doctrine_arrives():
    """Pods are opt-in; silence enables nothing."""
    pod = ScriptedPod(POD_A, make_intent())
    supervisor = PodSupervisor([pod])

    assert supervisor.enabled == set()
    assert supervisor.evaluate_all({}, {}, NOW) == []
    assert pod.calls == 0


def test_a_doctrine_enables_only_what_it_names():
    a, b = ScriptedPod(POD_A, make_intent(POD_A)), ScriptedPod(POD_B, make_intent(POD_B))
    supervisor = PodSupervisor([a, b])

    supervisor.update_from_doctrine(Doctrine({POD_A}))
    intents = supervisor.evaluate_all({}, {}, NOW)

    assert [i.pod for i in intents] == [POD_A]
    assert b.calls == 0, "a pod outside the enabled set must never even evaluate"


def test_a_flat_doctrine_disables_everything():
    pod = ScriptedPod(POD_A, make_intent())
    supervisor = PodSupervisor([pod])
    supervisor.update_from_doctrine(Doctrine({POD_A}))
    assert supervisor.evaluate_all({}, {}, NOW)

    supervisor.update_from_doctrine(Doctrine(set()))   # FLAT carries no pods
    assert supervisor.evaluate_all({}, {}, NOW) == []


def test_enum_valued_pod_names_are_accepted():
    """Doctrine.enabled_pods holds PodEnum members, not plain strings."""

    class FakeEnum:
        def __init__(self, value):
            self.value = value

    supervisor = PodSupervisor([ScriptedPod(POD_A, make_intent())])
    supervisor.update_from_doctrine(Doctrine({FakeEnum(POD_A)}))
    assert supervisor.enabled == {POD_A}


def test_an_unknown_pod_in_a_doctrine_is_ignored(caplog):
    caplog.set_level(logging.INFO, logger="pods.base")
    supervisor = PodSupervisor([ScriptedPod(POD_A, make_intent())])
    supervisor.update_from_doctrine(Doctrine({POD_A, "S9_MOONSHOT"}))

    assert supervisor.enabled == {POD_A}
    assert "unknown pod" in caplog.text


def test_a_doctrine_without_enabled_pods_disables_everything(caplog):
    caplog.set_level(logging.INFO, logger="pods.base")
    supervisor = PodSupervisor([ScriptedPod(POD_A, make_intent())])
    supervisor.update_from_doctrine(Doctrine({POD_A}))

    supervisor.update_from_doctrine(object())   # no enabled_pods attribute

    assert supervisor.enabled == set()
    assert "no enabled_pods" in caplog.text


# ===========================================================================
# consecutive-loss breaker — latched until a doctrine clears it
# ===========================================================================


def enabled_supervisor(*pods):
    supervisor = PodSupervisor(pods)
    supervisor.update_from_doctrine(Doctrine({p.name for p in pods}))
    return supervisor


def test_three_consecutive_losses_disable_the_pod(caplog):
    caplog.set_level(logging.INFO, logger="pods.base")
    pod = ScriptedPod(POD_A, make_intent())
    supervisor = enabled_supervisor(pod)

    for _ in range(config.POD_MAX_CONSECUTIVE_LOSSES):
        supervisor.record_outcome(POD_A, -5.0, NOW)

    assert supervisor.is_active(POD_A, NOW) is False
    assert supervisor.evaluate_all({}, {}, NOW) == []
    assert "POD_DISABLED" in caplog.text
    assert "consecutive losses" in caplog.text


def test_two_losses_do_not_disable():
    pod = ScriptedPod(POD_A, make_intent())
    supervisor = enabled_supervisor(pod)

    supervisor.record_outcome(POD_A, -5.0, NOW)
    supervisor.record_outcome(POD_A, -5.0, NOW)

    assert supervisor.is_active(POD_A, NOW) is True


def test_a_win_resets_the_losing_streak():
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()))

    supervisor.record_outcome(POD_A, -5.0, NOW)
    supervisor.record_outcome(POD_A, -5.0, NOW)
    supervisor.record_outcome(POD_A, +1.0, NOW)
    supervisor.record_outcome(POD_A, -5.0, NOW)

    assert supervisor.is_active(POD_A, NOW) is True


def test_a_scratch_does_not_extend_a_losing_streak():
    """Scratching out is not the same as being wrong."""
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()))

    supervisor.record_outcome(POD_A, -5.0, NOW)
    supervisor.record_outcome(POD_A, -5.0, NOW)
    supervisor.record_outcome(POD_A, 0.0, NOW)

    assert supervisor.is_active(POD_A, NOW) is True


def test_the_loss_latch_does_not_lapse_with_time():
    """Time does not heal being wrong about conditions."""
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()))
    for _ in range(3):
        supervisor.record_outcome(POD_A, -5.0, NOW)

    assert supervisor.is_active(POD_A, NOW + timedelta(days=3)) is False


def test_only_a_doctrine_clears_the_loss_latch(caplog):
    caplog.set_level(logging.INFO, logger="pods.base")
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()))
    for _ in range(3):
        supervisor.record_outcome(POD_A, -5.0, NOW)
    assert supervisor.is_active(POD_A, NOW) is False

    supervisor.update_from_doctrine(Doctrine({POD_A}))

    assert supervisor.is_active(POD_A, NOW) is True
    assert "clearing the consecutive-loss latch" in caplog.text
    # ...and the streak restarts from zero, not from three.
    supervisor.record_outcome(POD_A, -5.0, NOW)
    assert supervisor.is_active(POD_A, NOW) is True


def test_a_doctrine_that_omits_the_pod_does_not_clear_its_latch():
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()), ScriptedPod(POD_B, make_intent(POD_B)))
    for _ in range(3):
        supervisor.record_outcome(POD_A, -5.0, NOW)

    supervisor.update_from_doctrine(Doctrine({POD_B}))   # A not mentioned
    supervisor.update_from_doctrine(Doctrine({POD_A, POD_B}))

    # Re-enabling it later DOES clear it — that is a doctrine looking at A again.
    assert supervisor.is_active(POD_A, NOW) is True


# ===========================================================================
# daily breakers — boxed until UTC midnight
# ===========================================================================


def test_daily_loss_cap_disables_until_utc_midnight(caplog):
    caplog.set_level(logging.INFO, logger="pods.base")
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()))

    supervisor.record_outcome(POD_A, -config.POD_DAILY_LOSS_CAP_USD, NOW)

    assert supervisor.is_active(POD_A, NOW) is False
    assert "daily loss" in caplog.text
    # Still off later the same UTC day...
    assert supervisor.is_active(POD_A, NOW.replace(hour=23, minute=59)) is False
    # ...and back the next day.
    assert supervisor.is_active(POD_A, NOW + timedelta(days=1)) is True


def test_just_inside_the_daily_loss_cap_stays_active():
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()))
    supervisor.record_outcome(POD_A, -(config.POD_DAILY_LOSS_CAP_USD - 0.01), NOW)
    assert supervisor.is_active(POD_A, NOW) is True


def test_trade_count_cap_disables_until_utc_midnight(caplog):
    caplog.set_level(logging.INFO, logger="pods.base")
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()))

    for _ in range(config.POD_MAX_TRADES_PER_DAY):
        supervisor.record_outcome(POD_A, +0.5, NOW)   # all winners

    assert supervisor.is_active(POD_A, NOW) is False
    assert "trades today" in caplog.text
    assert supervisor.is_active(POD_A, NOW + timedelta(days=1)) is True


def test_one_below_the_trade_cap_stays_active():
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()))
    for _ in range(config.POD_MAX_TRADES_PER_DAY - 1):
        supervisor.record_outcome(POD_A, +0.5, NOW)
    assert supervisor.is_active(POD_A, NOW) is True


def test_daily_counters_reset_on_the_utc_day_rollover():
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()))
    for _ in range(config.POD_MAX_TRADES_PER_DAY):
        supervisor.record_outcome(POD_A, +0.5, NOW)

    tomorrow = NOW + timedelta(days=1)
    assert supervisor.is_active(POD_A, tomorrow) is True

    status = supervisor.status(tomorrow)[POD_A]
    assert status["daily_trades"] == 0
    assert status["daily_pnl"] == 0.0


def test_a_doctrine_does_not_clear_a_daily_box():
    """A new day is a real event; a new doctrine is not one for these caps."""
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()))
    supervisor.record_outcome(POD_A, -config.POD_DAILY_LOSS_CAP_USD, NOW)

    supervisor.update_from_doctrine(Doctrine({POD_A}))

    assert supervisor.is_active(POD_A, NOW) is False


def test_breakers_are_per_pod():
    a, b = ScriptedPod(POD_A, make_intent(POD_A)), ScriptedPod(POD_B, make_intent(POD_B))
    supervisor = enabled_supervisor(a, b)

    for _ in range(3):
        supervisor.record_outcome(POD_A, -5.0, NOW)

    assert supervisor.is_active(POD_A, NOW) is False
    assert supervisor.is_active(POD_B, NOW) is True
    assert [i.pod for i in supervisor.evaluate_all({}, {}, NOW)] == [POD_B]


# ===========================================================================
# the sweep survives its pods
# ===========================================================================


def test_a_raising_pod_is_skipped_and_never_kills_the_sweep(caplog):
    caplog.set_level(logging.INFO, logger="pods.base")
    boomer = ScriptedPod(POD_A, raises=RuntimeError("pod exploded"))
    healthy = ScriptedPod(POD_B, make_intent(POD_B))
    supervisor = enabled_supervisor(boomer, healthy)

    intents = supervisor.evaluate_all({}, {}, NOW)

    assert [i.pod for i in intents] == [POD_B], "one broken pod must not stop the others"
    assert "raised during evaluate" in caplog.text


def test_a_pod_returning_none_is_the_normal_case():
    supervisor = enabled_supervisor(ScriptedPod(POD_A, None))
    assert supervisor.evaluate_all({}, {}, NOW) == []


def test_a_pod_returning_a_non_intent_is_discarded(caplog):
    caplog.set_level(logging.INFO, logger="pods.base")
    supervisor = enabled_supervisor(ScriptedPod(POD_A, {"not": "an intent"}))

    assert supervisor.evaluate_all({}, {}, NOW) == []
    assert "not an Intent" in caplog.text


def test_a_pod_cannot_sign_another_pods_name(caplog):
    """
    Otherwise a disabled pod keeps trading under a healthy pod's name, and its
    losses land on the wrong breaker.
    """
    caplog.set_level(logging.INFO, logger="pods.base")
    impostor = ScriptedPod(POD_A, make_intent(pod=POD_B))
    supervisor = enabled_supervisor(impostor)

    assert supervisor.evaluate_all({}, {}, NOW) == []
    assert "discarding" in caplog.text


# ===========================================================================
# status
# ===========================================================================


def test_status_reports_every_pod():
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()), ScriptedPod(POD_B, make_intent(POD_B)))
    supervisor.record_outcome(POD_A, -5.0, NOW)

    status = supervisor.status(NOW)

    assert set(status) == {POD_A, POD_B}
    assert status[POD_A]["consecutive_losses"] == 1
    assert status[POD_A]["daily_trades"] == 1
    assert status[POD_A]["active"] is True
    assert status[POD_B]["consecutive_losses"] == 0


def test_status_explains_why_a_pod_is_off():
    supervisor = enabled_supervisor(ScriptedPod(POD_A, make_intent()))
    for _ in range(3):
        supervisor.record_outcome(POD_A, -5.0, NOW)

    status = supervisor.status(NOW)[POD_A]

    assert status["active"] is False
    assert status["loss_latched"] is True
    assert "consecutive losses" in status["reason"]


def test_supervisor_uses_an_injected_clock():
    """The validator's precedent: no internal datetime.now()."""
    supervisor = PodSupervisor([ScriptedPod(POD_A, make_intent())], now_utc=lambda: NOW)
    assert supervisor.status(NOW)[POD_A]["daily_trades"] == 0
