"""
Stage governor — turns the stage ladder from a logged label into machinery.

The ladder is PAPER -> SHADOW -> MICRO -> SCALED, in that order, and each rung
carries an ExecutionPolicy: how fills are produced and how much size may exist.
Task 16's order router consumes execution_policy(); nothing consumes it yet.

INVARIANT 1 ("STAGE read once at boot, no runtime mutation path may exist")
governs this module completely. Everything that decides behaviour is computed
ONCE, at import, and is thereafter read-only:

    env stage        <- config.get_stage(), itself read once from NEXUS_STAGE
    demotion flag    <- the presence of config.DEMOTED_FLAG_PATH on disk
    effective stage  <- env stage, minus one rung if demoted, floored at PAPER

There is deliberately no setter, no reload hook, and no function anywhere in
this module that writes _EFFECTIVE_STAGE. A process cannot change rung while
it runs; that is the entire point. Tests reach the other stages the only way
anything can — a fresh interpreter, or an explicit importlib.reload().

CLEARING A DEMOTION IS A HUMAN ACT.
There is no clear_demotion() and there will not be one. To leave a demoted
state an operator must:

    1. delete the flag file (default ./DEMOTED), and
    2. restart the process.

An automatic un-demote would let a system that just demoted itself for losing
money promote itself back without anyone looking at it. write_demotion() can
only ever move DOWN the ladder, and even that does not take effect until the
next boot — the running process keeps the policy it started with.

WHY A FILE, NOT A DATABASE ROW: config never lives in the database (see
core/database.py). A file also survives a crash, needs no connection to read
during boot, and is obvious to an operator with `ls`. The stage_events table
is an audit trail of what happened, never an input to what happens.
"""
import ast
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel

import config
from config import Stage, get_stage
from core import database

logger = logging.getLogger(__name__)


class ExecutionPolicy(BaseModel, extra="forbid"):
    """
    What the current rung permits. Frozen so a holder cannot edit a policy and
    hand it on as if the governor had issued it (INVARIANT 4: never weaken
    pydantic strictness). extra="forbid" means a typo'd or renamed config key
    fails loudly at boot rather than silently dropping a cap.
    """

    model_config = {"extra": "forbid", "frozen": True}

    stage: Stage
    fill_mode: Literal[
        "MODELED", "SHADOW_REAL_BIDASK", "LIVE_FIXED_MICRO", "LIVE_RISK_SIZED"
    ]
    max_lot_per_order: float
    max_total_lots: float
    max_concurrent_positions: int


# Ladder order is the Stage enum's declaration order — PAPER, SHADOW, MICRO,
# SCALED. Derived rather than restated so the two can never drift apart.
_LADDER = list(Stage)


def _one_rung_below(stage: Stage) -> Stage:
    """One step down the ladder, floored at PAPER (the safest rung)."""
    return _LADDER[max(0, _LADDER.index(stage) - 1)]


def _read_demotion_flag() -> Optional[str]:
    """
    Return the flag file's contents, or None if there is no flag.

    Fails SAFE: a flag file that exists but cannot be read still counts as a
    demotion. The file's presence is the signal; its contents are only the
    explanation, and an unreadable explanation is not a reason to keep trading
    at the higher rung.
    """
    path = config.DEMOTED_FLAG_PATH
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip() or "<demotion flag present but empty>"
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.error("stage: demotion flag at %s exists but is unreadable: %s", path, exc)
        return f"<demotion flag unreadable: {exc}>"


# --------------------------------------------------------------------------
# Import-time decisions. This block runs exactly once per process and is the
# only place these names are ever assigned.
# --------------------------------------------------------------------------

_ENV_STAGE: Stage = get_stage()
_DEMOTION_REASON: Optional[str] = _read_demotion_flag()
_DEMOTION_ACTIVE: bool = _DEMOTION_REASON is not None
_EFFECTIVE_STAGE: Stage = (
    _one_rung_below(_ENV_STAGE) if _DEMOTION_ACTIVE else _ENV_STAGE
)


def env_stage() -> Stage:
    """The stage the environment asked for, before any demotion."""
    return _ENV_STAGE


