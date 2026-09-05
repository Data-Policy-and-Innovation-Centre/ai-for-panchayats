"""The sharding probe refuses a count that would multiply its own work.

Run as a subprocess with no inherited ``PYTHONPATH``, exactly as the
docstring documents the script being run. That detail is the point: under
pytest, ``pythonpath = ["src", "."]`` in pyproject.toml puts the repository
root on the path, so the script's ``from scripts.benchmark_normalize import
_positive`` resolves here whether or not the script arranges for it -- while
``uv run python scripts/probe_normalize_sharding.py`` puts only ``scripts/``
on the path and dies with ModuleNotFoundError. Importing the module directly
would pass against a script that cannot actually be run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_normalize_sharding.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=_SCRIPT.parents[1], env=env, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("bad", ["--gps=-1", "--gps=0", "--shards=0", "--shards=-3"])
def test_a_nonpositive_count_fails_before_any_work(bad: str):
    """Both counts multiply what this probe copies and normalizes, and both
    are expensive to get wrong. ``--gps -1`` slices to every folder but the
    last, so a typo meant to shrink the run normalizes nearly the whole tree
    twice; ``--shards 0`` completes the full unsharded pass before
    ProcessPoolExecutor rejects ``max_workers=0``. Argparse rejects both
    before the script reaches either.
    """

    result = _run("--tree", "/nonexistent", "--work", "/nonexistent", bad)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "must be at least 1" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
