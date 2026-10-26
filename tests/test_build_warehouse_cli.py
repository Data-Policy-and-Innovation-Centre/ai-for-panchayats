"""Guard against the warehouse CLI entry point regressing to unimportable.

``scripts/build_warehouse.py`` is documented to run as
``uv run python scripts/build_warehouse.py ...``. Run that way, Python puts
``scripts/`` on ``sys.path`` -- not the repository root -- so the script
must add the repository root itself before importing anything that reaches
``src.pipeline`` (``warehouse.select`` does, transitively). Under pytest this
is invisible: ``pythonpath = ["src", "."]`` in pyproject.toml already puts
both roots on the path, so importing the module directly here would not
catch the regression. This test instead runs the documented command in a
real subprocess with no inherited ``PYTHONPATH``, exactly as a user would.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import build_warehouse
from src.warehouse.dimensions import DimensionError
from src.warehouse.geography import GeographyError
from src.warehouse.select import SelectionError

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "build_warehouse.py"), *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_help_does_not_fail_to_import():
    result = _run_cli("--help")
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_cli_reaches_business_logic_not_an_import_error():
    """An unknown snapshot id must fail on the domain check inside
    ``warehouse.select``, not on the import of ``src.pipeline`` -- proof the
    CLI got past module loading and into real code, run the documented way.
    """

    result = _run_cli("build", "--snapshot-id", "does-not-exist")
    assert "ModuleNotFoundError" not in result.stderr
    assert "unknown approved snapshot" in result.stderr


def test_cli_unknown_snapshot_id_exits_controlled_not_traceback():
    """SnapshotRegistry.get() raises SnapshotRegistryError, uncaught before
    the fix -- reaching the CLI as a traceback and exit 1 instead of the
    controlled exit 2 every other invalid-selection case already used.
    """

    result = _run_cli("build", "--snapshot-id", "does-not-exist")
    assert result.returncode == 2
    assert "unknown approved snapshot: does-not-exist" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("error", "why"),
    [
        (SelectionError, "a bad snapshot selection"),
        (GeographyError, "an LGD reference tree that is missing or malformed"),
        (DimensionError, "a code dictionary that is missing or malformed"),
    ],
)
def test_a_preflight_failure_exits_two_without_a_traceback(monkeypatch, error, why):
    """The three ways a build can refuse to start must agree.

    Each is raised before any DuckDB file is touched, so each should report
    its own diagnostic and exit 2. `DimensionError` was the odd one out --
    the loader was added without being added here, so a missing or malformed
    dimension CSV reached the documented
    `uv run python scripts/build_warehouse.py build ...` as a traceback and
    exit 1. Parametrized rather than written once for the new case, because
    the defect was a divergence between siblings, and only a test over all
    three catches the next one.
    """

    def refuse(**kwargs):
        raise error(f"cannot start: {why}")

    monkeypatch.setattr(build_warehouse, "build", refuse)
    args = argparse.Namespace(
        snapshot_id=["any"], database=None, no_validate=True,
    )
    assert build_warehouse.cmd_build(args) == 2
