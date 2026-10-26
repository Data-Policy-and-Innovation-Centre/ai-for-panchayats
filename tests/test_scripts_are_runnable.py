"""Every script must run the way a person runs it: `python scripts/<name>.py`.

This cannot be an import test. `pytest.ini` sets `pythonpath = ["src", "."]`,
so under pytest every one of these imports resolves whether or not the script
bootstraps its own path -- which is exactly how `build_snapshot_manifest.py`
came to be unrunnable while a full green suite said otherwise. The only test
that can see the defect is one that leaves the interpreter.

`--help` is the whole check on purpose. Argparse builds and exits before any
side effect, so this exercises the import block and nothing else, and it needs
no AWS, no database and no fixture.

`build_snapshot_manifest.py` is the reason this file exists. It is the single
place an artifact becomes deployable, and it exited 1 either way: `1` because
the guard refused a sample, and `1` because the module failed to import. A
person confirming "it refuses my sample" saw the right exit code from a script
that had not run at all.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Scripts that import from `src/` or `warehouse/` and so need the repository
# root on sys.path. Scripts with no first-party imports cannot have this
# defect and are not listed; `test_the_script_list_is_complete` proves the
# list is still right.
SCRIPTS = (
    "benchmark_normalize.py",
    "build_snapshot_manifest.py",
    "build_warehouse.py",
    "check_warehouse_conformance.py",
    "fetch_snapshot.py",
)

# Scripts that import first-party code and are deliberately NOT covered, each
# for a reason that is not the defect this file is about. Named rather than
# omitted, so `test_the_script_list_is_complete` still fails on a genuinely
# new script instead of being weakened to pass.
#
#   run_egram_scraper.py     `--help` does not return -- it does real work at
#                            import, so the probe below would hang rather than
#                            check anything. Its own bug, and it is the
#                            scraper this project deliberately never runs.
#   run_panchayat_nirnay.py  fails on `import config`, a top-level package
#                            that is not on the path either way. A different
#                            missing path, not the repo root.
#   run_sabhasaar.py         both of the above: `from config import
#                            directories`, and no argparse at all, so there is
#                            no `--help` to probe. Adding the repo root alone
#                            does not make it runnable.
UNCOVERED = (
    "run_egram_scraper.py",
    "run_panchayat_nirnay.py",
    "run_sabhasaar.py",
)


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_runs_as_a_script(script: str):
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), "--help"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120,
    )
    assert "ModuleNotFoundError" not in result.stderr, (
        f"scripts/{script} cannot be run directly:\n{result.stderr}"
    )
    assert result.returncode == 0, f"scripts/{script} --help exited {result.returncode}"


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_runs_as_a_module(script: str):
    """The form entrypoint.sh uses (`python -m scripts.fetch_snapshot`).

    Adding a `sys.path` bootstrap must not break it: appending a path that is
    already there is harmless, but asserting it beats assuming it, since
    production startup depends on this form.
    """

    result = subprocess.run(
        [sys.executable, "-m", f"scripts.{script.removesuffix('.py')}", "--help"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120,
    )
    assert result.returncode == 0, f"{script} as a module exited {result.returncode}: {result.stderr}"


def test_the_script_list_is_complete():
    """A new script importing `src/` must be added above, or this fails.

    Without this the list silently goes stale and the next script to grow a
    first-party import is unprotected.
    """

    needs_bootstrap = {
        path.name
        for path in sorted((REPO_ROOT / "scripts").glob("*.py"))
        if path.name != "__init__.py"
        and any(
            line.startswith(("from src", "import src", "from warehouse", "import warehouse"))
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    }
    missing = needs_bootstrap - set(SCRIPTS) - set(UNCOVERED)
    assert not missing, (
        f"these scripts import first-party code but are not covered: {sorted(missing)}"
    )
