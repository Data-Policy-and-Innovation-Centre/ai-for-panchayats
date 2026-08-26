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

from .schema import FACT_TABLES
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
    "plan": ("source_system", "source_run_id", "plan_code"),
    "planned_activity": ("source_system", "source_run_id", "activity_code"),
    "activity_delegation": ("source_system", "source_run_id", "activity_code"),
    "activity_training": ("source_system", "source_run_id", "activity_code"),
    "activity_community_service": ("source_system", "source_run_id", "activity_code"),
    "activity_nsap": ("source_system", "source_run_id", "activity_code", "category", "age_band", "gender"),
    "activity_asset": ("source_system", "source_run_id", "row_id"),
    "activity_fund": ("source_system", "source_run_id", "row_id"),
    "admin_approval": ("source_system", "source_run_id", "row_id"),
    "admin_approval_scheme": ("source_system", "source_run_id", "row_id"),
    "technical_approval": ("source_system", "source_run_id", "row_id"),
    "physical_progress": ("source_system", "source_run_id", "row_id"),
    "recommended_expenditure": (
        "source_system", "source_run_id", "gp_lgd_code", "plan_code", "activity_code", "s_no",
    ),
}

FOREIGN_KEYS: list[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = [
    ("plan", ("gp_lgd_code",), "gram_panchayat", ("gp_lgd_code",)),
    ("planned_activity", ("source_system", "source_run_id", "plan_code"),
     "plan", ("source_system", "source_run_id", "plan_code")),
    ("planned_activity", ("gp_lgd_code",), "gram_panchayat", ("gp_lgd_code",)),
    ("activity_delegation", ("source_system", "source_run_id", "activity_code"),
     "planned_activity", ("source_system", "source_run_id", "activity_code")),
    ("activity_training", ("source_system", "source_run_id", "activity_code"),
     "planned_activity", ("source_system", "source_run_id", "activity_code")),
    ("activity_community_service", ("source_system", "source_run_id", "activity_code"),
     "planned_activity", ("source_system", "source_run_id", "activity_code")),
    ("activity_nsap", ("source_system", "source_run_id", "activity_code"),
     "planned_activity", ("source_system", "source_run_id", "activity_code")),
    ("activity_asset", ("source_system", "source_run_id", "activity_code"),
     "planned_activity", ("source_system", "source_run_id", "activity_code")),
    ("activity_fund", ("source_system", "source_run_id", "activity_code"),
     "planned_activity", ("source_system", "source_run_id", "activity_code")),
    ("admin_approval", ("source_system", "source_run_id", "activity_code"),
     "planned_activity", ("source_system", "source_run_id", "activity_code")),
    ("admin_approval", ("gp_lgd_code",), "gram_panchayat", ("gp_lgd_code",)),
    ("admin_approval_scheme", ("source_system", "source_run_id", "parent_row_id"),
     "admin_approval", ("source_system", "source_run_id", "row_id")),
    ("technical_approval", ("source_system", "source_run_id", "activity_code"),
     "planned_activity", ("source_system", "source_run_id", "activity_code")),
    ("technical_approval", ("gp_lgd_code",), "gram_panchayat", ("gp_lgd_code",)),
    ("physical_progress", ("source_system", "source_run_id", "activity_code"),
     "planned_activity", ("source_system", "source_run_id", "activity_code")),
    ("recommended_expenditure", ("gp_lgd_code",), "gram_panchayat", ("gp_lgd_code",)),
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
        if table == "gram_panchayat":
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


def run_checks(
    con: duckdb.DuckDBPyConnection, counts: dict[str, int], selected: tuple[SelectedSnapshot, ...],
) -> list[Check]:
    return [
        *check_primary_keys(con),
        *check_orphans(con),
        *check_provenance(con, selected),
        *check_counts(con, counts),
    ]
