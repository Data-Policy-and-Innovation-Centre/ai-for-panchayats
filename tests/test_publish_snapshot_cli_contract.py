"""publish_snapshot.py shells out to build_snapshot_manifest.py; the flags must agree.

This is not hypothetical: the caller passed `--output` where the callee declares
`--out`. argparse exits 2, and the call happens AFTER the multi-gigabyte upload
-- so the failure costs the whole transfer and produces no manifest. Nothing in
the type system or the test suite connected the two, because one is a string
inside a subprocess list.

Parsing the caller's argv with the callee's OWN parser is the check.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _literal_flags(script: str, callee: str) -> list[str]:
    """Every string literal in the subprocess argv that names `callee`."""
    tree = ast.parse((ROOT / "scripts" / script).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        parts = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if any(callee in p for p in parts):
            return [p for p in parts if p.startswith("--")]
    raise AssertionError(f"no subprocess argv naming {callee} found in {script}")


def _declared_flags(script: str) -> set[str]:
    """Every long option the script declares via parser.add_argument."""
    tree = ast.parse((ROOT / "scripts" / script).read_text())
    flags: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.startswith("--"):
                        flags.add(arg.value)
    assert flags, f"{script} declares no long options -- the parser was not found"
    return flags


def test_every_flag_publish_passes_is_declared_by_the_manifest_builder() -> None:
    declared = _declared_flags("build_snapshot_manifest.py")
    passed = _literal_flags("publish_snapshot.py", "build_snapshot_manifest.py")
    assert passed, "no flags extracted from the subprocess argv"
    undeclared = [f for f in passed if f not in declared]
    assert not undeclared, (
        f"publish_snapshot.py passes {undeclared}, which build_snapshot_manifest.py does not "
        f"declare. argparse exits 2 -- and this subprocess runs AFTER the multi-gigabyte "
        f"upload, so the transfer is paid for and no manifest is written. "
        f"Declared: {sorted(declared)}"
    )
