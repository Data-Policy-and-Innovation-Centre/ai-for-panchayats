"""Panchayat analytical read model, built from canonical Parquet snapshots.

A disposable DuckDB database rebuilt from approved, hash-verified canonical
snapshots. Nothing here writes back to a government portal or to the
canonical Parquet tree; both remain read-only inputs.

    from warehouse.build import build
    build(snapshot_ids=("egramswaraj-2026-08",))   # rebuild, publish if valid

Layout:
    config.py    input locations, all environment-overridable, no filesystem IO
    schema.py    every table, key and constraint, in one place
    select.py    preflight: resolve and re-validate the chosen snapshots
    clean.py     shared, pure cleaning conventions (decimal money included)
    transform.py pure frame-to-frame shaping, plus reason-coded quarantine
    load.py      canonical Parquet reads and batched DuckDB inserts
    build.py     transactional rebuild and atomic publication
    validate.py  executable checks against the built warehouse
"""

from __future__ import annotations

__all__ = ["build", "validate_database"]


def __getattr__(name: str):
    # Imported lazily so `import warehouse` stays cheap.
    if name == "build":
        from .build import build
        return build
    if name == "validate_database":
        from .validate import run_checks
        return run_checks
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
