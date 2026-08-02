"""
Acceptance tests for Task 13: the stage governor.

Every test drives the module the only way it can be driven — a fresh import.
There is no setter to call, which is exactly what INVARIANT 1 requires, so
`importlib.reload` after changing the environment is the whole test harness.

The demotion flag path is redirected into tmp_path in every test, so a real
./DEMOTED file sitting in the repo root can never change a result here.
"""
import importlib
import logging
import os

import pytest
from pydantic import ValidationError

import config
import risk.stage as stage_mod
from core import database


@pytest.fixture(autouse=True)
def _restore_stage_modules():
    """Leave config and risk.stage in their default, undemoted state."""
    yield
    os.environ.pop("NEXUS_STAGE", None)
    importlib.reload(config)
    importlib.reload(stage_mod)


def load_stage(monkeypatch, tmp_path, env_stage, demoted_reason=None):
    """
    Reload risk.stage as if the process had just booted with `env_stage` set
    and, optionally, a demotion flag already on disk.
    """
    monkeypatch.setenv("NEXUS_STAGE", env_stage)
    importlib.reload(config)

    flag = tmp_path / "DEMOTED"
    if demoted_reason is not None:
        flag.write_text(demoted_reason, encoding="utf-8")
    monkeypatch.setattr(config, "DEMOTED_FLAG_PATH", str(flag))

    return importlib.reload(stage_mod)


# ---------------------------------------------------------------------------
# the policy table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_stage,fill_mode,per_order,total,concurrent",
    [
        ("PAPER", "MODELED", 0.10, 0.10, 2),
        ("SHADOW", "SHADOW_REAL_BIDASK", 0.10, 0.10, 2),
        ("MICRO", "LIVE_FIXED_MICRO", 0.01, 0.02, 2),
        ("SCALED", "LIVE_RISK_SIZED", 1.00, 1.00, 2),
    ],
)
def test_each_stage_maps_to_its_exact_policy(
    monkeypatch, tmp_path, env_stage, fill_mode, per_order, total, concurrent
):
    mod = load_stage(monkeypatch, tmp_path, env_stage)
    policy = mod.execution_policy()

    assert policy.stage == config.Stage(env_stage)
    assert policy.fill_mode == fill_mode
    assert policy.max_lot_per_order == per_order
    assert policy.max_total_lots == total
    assert policy.max_concurrent_positions == concurrent
    assert mod.demotion_active() is False


def test_micro_is_the_tightest_rung_that_touches_real_money(monkeypatch, tmp_path):
    """Guards the table's intent, not just its numbers."""
    micro = load_stage(monkeypatch, tmp_path, "MICRO").execution_policy()
    scaled = load_stage(monkeypatch, tmp_path, "SCALED").execution_policy()

    assert micro.max_lot_per_order < scaled.max_lot_per_order
    assert micro.max_total_lots < scaled.max_total_lots
    # Live money never uses the modelled fill path.
    assert micro.fill_mode.startswith("LIVE_")
    assert scaled.fill_mode.startswith("LIVE_")


def test_execution_policy_is_frozen_and_forbids_extra_fields(monkeypatch, tmp_path):
    """INVARIANT 4: never weaken pydantic strictness."""
    mod = load_stage(monkeypatch, tmp_path, "PAPER")
    policy = mod.execution_policy()

    with pytest.raises(ValidationError):
        mod.ExecutionPolicy(
            stage=config.Stage.PAPER,
            fill_mode="MODELED",
            max_lot_per_order=0.10,
            max_total_lots=0.10,
            max_concurrent_positions=2,
            sneaky_override=999,
        )
    with pytest.raises(ValidationError):
        mod.ExecutionPolicy(
            stage=config.Stage.PAPER,
            fill_mode="NOT_A_REAL_FILL_MODE",
            max_lot_per_order=0.10,
            max_total_lots=0.10,
            max_concurrent_positions=2,
        )
    # A holder cannot edit a policy and pass it on as the governor's word.
    with pytest.raises(ValidationError):
        policy.max_lot_per_order = 99.0


# ---------------------------------------------------------------------------
# demotion
# ---------------------------------------------------------------------------


def test_demotion_flag_drops_shadow_to_paper(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="risk.stage")
    mod = load_stage(monkeypatch, tmp_path, "SHADOW", demoted_reason="drawdown breach")

    assert mod.env_stage() == config.Stage.SHADOW
    assert mod.effective_stage() == config.Stage.PAPER
    assert mod.demotion_active() is True
    assert "drawdown breach" in mod.demotion_reason()

    # The policy follows the EFFECTIVE stage, or demotion would be decorative.
    assert mod.execution_policy().stage == config.Stage.PAPER
    assert mod.execution_policy().fill_mode == "MODELED"

    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert criticals, "an active demotion must log CRITICAL at import"
    message = criticals[0].getMessage()
    assert "DEMOTION ACTIVE" in message
    assert "drawdown breach" in message


