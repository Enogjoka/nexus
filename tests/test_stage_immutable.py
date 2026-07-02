"""
Acceptance tests for Task 0, invariant #1:
STAGE is read once at boot and no runtime mutation path may exist.
"""
import importlib
import re
from pathlib import Path

import pytest

import config

REPO_ROOT = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()
FORBIDDEN_PATTERN = re.compile(r"set_paper_mode|paper_mode|set_stage\(")


@pytest.fixture(autouse=True)
def _reset_config_module(monkeypatch):
    """Leave config in its default (env-unset) state for the next test."""
    yield
    monkeypatch.delenv("NEXUS_STAGE", raising=False)
    importlib.reload(config)


def test_get_stage_defaults_to_paper_when_env_unset(monkeypatch):
    monkeypatch.delenv("NEXUS_STAGE", raising=False)
    importlib.reload(config)
    assert config.get_stage() == config.Stage.PAPER


def test_get_stage_reads_shadow_from_env(monkeypatch):
    monkeypatch.setenv("NEXUS_STAGE", "SHADOW")
    importlib.reload(config)
    assert config.get_stage() == config.Stage.SHADOW


def test_no_public_mutation_api_exists_for_stage():
    # No setter of any kind, by any plausible name.
    assert not hasattr(config, "set_stage")
    assert not hasattr(config, "set_paper_mode")
    assert not hasattr(config, "paper_mode")

    # Broader sweep: no public callable in config.py starts with "set".
    mutators = [
        name
        for name in dir(config)
        if not name.startswith("_")
        and name != "get_stage"
        and callable(getattr(config, name))
        and name.lower().startswith("set")
    ]
    assert mutators == []


def test_repo_wide_no_stage_mutation_patterns_outside_this_file():
    violations = []
    for path in REPO_ROOT.rglob("*.py"):
        resolved = path.resolve()
        if resolved == THIS_FILE:
            continue
        if any(part in ("venv", "__pycache__", ".git") for part in resolved.parts):
            continue
        text = resolved.read_text(encoding="utf-8", errors="ignore")
        if FORBIDDEN_PATTERN.search(text):
            violations.append(str(resolved.relative_to(REPO_ROOT)))
    assert violations == [], f"Forbidden stage-mutation patterns found in: {violations}"
