"""The consumer-facing relations, materialised as tables rather than views.

The chatbot never queries the 19 spec tables directly. It queries seven
``v_*`` relations, and ``v_activity`` alone is referenced ~324 times in its
template catalogue (#51). Those definitions lived only in the consumer repo,
so a warehouse built here was not consumable at all.

**They are built as tables, not views, and that is the point.** The consumer
creates them in a writable in-memory catalogue because the snapshot is
attached read-only, so nothing is ever saved: every question re-runs the
whole join graph. ``scripts/profile_warehouse_queries.py`` measured what that
costs on the full state (#99, #98):

    nine probes, views          28,564 ms
    nine probes, materialised      791 ms

A 36x difference, against a CloudFront limit that cuts any request off at 60
seconds. Paying the joins once at build time is the fix.

The DDL is vendored verbatim at ``views.sql`` and rewritten to ``CREATE
TABLE`` on the way in -- verbatim so that a diff against the consumer's
``Ask/sql/create_views.sql`` is a real diff, and any divergence is visible
rather than reconstructed. The file's own order is dependency order
(``v_exp`` and ``v_approval`` feed ``v_activity``; ``v_asset`` and
``v_progress`` read it), so it is executed as written.

**The consumer needs one change to benefit.** Its ``ensure_views()`` must
skip creating a view when a table of that name already exists, or the
in-memory view shadows the stored table and nothing gets faster.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import duckdb

VIEW_DDL_PATH = Path(__file__).resolve().parent / "views.sql"

# `CREATE OR REPLACE VIEW <name> AS`, the only statement form in views.sql.
_CREATE_VIEW = re.compile(r"CREATE\s+OR\s+REPLACE\s+VIEW\s+(\w+)\s+AS", re.IGNORECASE)


class ViewDefinitionError(RuntimeError):
    """Raised when the vendored view DDL is missing or unrecognisable."""


@lru_cache(maxsize=1)
def _ddl() -> str:
    try:
        return VIEW_DDL_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ViewDefinitionError(f"view DDL unreadable: {VIEW_DDL_PATH}: {exc}") from exc


def relation_names() -> tuple[str, ...]:
    """The v_* relations, in the order the DDL defines them."""

    names = tuple(match.group(1) for match in _CREATE_VIEW.finditer(_ddl()))
    if not names:
        raise ViewDefinitionError(f"{VIEW_DDL_PATH}: no CREATE OR REPLACE VIEW statements")
    return names


def materialize(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Build every v_* relation as a table, in dependency order.

    Returns row counts per relation. Executed as one script rather than
    statement by statement: DuckDB runs multi-statement SQL, and splitting it
    here would mean writing a SQL parser to handle the CTEs and semicolons
    inside these definitions.
    """

    names = relation_names()
    # The only edit made to the vendored DDL, so that what runs stays
    # diffable against the consumer's copy.
    con.execute(_CREATE_VIEW.sub(lambda m: f"CREATE TABLE {m.group(1)} AS", _ddl()))
    return {
        name: con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]  # noqa: S608
        for name in names
    }