def effective_stage() -> Stage:
    """The stage this process actually adopted. Fixed for the process lifetime."""
    return _EFFECTIVE_STAGE


def demotion_active() -> bool:
    """True if a demotion flag was present at boot."""
    return _DEMOTION_ACTIVE


def demotion_reason() -> Optional[str]:
    """The flag file's recorded reason, or None when not demoted."""
    return _DEMOTION_REASON


def execution_policy() -> ExecutionPolicy:
    """
    The policy for the EFFECTIVE stage — a pure function of import-time state.

    Effective, not env: a demotion that did not change the policy would be
    decorative. Callers get the rung the process is actually running at.
    """
    spec = config.STAGE_POLICY[_EFFECTIVE_STAGE.value]
    return ExecutionPolicy(stage=_EFFECTIVE_STAGE, **spec)


def _record_event(event: str, reason: Optional[str] = None) -> bool:
    """
    Append an audit row. Best-effort by design (INVARIANT 6): the governor's
    decisions are already made from the environment and the filesystem, so a
    database that is down costs us the audit trail, never the boot.
    """
    try:
        database.execute(
            "INSERT INTO stage_events (event, env_stage, effective_stage, reason) "
            "VALUES (%s, %s, %s, %s)",
            (event, _ENV_STAGE.value, _EFFECTIVE_STAGE.value, reason),
        )
        return True
    except Exception:
        logger.warning(
            "stage: could not record %s audit row; continuing without it", event, exc_info=True
        )
        return False


def write_demotion(reason: str) -> str:
    """
    Create the demotion flag so the NEXT boot starts one rung lower.

    This does NOT change the running process: no caller of execution_policy()
    sees a different answer afterwards. Demotion takes effect on restart, which
    keeps INVARIANT 1 intact and makes the change reviewable before it applies.

    Written atomically (temp file in the same directory + os.replace) so a
    crash mid-write can never leave a half-written flag that a later boot would
    read as a corrupt reason. Returns the flag path.
    """
    path = config.DEMOTED_FLAG_PATH
    directory = os.path.dirname(os.path.abspath(path)) or "."
    timestamp = datetime.now(timezone.utc).isoformat()
    # Collapse whitespace so the flag stays a single greppable line.
    payload = f"{timestamp} {' '.join(str(reason).split())}\n"

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".DEMOTED.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Never leave the temp file behind for a later boot to trip over.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logger.critical(
        "stage: DEMOTION FLAG WRITTEN to %s (%s) — takes effect on next restart; "
        "clearing it requires a human to delete the file",
        path,
        reason,
    )
    _record_event("DEMOTION_WRITTEN", reason=reason)
    return path


def _writes_effective_stage() -> list:
    """
    Self-audit used by the test suite: return the names of any functions in
    this module that rebind an import-time decision. Rebinding a module global
    from inside a function requires a `global` declaration, so the AST is a
    complete and honest answer. Must always be empty — see INVARIANT 1.
    """
    guarded = {"_EFFECTIVE_STAGE", "_ENV_STAGE", "_DEMOTION_ACTIVE", "_DEMOTION_REASON"}
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Global) and guarded.intersection(inner.names):
                offenders.append(node.name)
    return offenders


def _log_boot_decision() -> None:
    """Every policy decision, logged once, at import."""
    if _DEMOTION_ACTIVE:
        logger.critical(
            "stage: DEMOTION ACTIVE — env stage %s demoted to %s. Reason: %s. "
            "Delete %s and restart to clear.",
            _ENV_STAGE.value,
            _EFFECTIVE_STAGE.value,
            _DEMOTION_REASON,
            config.DEMOTED_FLAG_PATH,
        )

    policy = execution_policy()
    logger.info(
        "stage: env=%s effective=%s demoted=%s | fill_mode=%s max_lot_per_order=%s "
        "max_total_lots=%s max_concurrent_positions=%s",
        _ENV_STAGE.value,
        _EFFECTIVE_STAGE.value,
        _DEMOTION_ACTIVE,
        policy.fill_mode,
        policy.max_lot_per_order,
        policy.max_total_lots,
        policy.max_concurrent_positions,
    )


_log_boot_decision()
_record_event("BOOT", reason=_DEMOTION_REASON)
