"""Post-load validation gate for a built warehouse.

DuckDB's own ``PRIMARY KEY``/``FOREIGN KEY`` constraints already reject a
violating row at ``INSERT`` time; the checks here re-assert the same
invariants as executable, reportable assertions (following PR #9's
``origin/Abhigyan_database:src/database/validate.py``), so a schema edit that
quietly drops a constraint is still caught, and so a build failure explains
itself instead of surfacing a raw DuckDB constraint-violation message.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from .schema import FACT_TABLES, NO_LINEAGE_TABLES
from .geography import GEOGRAPHY_COLUMNS, gp_geography
from .select import SelectedSnapshot


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str


class ValidationFailed(RuntimeError):
    def __init__(self, failures: list[Check]) -> None:
        lines = "\n".join(f"  - {c.name}: {c.detail}" for c in failures)
        super().__init__(f"{len(failures)} validation check(s) failed:\n{lines}")
        self.failures = failures


def _scalar(con: duckdb.DuckDBPyConnection, sql: str):
    return con.execute(sql).fetchone()[0]


PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "gram_panchayat": ("gp_lgd_code",),
    "plan": ("plan_code",),
    "planned_activity": ("activity_code",),
    "activity_delegation": ("activity_code",),
    "activity_training": ("activity_code",),
    "activity_community_service": ("activity_code",),
    "activity_nsap": ("nsap_id",),
    "activity_asset": ("activity_code",),
    "activity_fund": ("activity_code",),
    "admin_approval": ("row_id",),
    "admin_approval_scheme": ("row_id",),
    "technical_approval": ("row_id",),
    "physical_progress": ("row_id",),
    "activity_expenditure": ("expenditure_id",),
    "voucher": ("voucher_pk",),
    "dim_code": ("variable", "code"),
    "dim_welfare_scheme": ("scheme_code",),
    # activity_voucher and dim_lsdg_theme are deliberately absent: neither
    # has a declared primary key (see schema.py's comments on each).
}

FOREIGN_KEYS: list[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = [
    ("plan", ("gp_lgd_code",), "gram_panchayat", ("gp_lgd_code",)),
    ("planned_activity", ("plan_code",), "plan", ("plan_code",)),
    ("planned_activity", ("gp_lgd_code",), "gram_panchayat", ("gp_lgd_code",)),
    ("activity_delegation", ("activity_code",), "planned_activity", ("activity_code",)),
    ("activity_training", ("activity_code",), "planned_activity", ("activity_code",)),
    ("activity_community_service", ("activity_code",), "planned_activity", ("activity_code",)),
    ("activity_nsap", ("activity_code",), "planned_activity", ("activity_code",)),
    ("activity_asset", ("activity_code",), "planned_activity", ("activity_code",)),
    ("activity_fund", ("activity_code",), "planned_activity", ("activity_code",)),
    ("admin_approval", ("activity_code",), "planned_activity", ("activity_code",)),
    ("admin_approval", ("gp_lgd_code",), "gram_panchayat", ("gp_lgd_code",)),
    ("admin_approval_scheme", ("parent_row_id",), "admin_approval", ("row_id",)),
    ("technical_approval", ("activity_code",), "planned_activity", ("activity_code",)),
    ("technical_approval", ("gp_lgd_code",), "gram_panchayat", ("gp_lgd_code",)),
    ("physical_progress", ("activity_code",), "planned_activity", ("activity_code",)),
    # activity_expenditure -> planned_activity(activity_code) is
    # deliberately NOT listed: the spec itself says this FK is unenforced
    # (20 real rows violate it). See schema.py's activity_expenditure
    # comment.
    ("activity_expenditure", ("gp_lgd_code",), "gram_panchayat", ("gp_lgd_code",)),
    ("voucher", ("gp_lgd_code",), "gram_panchayat", ("gp_lgd_code",)),
    ("activity_voucher", ("expenditure_id",), "activity_expenditure", ("expenditure_id",)),
    ("activity_voucher", ("voucher_pk",), "voucher", ("voucher_pk",)),
]


def check_primary_keys(con: duckdb.DuckDBPyConnection) -> list[Check]:
    """No table may hold a duplicate primary key."""

    checks = []
    for table, keys in PRIMARY_KEYS.items():
        columns = ", ".join(keys)
        duplicates = _scalar(
            con,
            f"SELECT count(*) FROM (SELECT {columns} FROM {table} "
            f"GROUP BY {columns} HAVING count(*) > 1)",
        )
        checks.append(Check(f"unique {table}({columns})", duplicates == 0, f"{duplicates} duplicate key(s)"))
    return checks


def check_orphans(con: duckdb.DuckDBPyConnection) -> list[Check]:
    """Every declared relationship must actually hold, non-null side only."""

    checks = []
    for child, child_cols, parent, parent_cols in FOREIGN_KEYS:
        join = " AND ".join(f"p.{pc} = c.{cc}" for cc, pc in zip(child_cols, parent_cols))
        not_null = " AND ".join(f"c.{col} IS NOT NULL" for col in child_cols)
        orphans = _scalar(con, f"""
            SELECT count(*) FROM {child} c
            WHERE {not_null}
              AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {join})
        """)
        checks.append(Check(
            f"{child}({','.join(child_cols)}) -> {parent}({','.join(parent_cols)})",
            orphans == 0, f"{orphans} orphan row(s)",
        ))
    return checks


def check_provenance(con: duckdb.DuckDBPyConnection, selected: tuple[SelectedSnapshot, ...]) -> list[Check]:
    """Every fact row's (source_system, source_run_id) is one that was selected.

    This can only fail if a future code change starts inserting rows
    outside the loop over ``selected`` -- it is the check that would catch
    that regression rather than let it insert silently.
    """

    allowed = {(s.spec.source, s.spec.run_id) for s in selected}
    checks = []
    for table in FACT_TABLES:
        if table in NO_LINEAGE_TABLES:
            continue
        rows = con.execute(f"SELECT DISTINCT source_system, source_run_id FROM {table}").fetchall()
        stray = [pair for pair in rows if pair not in allowed]
        checks.append(Check(
            f"{table} provenance is within the selected snapshots", not stray,
            f"unselected (source_system, source_run_id) present: {stray}" if stray else "ok",
        ))
    return checks


def check_counts(con: duckdb.DuckDBPyConnection, counts: dict[str, int]) -> list[Check]:
    """The row count reported by the loader must match what is actually stored."""

    checks = []
    for table, expected in counts.items():
        if table == "quarantine":
            continue
        actual = _scalar(con, f"SELECT count(*) FROM {table}")
        checks.append(Check(f"rows in {table}", actual == expected, f"loader reported {expected}, table has {actual}"))
    return checks


def check_geography(con: duckdb.DuckDBPyConnection) -> list[Check]:
    """Every GP the LGD reference tree knows must carry its geography.

    Scope note, because the obvious check is the wrong one. A blank-*fraction*
    check cannot work here: a synthetic fixture legitimately uses a GP code
    like ``123`` that is not a real Odisha GP, so it resolves against nothing
    and would fail a fraction test at 100% while the loader is perfectly fine.
    An unresolved code is unknown geography, not a broken join.

    So the denominator is the GPs the tree actually covers. That makes this an
    invariant guard -- ``geography.gp_geography`` builds all six columns
    together, so an in-tree code can never be partially blank today, and this
    check exists to keep that true through future edits to the join in
    ``transform.gram_panchayat``.

    It deliberately does NOT assert scale. "Are all 6,794 GPs here, in 30
    districts and 314 blocks?" is a statement about the reference build, not
    about any database, and it lives in
    ``conformance.check_geography_completeness`` where the expected
    cardinality is known. #61 asked for both, and they are different checks.
    """

    total = _scalar(con, "SELECT count(*) FROM gram_panchayat")
    if not total:
        return [Check("gram_panchayat geography", True, "no rows to check")]
    known = set(gp_geography())
    # Every geography column, driven off the canonical tuple rather than a
    # hand-picked subset. An earlier revision of this check listed three of
    # the six by name and passed a table whose district_code and state_code
    # were 100% NULL -- the #61 failure mode, surviving its own guard.
    columns = ", ".join(GEOGRAPHY_COLUMNS)
    rows = con.execute(f"SELECT gp_lgd_code, {columns} FROM gram_panchayat").fetchall()
    resolvable = [row for row in rows if row[0] in known]
    blank = [row[0] for row in resolvable if any(value is None for value in row[1:])]
    unresolved = len(rows) - len(resolvable)
    detail = (
        f"{len(resolvable)} of {total} row(s) are in the LGD reference tree; "
        f"{len(blank)} of those are missing at least one of {columns}"
    )
    if unresolved:
        # Reported, never fatal: a GP code absent from the tree means the tree
        # needs refreshing (or the fixture is synthetic), and the full-state
        # conformance check is what turns that into a ship-blocking number.
        detail += f"; {unresolved} row(s) not in the tree"
    return [Check("gram_panchayat geography", not blank, detail)]


def run_checks(
    con: duckdb.DuckDBPyConnection, counts: dict[str, int], selected: tuple[SelectedSnapshot, ...],
) -> list[Check]:
    return [
        *check_primary_keys(con),
        *check_orphans(con),
        *check_provenance(con, selected),
        *check_counts(con, counts),
        *check_geography(con),
    ]