@pytest.mark.parametrize(
    "env_stage,expected",
    [("SCALED", "MICRO"), ("MICRO", "SHADOW"), ("SHADOW", "PAPER")],
)
def test_demotion_moves_exactly_one_rung(monkeypatch, tmp_path, env_stage, expected):
    mod = load_stage(monkeypatch, tmp_path, env_stage, demoted_reason="one rung")
    assert mod.effective_stage() == config.Stage(expected)


def test_demotion_floors_at_paper(monkeypatch, tmp_path):
    """PAPER is the bottom rung; a demotion there is a no-op, not an error."""
    mod = load_stage(monkeypatch, tmp_path, "PAPER", demoted_reason="already at the floor")

    assert mod.env_stage() == config.Stage.PAPER
    assert mod.effective_stage() == config.Stage.PAPER
    assert mod.demotion_active() is True
    assert mod.execution_policy().stage == config.Stage.PAPER


def test_absent_flag_means_no_demotion(monkeypatch, tmp_path):
    mod = load_stage(monkeypatch, tmp_path, "SCALED")
    assert mod.demotion_active() is False
    assert mod.demotion_reason() is None
    assert mod.effective_stage() == config.Stage.SCALED


def test_empty_flag_file_still_demotes(monkeypatch, tmp_path):
    """Presence is the signal; contents are only the explanation."""
    mod = load_stage(monkeypatch, tmp_path, "SHADOW", demoted_reason="")
    assert mod.demotion_active() is True
    assert mod.effective_stage() == config.Stage.PAPER
    assert "empty" in mod.demotion_reason()


def test_unreadable_flag_fails_safe_and_still_demotes(monkeypatch, tmp_path, caplog):
    """An unreadable explanation is not a reason to keep trading higher."""
    caplog.set_level(logging.INFO, logger="risk.stage")
    monkeypatch.setenv("NEXUS_STAGE", "MICRO")
    importlib.reload(config)

    # A directory where a file is expected: exists, but open() raises OSError.
    flag = tmp_path / "DEMOTED"
    flag.mkdir()
    monkeypatch.setattr(config, "DEMOTED_FLAG_PATH", str(flag))
    mod = importlib.reload(stage_mod)

    assert mod.demotion_active() is True
    assert mod.effective_stage() == config.Stage.SHADOW
    assert "unreadable" in mod.demotion_reason()


# ---------------------------------------------------------------------------
# write_demotion
# ---------------------------------------------------------------------------


def test_write_demotion_writes_reason_and_timestamp_atomically(monkeypatch, tmp_path):
    mod = load_stage(monkeypatch, tmp_path, "SHADOW")
    flag = tmp_path / "DEMOTED"
    assert not flag.exists()

    returned = mod.write_demotion("equity curve broke the floor")

    assert returned == str(flag)
    body = flag.read_text(encoding="utf-8")
    assert "equity curve broke the floor" in body
    # An ISO-8601 UTC timestamp, parseable rather than merely present.
    from datetime import datetime

    stamp = body.split()[0]
    assert datetime.fromisoformat(stamp).tzinfo is not None

    # Atomic: no temp file survives the write.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".DEMOTED.")]
    assert leftovers == []


def test_write_demotion_does_not_change_the_running_process(monkeypatch, tmp_path):
    """INVARIANT 1: demotion takes effect on restart, never mid-flight."""
    mod = load_stage(monkeypatch, tmp_path, "SHADOW")
    before = mod.execution_policy()
    assert mod.demotion_active() is False

    mod.write_demotion("should not apply until restart")

    assert mod.demotion_active() is False
    assert mod.effective_stage() == config.Stage.SHADOW
    assert mod.execution_policy() == before

    # ...but the next boot picks it up.
    rebooted = importlib.reload(stage_mod)
    assert rebooted.demotion_active() is True
    assert rebooted.effective_stage() == config.Stage.PAPER


def test_write_demotion_collapses_a_multiline_reason(monkeypatch, tmp_path):
    """The flag must stay one greppable line so boot can log it verbatim."""
    mod = load_stage(monkeypatch, tmp_path, "SHADOW")
    mod.write_demotion("line one\nline two\n\tindented")

    body = (tmp_path / "DEMOTED").read_text(encoding="utf-8")
    assert body.count("\n") == 1
    assert "line one line two indented" in body


