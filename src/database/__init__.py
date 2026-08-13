"""Panchayat analytical read model.

A disposable DuckDB database rebuilt from versioned source extracts. Nothing
here writes back to a government portal; the portals remain the system of
record.

    from database.build import build
    build()                       # rebuild and publish, if it validates

Layout:
    config.py     input locations, all environment-overridable
    schema.py     every table, key and constraint, in one place
    clean.py      shared cleaning conventions
    transform.py  pure frame-to-frame shaping, plus quarantine
    build.py      transactional rebuild and atomic publication
    validate.py   executable checks against a versioned manifest
"""

from __future__ import annotations

__all__ = ["build", "validate_database", "table_counts"]


def __getattr__(name: str):
    # Imported lazily so `import database` stays cheap for notebooks.
    if name == "build":
        from .build import build
        return build
    if name == "table_counts":
        from .build import table_counts
        return table_counts
    if name == "validate_database":
        from .validate import validate_database
        return validate_database
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
