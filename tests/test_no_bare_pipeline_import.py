"""Guard against the bare `pipeline` import defect re-appearing.

`main.py` imports the pipeline package as `src.pipeline` (the repo root is on
sys.path there). Three adapters instead imported it as bare `pipeline`. Both
spellings resolve to the same file on disk, but Python treats them as two
distinct modules -- so `RunPublisher`, `ManifestError`, and every other name
in the package exist twice, as two unrelated objects. An `except
src.pipeline.ManifestError` clause silently fails to catch an exception
raised as `pipeline.ManifestError`; an `isinstance` check across the
boundary fails the same way. The defect is invisible under pytest because
`pythonpath = ["src", "."]` satisfies both spellings at once, and only
surfaces at real CLI integration (`uv run python main.py`).

This test scans the project's own source tree on disk -- it does not rely on
sys.path or on importing anything -- because sys.path is exactly what hides
the problem the rest of the time.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCAN_TARGETS = ("src", "scripts", "tests", "main.py", "config.py")
_SELF = Path(__file__).resolve()

# Matches a top-level `from pipeline ...` or `import pipeline ...` statement,
# but not `from src.pipeline ...` / `import src.pipeline ...` (the "from "
# / "import " token is immediately followed by "src." there, not "pipeline").
_BARE_PIPELINE_IMPORT = re.compile(r"^\s*(from pipeline\b|import pipeline\b)")


def _project_python_files():
    for target in _SCAN_TARGETS:
        path = _PROJECT_ROOT / target
        if path.is_file() and path.suffix == ".py":
            yield path
        elif path.is_dir():
            yield from path.rglob("*.py")


def test_no_bare_pipeline_import_anywhere_in_the_project():
    offenders: list[str] = []
    for path in _project_python_files():
        if path.resolve() == _SELF:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _BARE_PIPELINE_IMPORT.match(line):
                offenders.append(f"{path.relative_to(_PROJECT_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Bare `pipeline` import found. This package must always be imported "
        "as `src.pipeline`, matching main.py -- a bare `pipeline` import "
        "resolves to the same file but creates a second, distinct module "
        "object (and thus distinct classes/exceptions) whenever the repo "
        "root and `src` both sit on sys.path, e.g. outside pytest. "
        "Offending line(s):\n" + "\n".join(offenders)
    )