def test_write_demotion_overwrites_an_existing_flag(monkeypatch, tmp_path):
    mod = load_stage(monkeypatch, tmp_path, "SHADOW", demoted_reason="old reason")
    mod.write_demotion("new reason")

    body = (tmp_path / "DEMOTED").read_text(encoding="utf-8")
    assert "new reason" in body
    assert "old reason" not in body


# ---------------------------------------------------------------------------
# no mutation path (INVARIANT 1)
# ---------------------------------------------------------------------------


def test_no_function_rebinds_the_import_time_decisions(monkeypatch, tmp_path):
    """
    Rebinding a module global from inside a function requires a `global`
    declaration, so an AST sweep is a complete answer, not a heuristic.
    """
    mod = load_stage(monkeypatch, tmp_path, "SHADOW")
    assert mod._writes_effective_stage() == []


def test_module_exposes_no_stage_or_policy_setter(monkeypatch, tmp_path):
    mod = load_stage(monkeypatch, tmp_path, "SHADOW")

    for banned in ("set_stage", "set_policy", "set_effective_stage", "clear_demotion"):
        assert not hasattr(mod, banned), f"{banned} must not exist"

    setters = [
        name
        for name in dir(mod)
        if not name.startswith("_")
        and callable(getattr(mod, name))
        and name.lower().startswith("set")
    ]
    assert setters == []


def test_effective_stage_survives_env_changes_without_a_reload(monkeypatch, tmp_path):
    """A running process cannot change rung, however the environment moves."""
    mod = load_stage(monkeypatch, tmp_path, "SHADOW")
    assert mod.effective_stage() == config.Stage.SHADOW

    monkeypatch.setenv("NEXUS_STAGE", "SCALED")

    assert mod.effective_stage() == config.Stage.SHADOW
    assert mod.execution_policy().stage == config.Stage.SHADOW


# ---------------------------------------------------------------------------
# audit trail
# ---------------------------------------------------------------------------


def test_boot_row_lands_in_stage_events(monkeypatch, tmp_path):
    mod = load_stage(monkeypatch, tmp_path, "SCALED", demoted_reason="audit probe")

    rows = database.fetch(
        "SELECT event, env_stage, effective_stage, reason "
        "FROM stage_events ORDER BY id DESC LIMIT 1"
    )
    assert rows, "import must append a BOOT row"
    event, env_stage, effective_stage, reason = rows[0]
    assert event == "BOOT"
    assert env_stage == "SCALED"
    assert effective_stage == "MICRO"
    assert "audit probe" in reason
    assert mod.effective_stage() == config.Stage.MICRO


def test_write_demotion_records_its_own_row(monkeypatch, tmp_path):
    mod = load_stage(monkeypatch, tmp_path, "SHADOW")
    mod.write_demotion("recorded reason")

    rows = database.fetch(
        "SELECT event, env_stage, effective_stage, reason "
        "FROM stage_events ORDER BY id DESC LIMIT 1"
    )
    event, env_stage, effective_stage, reason = rows[0]
    assert event == "DEMOTION_WRITTEN"
    assert env_stage == "SHADOW"
    # The row records the stage the process is STILL running at.
    assert effective_stage == "SHADOW"
    assert reason == "recorded reason"


def test_import_succeeds_when_the_database_is_down(monkeypatch, tmp_path, caplog):
    """INVARIANT 6: a dead database costs the audit trail, never the boot."""
    caplog.set_level(logging.INFO, logger="risk.stage")

    def unavailable(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(database, "execute", unavailable)
    mod = load_stage(monkeypatch, tmp_path, "MICRO", demoted_reason="db is down")

    # The governor still decided correctly without the database.
    assert mod.effective_stage() == config.Stage.SHADOW
    assert mod.execution_policy().fill_mode == "SHADOW_REAL_BIDASK"
    assert any("could not record BOOT" in r.getMessage() for r in caplog.records)


def test_write_demotion_still_writes_the_flag_when_the_database_is_down(
    monkeypatch, tmp_path
):
    """The flag is the mechanism; the row is only the audit trail."""
    mod = load_stage(monkeypatch, tmp_path, "SHADOW")

    def unavailable(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(database, "execute", unavailable)
    mod.write_demotion("db down but flag must land")

    assert (tmp_path / "DEMOTED").exists()
    assert "db down but flag must land" in (tmp_path / "DEMOTED").read_text(encoding="utf-8")
