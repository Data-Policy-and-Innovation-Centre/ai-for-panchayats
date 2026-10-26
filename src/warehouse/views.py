"""The consumer-facing relations, materialised as tables rather than views.

The chatbot never queries the fact tables directly. It queries seven
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
skip creating a relation whose name already exists -- as a table *or* a
view -- or its in-memory definition shadows what was built here and nothing
gets faster. Note "or a view": ``v_activity`` is deliberately left as a view
over the stored ``v_activity_base`` (see below), so a check that looked only
for tables would replace it with the consumer's own full-join version and
give up the materialisation entirely.
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


# Which relations are stored, and which are left as views. `ensure_views()`
# skips any name that already exists, so a subset is coherent: the stored ones
# shadow the consumer's definition, the rest behave exactly as they do today.
#
# Set by measurement, not taste -- see the size/latency table in #51. Override
# with the `relations` argument to compare variants.
#
# `v_activity_base` rather than `v_activity` is the one name here that differs
# from the consumer's, and it is deliberate. `v_activity` adds
# `days_since_sanction`, which counts days up to CURRENT_DATE; storing it would
# freeze that count at build time and the answer would drift every day the
# snapshot stayed deployed. The base carries the whole join graph, so the view
# over it costs one scalar per row. See views.sql.
STORED_BY_DEFAULT: tuple[str, ...] = (
    "v_exp", "v_approval", "v_activity_base", "v_asset", "v_plan",
    "v_progress", "v_voucher",
)


def materialize(
    con: duckdb.DuckDBPyConnection, relations: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Build every v_* relation as a table, in dependency order.

    Returns row counts per relation. Executed as one script rather than
    statement by statement: DuckDB runs multi-statement SQL, and splitting it
    here would mean writing a SQL parser to handle the CTEs and semicolons
    inside these definitions.
    """

    stored = set(STORED_BY_DEFAULT if relations is None else relations)
    unknown = stored - set(relation_names())
    if unknown:
        raise ViewDefinitionError(f"not defined in {VIEW_DDL_PATH.name}: {sorted(unknown)}")

    # The only edit made to the vendored DDL, so that what runs stays
    # diffable against the consumer's copy. A relation that is not stored is
    # still created -- as a view -- because the stored ones are defined in
    # terms of it; DuckDB persists the view definition, and the consumer's
    # ensure_views() leaves it alone.
    def rewrite(match: re.Match[str]) -> str:
        name = match.group(1)
        return f"CREATE TABLE {name} AS" if name in stored else f"CREATE VIEW {name} AS"

    con.execute(_CREATE_VIEW.sub(rewrite, _ddl()))
    return {
        name: con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]  # noqa: S608
        for name in relation_names() if name in stored
    }
