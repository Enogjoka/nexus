"""
Acceptance tests for Task 14: Ring 0, the sovereign risk kernel.

Every breaker gets two tests: a scenario that trips it, and a near-miss that
must still pass. A breaker that fires on everything is as broken as one that
fires on nothing, and only the pair pins the boundary.

The whole world is injected, so nothing here touches a broker, a market, or a
model. KILL_FILE_PATH and DEMOTED_FLAG_PATH are redirected into tmp_path in
every test: a real ./KILL in the repo can never change a result, and no test
can leave a flag file behind.
"""
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import config
from core import database
from risk import kernel as kernel_mod
from risk.kernel import Kernel, OrderRequest, Verdict

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXED_NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


class FakeWorld:
    """Everything the kernel is allowed to know, all of it injectable."""

    def __init__(
        self,
        equity=5000.0,
        positions=None,
        spread=0.20,
        tick_age=5.0,
        flatten_result=True,
    ):
        self.equity = equity
        self.positions = list(positions or [])
        self.spread = spread
        self.tick_age = tick_age
        self.flatten_result = flatten_result

        self.equity_error = None
        self.positions_error = None
        self.spread_error = None
        self.tick_error = None

        self.flatten_calls = []
        self.now = FIXED_NOW
        self.broker = None

    # injected callables
    def get_equity(self):
        if self.equity_error:
            raise self.equity_error
        return self.equity

    def get_open_positions(self):
        if self.positions_error:
            raise self.positions_error
        return list(self.positions)

    def get_spread(self):
        if self.spread_error:
            raise self.spread_error
        return self.spread

    def get_tick_age(self):
        if self.tick_error:
            raise self.tick_error
        return self.tick_age

    def flatten_all(self, reason):
        self.flatten_calls.append(reason)
        return self.flatten_result

    def broker_positions(self):
        return list(self.broker or [])

    def now_utc(self):
        return self.now


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Redirect both operational flag files out of the repo."""
    monkeypatch.setattr(config, "KILL_FILE_PATH", str(tmp_path / "KILL"))
    monkeypatch.setattr(config, "DEMOTED_FLAG_PATH", str(tmp_path / "DEMOTED"))
    return tmp_path


def build(world, with_broker=False):
    return Kernel(
        get_equity=world.get_equity,
        get_open_positions=world.get_open_positions,
        get_spread=world.get_spread,
        get_tick_age_seconds=world.get_tick_age,
        flatten_all=world.flatten_all,
        broker_positions=world.broker_positions if with_broker else None,
        now_utc=world.now_utc,
    )


_ORDER_SEQ = {"n": 0}


def make_order(**overrides):
    _ORDER_SEQ["n"] += 1
    base = dict(
        direction="LONG",
        lots=0.05,
        entry_price=4000.0,
        stop_price=3990.0,
        source="SWING",
        client_order_id=f"test-{os.getpid()}-{_ORDER_SEQ['n']}",
    )
    base.update(overrides)
    return OrderRequest(**base)


def events_for(client_order_id):
    return database.fetch(
        "SELECT breaker, action, reason FROM kernel_events "
        "WHERE context->>'client_order_id' = %s ORDER BY id",
        (client_order_id,),
    )


# ===========================================================================
# baseline
# ===========================================================================


def test_clean_order_is_permitted(sandbox):
    verdict = build(FakeWorld()).permit(make_order())
    assert verdict.allowed is True
    assert verdict.breaker is None
    assert verdict.clamped_lots is None


# ===========================================================================
# 1. kill switch
# ===========================================================================


def test_kill_file_denies_and_flattens(sandbox):
    (sandbox / "KILL").write_text("stop everything", encoding="utf-8")
    world = FakeWorld()
    verdict = build(world).permit(make_order())

    assert verdict.allowed is False
    assert verdict.breaker == kernel_mod.KILL_SWITCH
    assert world.flatten_calls == ["kill switch"]


def test_kill_switch_outranks_every_other_breaker(sandbox):
    """First denial wins, and the kill file is checked before anything else."""
    (sandbox / "KILL").write_text("stop", encoding="utf-8")
    world = FakeWorld(spread=99.0, tick_age=9999.0, equity=1.0)
    verdict = build(world).permit(make_order())
    assert verdict.breaker == kernel_mod.KILL_SWITCH


def test_absent_kill_file_permits(sandbox):
    assert not (sandbox / "KILL").exists()
    assert build(FakeWorld()).permit(make_order()).allowed is True


# ===========================================================================
# 2. halted
# ===========================================================================


def test_halt_denies_subsequent_orders(sandbox):
    world = FakeWorld()
    kern = build(world)
    kern.emergency_flatten("manual halt")

    verdict = kern.permit(make_order())
    assert verdict.allowed is False
    assert verdict.breaker == kernel_mod.HALTED
    assert kern.halted is True


def test_daily_loss_halt_lapses_at_utc_midnight(sandbox):
    world = FakeWorld(equity=5000.0)
    kern = build(world)

    world.equity = 4900.0  # -2.0%, past the 1.5% cap
    assert kern.permit(make_order()).breaker == kernel_mod.DAILY_LOSS
    assert kern.halted is True

    # Still the same UTC day: the halt holds.
    world.now = FIXED_NOW + timedelta(hours=6)
    assert kern.permit(make_order()).breaker == kernel_mod.HALTED

    # Past midnight: the halt lapses and the day's baseline rolls forward.
    world.now = FIXED_NOW.replace(hour=0, minute=1) + timedelta(days=1)
    verdict = kern.permit(make_order())
    assert verdict.allowed is True, verdict.reason
    assert kern.halted is False
    assert kern.day_start_equity == 4900.0


def test_non_expiring_halt_survives_midnight(sandbox):
    """Only the daily-loss halt expires. Everything else needs a restart."""
    world = FakeWorld()
    kern = build(world)
    kern.emergency_flatten("reconciliation mismatch")

    world.now = FIXED_NOW + timedelta(days=3)
    assert kern.permit(make_order()).breaker == kernel_mod.HALTED
    assert kern.halted is True


# ===========================================================================
# 3. equity (fail closed)
# ===========================================================================


def test_equity_raising_denies(sandbox):
    world = FakeWorld()
    world.equity_error = RuntimeError("terminal not connected")
    verdict = build(world).permit(make_order())
    assert verdict.allowed is False
    assert verdict.breaker == kernel_mod.EQUITY_UNKNOWN


def test_equity_at_floor_denies(sandbox):
    verdict = build(FakeWorld(equity=config.KERNEL_EQUITY_FLOOR)).permit(make_order())
    assert verdict.breaker == kernel_mod.EQUITY_UNKNOWN


def test_equity_just_above_floor_permits(sandbox):
    world = FakeWorld(equity=config.KERNEL_EQUITY_FLOOR + 0.01)
    assert build(world).permit(make_order()).allowed is True


def test_non_finite_equity_denies(sandbox):
    verdict = build(FakeWorld(equity=float("nan"))).permit(make_order())
    assert verdict.breaker == kernel_mod.EQUITY_UNKNOWN


# ===========================================================================
# 4. daily loss
# ===========================================================================


def test_daily_loss_breach_denies_and_flattens(sandbox):
    world = FakeWorld(equity=5000.0)
    kern = build(world)
    world.equity = 5000.0 - 76.0  # -1.52%

    verdict = kern.permit(make_order())
    assert verdict.breaker == kernel_mod.DAILY_LOSS
    assert world.flatten_calls, "a daily-loss breach must flatten"
    assert kern.halted is True


def test_daily_loss_near_miss_permits(sandbox):
    world = FakeWorld(equity=5000.0)
    kern = build(world)
    world.equity = 5000.0 - 74.0  # -1.48%, inside the cap
    assert kern.permit(make_order()).allowed is True


# ===========================================================================
# 5. drawdown
# ===========================================================================


def test_drawdown_breach_flattens_demotes_and_halts(sandbox):
    """Proves the kernel -> stage demotion path end to end."""
    world = FakeWorld(equity=5000.0)
    kern = build(world)

    world.equity = 10000.0            # establish a peak
    assert kern.permit(make_order()).allowed is True
    assert kern.peak_equity == 10000.0

    world.equity = 9400.0             # 6% under peak, still up on the day
    verdict = kern.permit(make_order())

    assert verdict.breaker == kernel_mod.MAX_DRAWDOWN
    assert world.flatten_calls
    assert kern.halted is True

    flag = sandbox / "DEMOTED"
    assert flag.exists(), "a drawdown breach must write the demotion flag"
    assert "drawdown" in flag.read_text(encoding="utf-8").lower()


def test_drawdown_near_miss_permits(sandbox):
    world = FakeWorld(equity=5000.0)
    kern = build(world)
    world.equity = 10000.0
    kern.permit(make_order())

    world.equity = 9600.0  # -4%, inside the 5% band
    assert kern.permit(make_order()).allowed is True
    assert not (sandbox / "DEMOTED").exists()


def test_drawdown_denial_stands_even_if_demotion_write_fails(sandbox, monkeypatch):
    world = FakeWorld(equity=5000.0)
    kern = build(world)
    world.equity = 10000.0
    kern.permit(make_order())

    def boom(reason):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(kernel_mod.stage, "write_demotion", boom)
    world.equity = 9400.0
    verdict = kern.permit(make_order())

    assert verdict.breaker == kernel_mod.MAX_DRAWDOWN
    assert kern.halted is True


# ===========================================================================
# 6. concurrent positions
# ===========================================================================


def test_position_cap_denies(sandbox):
    world = FakeWorld(
        positions=[{"lots": 0.01, "direction": "LONG"}, {"lots": 0.01, "direction": "SHORT"}]
    )
    verdict = build(world).permit(make_order())
    assert verdict.breaker == kernel_mod.MAX_POSITIONS


def test_one_position_below_cap_permits(sandbox):
    world = FakeWorld(positions=[{"lots": 0.01, "direction": "LONG"}])
    assert build(world).permit(make_order()).allowed is True


def test_unreadable_position_book_denies(sandbox):
    world = FakeWorld()
    world.positions_error = RuntimeError("broker query failed")
    verdict = build(world).permit(make_order())
    assert verdict.breaker == kernel_mod.POSITIONS_UNKNOWN


def test_position_with_unparseable_lots_denies(sandbox):
    world = FakeWorld(positions=[{"lots": "not-a-number", "direction": "LONG"}])
    verdict = build(world).permit(make_order())
    assert verdict.breaker == kernel_mod.POSITIONS_UNKNOWN


# ===========================================================================
# 7. lot caps
# ===========================================================================


def test_oversized_order_is_clamped_not_denied(sandbox):
    """PAPER caps a single order at 0.10 lots."""
    verdict = build(FakeWorld()).permit(make_order(lots=0.5))

    assert verdict.allowed is True
    assert verdict.clamped_lots == 0.10
    assert config.STAGE_POLICY["PAPER"]["max_lot_per_order"] == 0.10


def test_order_at_the_cap_is_not_clamped(sandbox):
    verdict = build(FakeWorld()).permit(make_order(lots=0.10))
    assert verdict.allowed is True
    assert verdict.clamped_lots is None


def test_total_lot_cap_denies(sandbox):
    world = FakeWorld(positions=[{"lots": 0.08, "direction": "LONG"}])
    verdict = build(world).permit(make_order(lots=0.05))
    assert verdict.breaker == kernel_mod.TOTAL_LOTS


def test_total_lot_near_miss_permits(sandbox):
    world = FakeWorld(positions=[{"lots": 0.04, "direction": "LONG"}])
    assert build(world).permit(make_order(lots=0.05)).allowed is True


def test_clamping_does_not_rescue_an_over_full_book(sandbox):
    """The clamp caps one order; it must not smuggle size past the book cap."""
    world = FakeWorld(positions=[{"lots": 0.05, "direction": "LONG"}])
    verdict = build(world).permit(make_order(lots=0.5))
    assert verdict.allowed is False
    assert verdict.breaker == kernel_mod.TOTAL_LOTS


# ===========================================================================
# 8. spread
# ===========================================================================


def test_unknown_spread_denies(sandbox):
    verdict = build(FakeWorld(spread=None)).permit(make_order())
    assert verdict.breaker == kernel_mod.SPREAD_UNKNOWN


def test_spread_getter_raising_denies(sandbox):
    world = FakeWorld()
    world.spread_error = RuntimeError("no tick")
    assert build(world).permit(make_order()).breaker == kernel_mod.SPREAD_UNKNOWN


def test_spread_above_ceiling_denies(sandbox):
    world = FakeWorld(spread=config.SPREAD_CEILING_USD + 0.01)
    assert build(world).permit(make_order()).breaker == kernel_mod.SPREAD_CEILING


def test_spread_exactly_at_ceiling_permits(sandbox):
    world = FakeWorld(spread=config.SPREAD_CEILING_USD)
    assert build(world).permit(make_order()).allowed is True


# ===========================================================================
# 9. stale data
# ===========================================================================


def test_unknown_tick_age_denies(sandbox):
    assert build(FakeWorld(tick_age=None)).permit(make_order()).breaker == kernel_mod.STALE_DATA


def test_tick_age_getter_raising_denies(sandbox):
    world = FakeWorld()
    world.tick_error = RuntimeError("feed down")
    assert build(world).permit(make_order()).breaker == kernel_mod.STALE_DATA


def test_stale_tick_denies(sandbox):
    world = FakeWorld(tick_age=config.STALE_TICK_SECONDS + 0.1)
    assert build(world).permit(make_order()).breaker == kernel_mod.STALE_DATA


def test_tick_exactly_at_limit_permits(sandbox):
    world = FakeWorld(tick_age=config.STALE_TICK_SECONDS)
    assert build(world).permit(make_order()).allowed is True


# ===========================================================================
# 10. stop geometry
# ===========================================================================


def test_long_stop_above_entry_denies(sandbox):
    verdict = build(FakeWorld()).permit(
        make_order(direction="LONG", entry_price=4000.0, stop_price=4010.0)
    )
    assert verdict.breaker == kernel_mod.STOP_GEOMETRY


def test_long_stop_equal_to_entry_denies(sandbox):
    verdict = build(FakeWorld()).permit(
        make_order(direction="LONG", entry_price=4000.0, stop_price=4000.0)
    )
    assert verdict.breaker == kernel_mod.STOP_GEOMETRY


def test_short_stop_below_entry_denies(sandbox):
    verdict = build(FakeWorld()).permit(
        make_order(direction="SHORT", entry_price=4000.0, stop_price=3990.0)
    )
    assert verdict.breaker == kernel_mod.STOP_GEOMETRY


def test_short_stop_above_entry_permits(sandbox):
    verdict = build(FakeWorld()).permit(
        make_order(direction="SHORT", entry_price=4000.0, stop_price=4010.0)
    )
    assert verdict.allowed is True


# ===========================================================================
# fail-closed trio, stated as one property
# ===========================================================================


@pytest.mark.parametrize(
    "attribute,value,expected",
    [
        ("equity_error", RuntimeError("x"), kernel_mod.EQUITY_UNKNOWN),
        ("spread_error", RuntimeError("x"), kernel_mod.SPREAD_UNKNOWN),
        ("tick_error", RuntimeError("x"), kernel_mod.STALE_DATA),
    ],
)
def test_an_unknown_never_permits(sandbox, attribute, value, expected):
    world = FakeWorld()
    setattr(world, attribute, value)
    verdict = build(world).permit(make_order())
    assert verdict.allowed is False
    assert verdict.breaker == expected


# ===========================================================================
# reconciliation
# ===========================================================================


def test_reconcile_without_a_broker_is_a_noop(sandbox):
    world = FakeWorld()
    kern = build(world, with_broker=False)

    assert kern.reconcile([{"lots": 0.1, "direction": "LONG"}], []) is True
    assert world.flatten_calls == []
    assert kern.halted is False


def test_reconcile_matching_books_passes(sandbox):
    world = FakeWorld()
    kern = build(world, with_broker=True)
    book = [{"lots": 0.05, "direction": "LONG"}, {"lots": 0.02, "direction": "SHORT"}]

    assert kern.reconcile(book, list(reversed(book))) is True
    assert kern.halted is False


def test_reconcile_mismatch_flattens_and_halts(sandbox):
    world = FakeWorld()
    kern = build(world, with_broker=True)

    result = kern.reconcile(
        [{"lots": 0.05, "direction": "LONG"}],
        [{"lots": 0.09, "direction": "LONG"}],
    )

    assert result is False
    assert world.flatten_calls == ["reconciliation mismatch"]
    assert kern.halted is True


def test_reconcile_detects_a_direction_only_mismatch(sandbox):
    kern = build(FakeWorld(), with_broker=True)
    assert kern.reconcile(
        [{"lots": 0.05, "direction": "LONG"}],
        [{"lots": 0.05, "direction": "SHORT"}],
    ) is False


def test_reconcile_tolerates_float_representation_noise(sandbox):
    kern = build(FakeWorld(), with_broker=True)
    assert kern.reconcile(
        [{"lots": 0.1 + 0.2, "direction": "LONG"}],
        [{"lots": 0.3, "direction": "LONG"}],
    ) is True


# ===========================================================================
# watchdog
# ===========================================================================


def test_watchdog_honours_the_kill_switch_with_no_order_flow(sandbox):
    world = FakeWorld()
    kern = build(world)
    (sandbox / "KILL").write_text("stop", encoding="utf-8")

    result = kern.watchdog_tick()

    assert result["kill"] is True
    assert world.flatten_calls == ["kill switch"]
    assert kern.halted is True


def test_watchdog_refreshes_peak_equity(sandbox):
    world = FakeWorld(equity=5000.0)
    kern = build(world)
    world.equity = 7500.0

    kern.watchdog_tick()

    assert kern.peak_equity == 7500.0


def test_watchdog_reconciles_when_a_broker_is_present(sandbox):
    world = FakeWorld(positions=[{"lots": 0.05, "direction": "LONG"}])
    world.broker = [{"lots": 0.05, "direction": "LONG"}]
    kern = build(world, with_broker=True)

    assert kern.watchdog_tick()["reconciled"] is True

    world.broker = [{"lots": 0.99, "direction": "LONG"}]
    assert kern.watchdog_tick()["reconciled"] is False
    assert kern.halted is True


def test_watchdog_never_raises(sandbox):
    world = FakeWorld()
    world.equity_error = RuntimeError("everything is broken")
    world.positions_error = RuntimeError("also broken")
    kern = build(world, with_broker=True)

    kern.watchdog_tick()  # must not raise


# ===========================================================================
# zero AI (the hard constraint)
# ===========================================================================


def test_kernel_imports_nothing_from_ai_or_fusion():
    for module in kernel_mod.imported_top_level_modules():
        root = module.split(".")[0]
        assert root not in ("ai", "fusion"), f"Ring 0 must not import {module}"


def test_importing_the_kernel_loads_no_ai_or_fusion_module():
    """
    Transitive proof, in a clean interpreter: nothing the kernel pulls in may
    drag ai/ or fusion/ along behind it.
    """
    code = (
        "import sys, risk.kernel;"
        "bad=sorted(m for m in sys.modules"
        " if m.split('.')[0] in ('ai','fusion'));"
        "print(','.join(bad))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"leaked modules: {result.stdout!r}"


def test_kernel_has_no_dynamic_import_escape_hatch():
    """
    The import-table test reads static imports; this closes the other door.
    importlib.import_module("ai.analysis") would satisfy an AST import scan
    while still dragging a model into Ring 0, so the kernel simply may not
    contain dynamic-import machinery at all.
    """
    import ast as ast_mod

    tree = ast_mod.parse((REPO_ROOT / "risk" / "kernel.py").read_text(encoding="utf-8"))

    for node in ast_mod.walk(tree):
        if isinstance(node, ast_mod.Name):
            assert node.id != "__import__", "Ring 0 must not call __import__"
        if isinstance(node, ast_mod.Attribute):
            assert node.attr != "import_module", "Ring 0 must not use importlib"

    assert "importlib" not in kernel_mod.imported_top_level_modules()


# ===========================================================================
# contracts
# ===========================================================================


def test_verdict_is_frozen_and_strict():
    verdict = Verdict(allowed=True, reason="ok")
    with pytest.raises(ValidationError):
        verdict.allowed = False
    with pytest.raises(ValidationError):
        Verdict(allowed=True, reason="ok", surprise=1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"lots": 0},
        {"lots": -0.1},
        {"entry_price": 0},
        {"stop_price": -1},
        {"direction": "SIDEWAYS"},
        {"source": "HUNCH"},
        {"surprise": True},
    ],
)
def test_order_request_rejects_malformed_input(overrides):
    base = dict(
        direction="LONG",
        lots=0.05,
        entry_price=4000.0,
        stop_price=3990.0,
        source="SWING",
        client_order_id="x",
    )
    base.update(overrides)
    with pytest.raises(ValidationError):
        OrderRequest(**base)


def test_order_request_is_frozen():
    order = make_order()
    with pytest.raises(ValidationError):
        order.lots = 99.0


def test_policy_is_read_once_at_construction(sandbox, monkeypatch):
    """A policy that could change under a running kernel is not a policy."""
    kern = build(FakeWorld())
    before = kern.policy

    monkeypatch.setattr(
        kernel_mod.stage, "execution_policy", lambda: (_ for _ in ()).throw(AssertionError)
    )
    assert kern.permit(make_order()).allowed is True
    assert kern.policy is before


# ===========================================================================
# audit trail
# ===========================================================================


def test_allow_writes_an_audit_row(sandbox):
    order = make_order()
    build(FakeWorld()).permit(order)

    rows = events_for(order.client_order_id)
    assert [r[1] for r in rows] == ["ALLOW"]
    assert rows[0][0] == kernel_mod.NONE


def test_clamp_writes_its_own_row_before_the_allow(sandbox):
    order = make_order(lots=0.5)
    build(FakeWorld()).permit(order)

    actions = [r[1] for r in events_for(order.client_order_id)]
    assert actions == ["CLAMP", "ALLOW"]


def test_flatten_writes_a_row(sandbox):
    world = FakeWorld()
    kern = build(world)
    before = database.fetch("SELECT count(*) FROM kernel_events WHERE action='FLATTEN'")[0][0]

    kern.emergency_flatten("test flatten")

    after = database.fetch("SELECT count(*) FROM kernel_events WHERE action='FLATTEN'")[0][0]
    assert after == before + 1


def test_audit_context_carries_the_numbers_the_decision_turned_on(sandbox):
    order = make_order(lots=0.5)
    build(FakeWorld(spread=0.33)).permit(order)

    rows = database.fetch(
        "SELECT context FROM kernel_events "
        "WHERE context->>'client_order_id' = %s AND action='ALLOW'",
        (order.client_order_id,),
    )
    context = rows[0][0]
    if isinstance(context, str):
        context = json.loads(context)
    assert context["spread"] == 0.33
    assert context["equity"] == 5000.0
    assert context["effective_lots"] == 0.10


def test_kernel_survives_a_dead_database(sandbox, monkeypatch, caplog):
    """INVARIANT 6: losing the black box must never cost a safety verdict."""
    caplog.set_level(logging.INFO, logger="risk.kernel")

    def unavailable(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(database, "execute", unavailable)

    assert build(FakeWorld()).permit(make_order()).allowed is True
    world = FakeWorld(spread=None)
    assert build(world).permit(make_order()).breaker == kernel_mod.SPREAD_UNKNOWN
    assert any("could not record" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# property sweep: no silent verdicts
# ---------------------------------------------------------------------------


def _denial_scenarios(sandbox):
    """One factory per breaker reachable through permit()."""

    def kill():
        (sandbox / "KILL").write_text("stop", encoding="utf-8")
        return FakeWorld(), {}

    def equity_unknown():
        world = FakeWorld()
        world.equity_error = RuntimeError("x")
        return world, {}

    def positions_unknown():
        world = FakeWorld()
        world.positions_error = RuntimeError("x")
        return world, {}

    def max_positions():
        return FakeWorld(positions=[{"lots": 0.01, "direction": "LONG"}] * 2), {}

    def total_lots():
        return FakeWorld(positions=[{"lots": 0.08, "direction": "LONG"}]), {}

    def spread_unknown():
        return FakeWorld(spread=None), {}

    def spread_ceiling():
        return FakeWorld(spread=9.0), {}

    def stale():
        return FakeWorld(tick_age=9999.0), {}

    def geometry():
        return FakeWorld(), {"stop_price": 4100.0}

    return [
        (kernel_mod.KILL_SWITCH, kill),
        (kernel_mod.EQUITY_UNKNOWN, equity_unknown),
        (kernel_mod.POSITIONS_UNKNOWN, positions_unknown),
        (kernel_mod.MAX_POSITIONS, max_positions),
        (kernel_mod.TOTAL_LOTS, total_lots),
        (kernel_mod.SPREAD_UNKNOWN, spread_unknown),
        (kernel_mod.SPREAD_CEILING, spread_ceiling),
        (kernel_mod.STALE_DATA, stale),
        (kernel_mod.STOP_GEOMETRY, geometry),
    ]


def test_every_denial_leaves_an_audit_row(sandbox):
    """
    Property-style sweep: for every breaker reachable through permit(), the
    denial and the row must both exist. No silent verdicts.
    """
    missing = []
    for expected_breaker, factory in _denial_scenarios(sandbox):
        world, order_overrides = factory()
        order = make_order(**order_overrides)
        verdict = build(world).permit(order)

        assert verdict.allowed is False, f"{expected_breaker} scenario was permitted"
        assert verdict.breaker == expected_breaker

        rows = events_for(order.client_order_id)
        denials = [r for r in rows if r[1] == "DENY" and r[0] == expected_breaker]
        if not denials:
            missing.append((expected_breaker, rows))

        # cleanup between scenarios
        kill_file = sandbox / "KILL"
        if kill_file.exists():
            kill_file.unlink()

    assert missing == [], f"denials with no audit row: {missing}"


def test_daily_loss_and_drawdown_denials_are_audited(sandbox):
    """The two breakers the sweep cannot reach without mutating equity."""
    world = FakeWorld(equity=5000.0)
    kern = build(world)
    world.equity = 4900.0
    order = make_order()
    assert kern.permit(order).breaker == kernel_mod.DAILY_LOSS
    assert any(r[0] == kernel_mod.DAILY_LOSS and r[1] == "DENY" for r in events_for(order.client_order_id))

    world2 = FakeWorld(equity=5000.0)
    kern2 = build(world2)
    world2.equity = 10000.0
    kern2.permit(make_order())
    world2.equity = 9400.0
    order2 = make_order()
    assert kern2.permit(order2).breaker == kernel_mod.MAX_DRAWDOWN
    assert any(
        r[0] == kernel_mod.MAX_DRAWDOWN and r[1] == "DENY" for r in events_for(order2.client_order_id)
    )
