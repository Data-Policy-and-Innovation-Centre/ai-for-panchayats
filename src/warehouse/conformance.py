"""Spec conformance checker for a *built* DuckDB warehouse.

This module is the acceptance test for a reproduced data pipeline: given a
path to a built ``.duckdb`` file, it reports every way the file deviates
from ``THE SPECIFICATION`` transcribed below from the project's ER diagram
and database description.

Deliberate independence: every check queries DuckDB's own catalog
(``information_schema`` / ``duckdb_constraints()``) through a live
connection at runtime. Nothing here imports ``warehouse.schema`` or
``warehouse.transform``. A checker built from the same constants as the
thing it checks can only ever agree with itself; this one has to actually
look.

Usage::

    from warehouse.conformance import check_conformance
    findings = check_conformance(con)
    if any(f.severity == "violation" for f in findings):
        ...

Or from the command line::

    uv run python scripts/check_warehouse_conformance.py path/to/warehouse.duckdb
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import duckdb

Severity = Literal["violation", "informational"]


@dataclass(frozen=True, slots=True)
class Finding:
    """One reportable deviation (or informational note) from the spec.

    ``expected``/``actual`` are always populated (even for informational
    findings) so a report line never has to say "see detail" instead of
    showing the comparison.
    """

    check: str
    severity: Severity
    expected: str
    actual: str
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        tag = "VIOLATION" if self.severity == "violation" else "INFO"
        line = f"[{tag}] {self.check}: expected {self.expected}, got {self.actual}"
        if self.detail:
            line += f" -- {self.detail}"
        return line


# ---------------------------------------------------------------------------
# THE SPECIFICATION -- authoritative constants, easy to update in one place.
# ---------------------------------------------------------------------------

# Section 1: exactly these tables must exist -- the spec's 19 plus gp_profile.
# The count is deliberately not written into the check messages below: it
# has drifted once already, and this set is the only place that should
# have to change when it drifts again.
EXPECTED_TABLES: frozenset[str] = frozenset({
    "gram_panchayat", "plan", "planned_activity", "activity_delegation",
    "activity_asset", "activity_fund", "activity_training", "activity_community_service",
    "activity_nsap", "activity_expenditure", "voucher", "activity_voucher",
    "admin_approval", "admin_approval_scheme", "technical_approval", "physical_progress",
    "dim_code", "dim_welfare_scheme", "dim_lsdg_theme",
    # The twentieth: GP demographics from the panchayat profile extract
    # (#123). The Box spec documents "19 tables" and needs the same
    # amendment, or the checker and the documents now disagree.
    "gp_profile",
})

# An internal bookkeeping table that may legitimately exist alongside the 20.
# Its presence is informational, never a violation.
ALLOWED_EXTRA_TABLES: frozenset[str] = frozenset({"quarantine"})

# The consumer-facing relations, materialised as tables by warehouse.views
# (#51). They are derived from the fact tables -- no new facts -- so they are allowed
# alongside them rather than counted among them. Checked for presence
# separately: a warehouse missing them builds but is not consumable.
DERIVED_RELATIONS: frozenset[str] = frozenset({
    "v_exp", "v_approval", "v_activity", "v_activity_base", "v_plan",
    "v_asset", "v_progress", "v_voucher",
})

# The one derived relation that must NOT be stored. `v_activity` adds
# `days_since_sanction`, which counts days up to CURRENT_DATE, so a stored
# copy would answer as of the build date and drift for as long as the
# snapshot stayed deployed. Its join graph is stored as `v_activity_base`
# instead, so the cost #51 exists to remove is still paid once. Named here
# rather than merely omitted from a list, so that storing it later trips the
# check instead of silently reintroducing the staleness.
DYNAMIC_RELATIONS: frozenset[str] = frozenset({"v_activity"})

# Section 2: primary keys. ``None`` means "no primary key is expected" --
# a table appearing here with None is a documented fact from the spec
# (activity_voucher, dim_lsdg_theme), not an omission.
EXPECTED_PRIMARY_KEYS: dict[str, tuple[str, ...] | None] = {
    "gram_panchayat": ("gp_lgd_code",),
    "gp_profile": ("gp_lgd_code",),
    "plan": ("plan_code",),
    "planned_activity": ("activity_code",),
    "activity_delegation": ("activity_code",),
    "activity_asset": ("activity_code",),
    "activity_fund": ("activity_code",),
    "activity_training": ("activity_code",),
    "activity_community_service": ("activity_code",),
    "activity_nsap": ("nsap_id",),
    "activity_expenditure": ("expenditure_id",),
    "voucher": ("voucher_pk",),
    "activity_voucher": None,
    "admin_approval": ("row_id",),
    "admin_approval_scheme": ("row_id",),
    "technical_approval": ("row_id",),
    "physical_progress": ("row_id",),
    "dim_code": ("variable", "code"),
    "dim_welfare_scheme": ("scheme_code",),
    "dim_lsdg_theme": None,
}

# Section 3: constraints.
VOUCHER_UNIQUE_COLUMNS: tuple[str, ...] = ("gp_lgd_code", "fiscal_year", "voucher_no")
NO_ENFORCED_FK: tuple[str, str, str] = ("activity_expenditure", "activity_code", "planned_activity")
NULLABLE_REQUIRED: tuple[str, str] = ("activity_voucher", "voucher_pk")

# Every other FK the spec's ER diagram gives, positive this time: each
# (child_table, child_column, referenced_table) triple below must be an
# *enforced* FOREIGN KEY. This is deliberately the complement of
# NO_ENFORCED_FK above -- that one forbidden relationship is the single
# documented exception, not evidence that FKs in general are optional.
# activity_expenditure.plan_code -> plan is deliberately excluded here too,
# for the same "not verified to always resolve" reason NO_ENFORCED_FK is.
EXPECTED_FOREIGN_KEYS: tuple[tuple[str, str, str], ...] = (
    ("gp_profile", "gp_lgd_code", "gram_panchayat"),
    ("plan", "gp_lgd_code", "gram_panchayat"),
    ("planned_activity", "plan_code", "plan"),
    ("planned_activity", "gp_lgd_code", "gram_panchayat"),
    ("activity_delegation", "activity_code", "planned_activity"),
    ("activity_asset", "activity_code", "planned_activity"),
    ("activity_fund", "activity_code", "planned_activity"),
    ("activity_training", "activity_code", "planned_activity"),
    ("activity_community_service", "activity_code", "planned_activity"),
    ("activity_nsap", "activity_code", "planned_activity"),
    ("activity_expenditure", "gp_lgd_code", "gram_panchayat"),
    ("voucher", "gp_lgd_code", "gram_panchayat"),
    ("activity_voucher", "expenditure_id", "activity_expenditure"),
    ("activity_voucher", "voucher_pk", "voucher"),
    ("admin_approval", "activity_code", "planned_activity"),
    ("admin_approval", "gp_lgd_code", "gram_panchayat"),
    ("admin_approval_scheme", "parent_row_id", "admin_approval"),
    ("technical_approval", "activity_code", "planned_activity"),
    ("technical_approval", "gp_lgd_code", "gram_panchayat"),
    ("physical_progress", "activity_code", "planned_activity"),
)

# Section 4: types.
# Business keys must be VARCHAR wherever they appear, in any table.
BUSINESS_KEY_COLUMNS: frozenset[str] = frozenset({
    "gp_lgd_code", "activity_code", "plan_code", "voucher_no",
})
# Surrogate keys must be an integer type wherever they appear.
SURROGATE_KEY_COLUMNS: frozenset[str] = frozenset({"expenditure_id", "voucher_pk", "nsap_id"})
INTEGER_TYPES: frozenset[str] = frozenset({
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
})
# Accounting/expenditure money is DECIMAL(16,2); planning-side cost is DOUBLE.
# Column names below are the ones named explicitly in the reconciliation
# section of the spec -- update here if a real build spells them differently.
MONEY_DECIMAL_16_2: dict[tuple[str, str], str] = {
    ("voucher", "amount"): "DECIMAL(16,2)",
    ("activity_expenditure", "total_expenditure"): "DECIMAL(16,2)",
}
MONEY_DOUBLE: dict[tuple[str, str], str] = {
    ("planned_activity", "total_cost"): "DOUBLE",
}
BENEFICIARY_COUNT_COLUMN: tuple[str, str] = ("activity_nsap", "beneficiary_count")

# Section 5: data-level invariants.
SATELLITE_TABLES: tuple[str, ...] = (
    "activity_delegation", "activity_asset", "activity_fund",
    "activity_training", "activity_community_service",
)
PARENT_TABLE_FOR_SATELLITES = "planned_activity"
VOUCHER_DIRECTION_TABLE = "voucher"
VOUCHER_DIRECTION_COLUMN = "direction"
VOUCHER_ALLOWED_DIRECTIONS: frozenset[str] = frozenset({"payment", "receipt"})
FISCAL_YEAR_COLUMN = "fiscal_year"
FISCAL_YEAR_PATTERN = r"^[0-9]{4}-[0-9]{4}$"
DIM_CODE_TABLE = "dim_code"
DIM_CODE_COLUMNS: tuple[str, str] = ("variable", "code")

# Odisha's administrative cardinality, from the LGD reference tree that
# populates these columns (30 zillas / 314 blocks / 6,794 GPs -- counted, not
# quoted). Asserted rather than eyeballed, per #61: a geography backfill that
# joins on the wrong key still produces non-null columns, and the only thing
# that catches it is the shape of the result.
GRAM_PANCHAYAT_TABLE = "gram_panchayat"
GEOGRAPHY_COLUMNS: tuple[str, ...] = (
    "state_code", "state_name", "district_code", "zp_name", "block_code", "block_name",
)
EXPECTED_GP_COUNT = 6794
EXPECTED_DISTRICT_COUNT = 30
EXPECTED_BLOCK_COUNT = 314

# Coverage, not equality. `gram_panchayat` is built from GPs that were
# *observed* in the scrape, and a GP whose every payload is empty produces no
# rows and so never reaches the dimension -- verified: three scraped folders,
# one of them dataless, yields two rows. Demanding exactly 6,794 would refuse
# a genuinely complete build.
#
# The threshold does not need to be precise, because the thing it separates
# is three orders of magnitude wide: the pilot is 20 GPs, the state is ~6,800.
# Anything between is neither, and should be looked at by a person.
MIN_GP_COVERAGE = 0.90

# The demographic measures, spelled out here rather than imported from
# `transform` on purpose: this module is written against the spec, not against
# our loader's inputs, so a rename on one side has to fail loudly instead of
# being followed silently by the checker meant to catch it.
#
# Listed in full for the reason the geography list is: naming a subset let a
# table with 100% NULL district_code pass clean, which is what #61 is named
# for. The same trap is already visible here -- the manifest test fixture
# populated total_population and households and left the other eight NULL,
# and every check passed.
GP_PROFILE_TABLE = "gp_profile"
GP_PROFILE_MEASURES: tuple[str, ...] = (
    "total_population", "male_population", "female_population",
    "transgender_population", "children_population", "sc_population",
    "st_population", "obc_population", "general_population", "households",
)

# Section 6: reconciliation totals -- the exact published totals from the
# reference build. Kept as easy-to-update constants; compared with exact
# decimal arithmetic, never binary float ``==``.
#
# Full-state figures (#175). The first two were measured from the full-state
# build and agree with the externally-built production database exactly, to
# the paisa; they were the 20-GP pilot figures until #175, which is why every
# real build had to skip this whole section and so checked none of it.
#
# The voucher total is NOT the production figure. Production reads
# 455046197982.47; ours reads what our source can support, because the
# accounting extract covers 6,436 of 6,794 GPs (#171). It is recorded here
# rather than left at the pilot value so that the number is visible and the
# gap is one subtraction, but it is exempted by name in every real build
# until #171 lands -- see EXEMPTABLE_RECONCILIATION_CHECKS.
EXPECTED_VOUCHER_AMOUNT_TOTAL = Decimal("428724765277.36")
EXPECTED_ACTIVITY_EXPENDITURE_TOTAL = Decimal("78053445024.44")
EXPECTED_PLANNED_COST_TOTAL = Decimal("258086866807.00")

RECONCILIATION_TARGETS: tuple[tuple[str, str, str, Decimal], ...] = (
    ("reconciliation.voucher_amount_total", "voucher", "amount", EXPECTED_VOUCHER_AMOUNT_TOTAL),
    (
        "reconciliation.activity_expenditure_total", "activity_expenditure",
        "total_expenditure", EXPECTED_ACTIVITY_EXPENDITURE_TOTAL,
    ),
    (
        "reconciliation.planned_cost_total", "planned_activity",
        "total_cost", EXPECTED_PLANNED_COST_TOTAL,
    ),
)

# The names ``exempt_reconciliation`` accepts, derived from the targets rather
# than spelled a second time, so a renamed target cannot leave a stale
# exemption quietly matching nothing.
EXEMPTABLE_RECONCILIATION_CHECKS = frozenset(name for name, *_ in RECONCILIATION_TARGETS)


# ---------------------------------------------------------------------------
# Catalog introspection helpers -- live queries only, no imported constants.
# ---------------------------------------------------------------------------

def _existing_tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE'"
    ).fetchall()
    return {row[0] for row in rows}


def _row_count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _columns(con: duckdb.DuckDBPyConnection, table: str) -> dict[str, tuple[str, str]]:
    """``{column_name: (data_type, is_nullable)}`` for one table."""

    rows = con.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return {name: (data_type, is_nullable) for name, data_type, is_nullable in rows}


def _primary_key_columns(con: duckdb.DuckDBPyConnection, table: str) -> tuple[str, ...] | None:
    row = con.execute(
        "SELECT constraint_column_names FROM duckdb_constraints() "
        "WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'",
        [table],
    ).fetchone()
    if row is None:
        return None
    return tuple(row[0])


def _has_unique_constraint(
    con: duckdb.DuckDBPyConnection, table: str, columns: tuple[str, ...],
) -> bool:
    rows = con.execute(
        "SELECT constraint_column_names FROM duckdb_constraints() "
        "WHERE table_name = ? AND constraint_type = 'UNIQUE'",
        [table],
    ).fetchall()
    wanted = set(columns)
    return any(set(row[0]) == wanted for row in rows)


def _enforced_fk_exists(
    con: duckdb.DuckDBPyConnection, table: str, column: str, referenced_table: str,
) -> bool:
    rows = con.execute(
        "SELECT constraint_column_names, referenced_table FROM duckdb_constraints() "
        "WHERE table_name = ? AND constraint_type = 'FOREIGN KEY'",
        [table],
    ).fetchall()
    return any(list(cols) == [column] and ref == referenced_table for cols, ref in rows)


def _decimal_sum(con: duckdb.DuckDBPyConnection, table: str, column: str) -> Decimal:
    """Sum ``column`` with exact decimal arithmetic, regardless of storage type.

    Casting inside DuckDB (rather than summing in Python) means the result
    always comes back as a ``decimal.Decimal``, so callers never compare
    money with binary-float ``==``.
    """

    result = con.execute(f"SELECT CAST(SUM({column}) AS DECIMAL(38,2)) FROM {table}").fetchone()[0]
    if result is None:
        return Decimal("0.00")
    return Decimal(str(result))


# ---------------------------------------------------------------------------
# Individual checks. Each returns only the findings for its own concern, so
# tests can exercise one check at a time.
# ---------------------------------------------------------------------------

def check_table_existence(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 1: exactly EXPECTED_TABLES, missing/extra reported separately."""

    findings: list[Finding] = []
    actual = _existing_tables(con)
    missing = EXPECTED_TABLES - actual
    for table in sorted(missing):
        findings.append(Finding(
            check="tables.missing", severity="violation",
            expected=f"table {table!r} exists", actual="absent",
        ))

    unexpected = actual - EXPECTED_TABLES - ALLOWED_EXTRA_TABLES - DERIVED_RELATIONS
    for table in sorted(unexpected):
        findings.append(Finding(
            check="tables.unexpected", severity="violation",
            expected="only the expected tables", actual=f"unexpected table {table!r}",
        ))

    for table in sorted(actual & ALLOWED_EXTRA_TABLES):
        findings.append(Finding(
            check="tables.internal", severity="informational",
            expected="not part of the expected inventory", actual=f"{table!r} present",
            detail="internal bookkeeping table, not a violation",
        ))
    return findings


def check_primary_keys(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 2: primary key columns, including the two "no PK" tables."""

    findings: list[Finding] = []
    actual_tables = _existing_tables(con)
    for table, expected_pk in EXPECTED_PRIMARY_KEYS.items():
        if table not in actual_tables:
            continue  # already reported by check_table_existence
        actual_pk = _primary_key_columns(con, table)
        if expected_pk is None:
            if actual_pk is not None:
                findings.append(Finding(
                    check=f"primary_key.{table}", severity="violation",
                    expected="no primary key", actual=f"PRIMARY KEY({', '.join(actual_pk)})",
                ))
            continue
        if actual_pk is None:
            findings.append(Finding(
                check=f"primary_key.{table}", severity="violation",
                expected=f"PRIMARY KEY({', '.join(expected_pk)})", actual="no primary key",
            ))
        elif set(actual_pk) != set(expected_pk):
            findings.append(Finding(
                check=f"primary_key.{table}", severity="violation",
                expected=f"PRIMARY KEY({', '.join(expected_pk)})",
                actual=f"PRIMARY KEY({', '.join(actual_pk)})",
            ))
    return findings


def check_voucher_unique_constraint(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 3: voucher UNIQUE(gp_lgd_code, fiscal_year, voucher_no)."""

    table = "voucher"
    if table not in _existing_tables(con):
        return []
    if _has_unique_constraint(con, table, VOUCHER_UNIQUE_COLUMNS):
        return []
    return [Finding(
        check="constraint.voucher_unique", severity="violation",
        expected=f"UNIQUE({', '.join(VOUCHER_UNIQUE_COLUMNS)}) on voucher",
        actual="no matching UNIQUE constraint found",
    )]


def check_activity_expenditure_fk_not_enforced(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 3: activity_expenditure.activity_code must NOT have an enforced FK.

    20 legitimate orphans exist by design; an enforced FK would reject them.
    """

    table, column, referenced = NO_ENFORCED_FK
    if table not in _existing_tables(con):
        return []
    if _enforced_fk_exists(con, table, column, referenced):
        return [Finding(
            check="constraint.activity_expenditure_fk_not_enforced", severity="violation",
            expected=f"no enforced FK from {table}.{column} to {referenced}",
            actual=f"enforced FOREIGN KEY {table}.{column} -> {referenced} found",
            detail="20 legitimate orphans exist by design; an enforced FK would reject them",
        )]
    return []


def check_required_foreign_keys(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 3: every positive FK the spec's ER diagram gives must be enforced.

    Complements ``check_activity_expenditure_fk_not_enforced`` above, which
    checks the one deliberately-*forbidden* relationship. Without this
    check, a warehouse that dropped every other declared FK still passes
    conformance whenever its current rows happen to satisfy referential
    integrity by coincidence -- and accepts orphan rows the moment they
    stop coinciding.
    """

    findings: list[Finding] = []
    actual_tables = _existing_tables(con)
    for table, column, referenced in EXPECTED_FOREIGN_KEYS:
        if table not in actual_tables or referenced not in actual_tables:
            continue  # already reported by check_table_existence
        if not _enforced_fk_exists(con, table, column, referenced):
            findings.append(Finding(
                check=f"constraint.foreign_key.{table}.{column}", severity="violation",
                expected=f"FOREIGN KEY {table}.{column} -> {referenced}",
                actual="no enforced FK found",
            ))
    return findings


def check_activity_voucher_nullable(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 3: activity_voucher.voucher_pk MUST be nullable (488 unmatched rows)."""

    table, column = NULLABLE_REQUIRED
    if table not in _existing_tables(con):
        return []
    columns = _columns(con, table)
    if column not in columns:
        return [Finding(
            check="constraint.activity_voucher_nullable", severity="violation",
            expected=f"{table}.{column} exists and is nullable", actual="column not found",
        )]
    _, is_nullable = columns[column]
    if is_nullable != "YES":
        return [Finding(
            check="constraint.activity_voucher_nullable", severity="violation",
            expected=f"{table}.{column} is nullable", actual=f"{table}.{column} is NOT NULL",
            detail="488 legitimately unmatched rows require this column to allow NULL",
        )]
    return []


def check_business_key_types(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 4: gp_lgd_code/activity_code/plan_code/voucher_no are always VARCHAR."""

    findings: list[Finding] = []
    for table in sorted(_existing_tables(con)):
        for column, (data_type, _) in _columns(con, table).items():
            if column in BUSINESS_KEY_COLUMNS and data_type != "VARCHAR":
                findings.append(Finding(
                    check=f"type.business_key.{table}.{column}", severity="violation",
                    expected="VARCHAR", actual=data_type,
                    detail="business keys must stay VARCHAR to preserve leading zeros",
                ))
    return findings


def check_surrogate_key_types(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 4: expenditure_id/voucher_pk/nsap_id are always an integer type."""

    findings: list[Finding] = []
    for table in sorted(_existing_tables(con)):
        for column, (data_type, _) in _columns(con, table).items():
            if column in SURROGATE_KEY_COLUMNS and data_type not in INTEGER_TYPES:
                findings.append(Finding(
                    check=f"type.surrogate_key.{table}.{column}", severity="violation",
                    expected="an integer type", actual=data_type,
                ))
    return findings


def check_money_types(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 4: accounting money is DECIMAL(16,2); planning cost is DOUBLE.

    A table that exists but is missing the expected money column entirely
    is reported as its own violation, before any type comparison -- a
    silent ``continue`` here would let a warehouse with no
    ``voucher.amount`` (etc.) pass this check even though it is
    structurally incompatible with the spec.
    """

    findings: list[Finding] = []
    actual_tables = _existing_tables(con)
    for money_map in (MONEY_DECIMAL_16_2, MONEY_DOUBLE):
        for (table, column), expected_type in money_map.items():
            if table not in actual_tables:
                continue
            columns = _columns(con, table)
            if column not in columns:
                findings.append(Finding(
                    check=f"type.money.{table}.{column}", severity="violation",
                    expected=f"column {table}.{column} ({expected_type})", actual="column not found",
                ))
                continue
            data_type, _ = columns[column]
            if data_type != expected_type:
                findings.append(Finding(
                    check=f"type.money.{table}.{column}", severity="violation",
                    expected=expected_type, actual=data_type,
                ))
    return findings


def check_beneficiary_count_type(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 4: activity_nsap.beneficiary_count is a count, not money."""

    table, column = BENEFICIARY_COUNT_COLUMN
    if table not in _existing_tables(con):
        return []
    columns = _columns(con, table)
    if column not in columns:
        return [Finding(
            check="type.beneficiary_count", severity="violation",
            expected=f"column {table}.{column} exists", actual="column not found",
        )]
    data_type, _ = columns[column]
    if data_type not in INTEGER_TYPES:
        return [Finding(
            check="type.beneficiary_count", severity="violation",
            expected="an integer type", actual=data_type,
        )]
    return []


def check_satellite_row_parity(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 5: the five 1:1 satellites match planned_activity exactly."""

    findings: list[Finding] = []
    actual_tables = _existing_tables(con)
    if PARENT_TABLE_FOR_SATELLITES not in actual_tables:
        return findings
    parent_count = _row_count(con, PARENT_TABLE_FOR_SATELLITES)

    for table in SATELLITE_TABLES:
        if table not in actual_tables:
            continue
        satellite_count = _row_count(con, table)
        if parent_count == 0 and satellite_count == 0:
            continue  # nothing to reconcile
        if satellite_count != parent_count:
            findings.append(Finding(
                check=f"data.satellite_row_count.{table}", severity="violation",
                expected=f"{parent_count} row(s) (== planned_activity)",
                actual=f"{satellite_count} row(s)",
            ))
        parent_codes = {
            row[0] for row in con.execute(
                f"SELECT activity_code FROM {PARENT_TABLE_FOR_SATELLITES}"
            ).fetchall()
        }
        satellite_codes = {
            row[0] for row in con.execute(f"SELECT activity_code FROM {table}").fetchall()
        }
        orphans = satellite_codes - parent_codes
        unmatched_parents = parent_codes - satellite_codes
        if orphans:
            findings.append(Finding(
                check=f"data.satellite_orphans.{table}", severity="violation",
                expected="every activity_code present in planned_activity",
                actual=f"{len(orphans)} orphan activity_code(s) in {table}",
                detail=f"example: {sorted(orphans)[:5]!r}",
            ))
        if unmatched_parents:
            findings.append(Finding(
                check=f"data.satellite_missing.{table}", severity="violation",
                expected=f"every planned_activity row has a {table} row",
                actual=f"{len(unmatched_parents)} planned_activity row(s) with no {table} row",
                detail=f"example: {sorted(unmatched_parents)[:5]!r}",
            ))
    return findings


def check_voucher_direction_values(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 5: voucher.direction is only 'payment' or 'receipt'."""

    table = VOUCHER_DIRECTION_TABLE
    if table not in _existing_tables(con):
        return []
    if VOUCHER_DIRECTION_COLUMN not in _columns(con, table):
        return []
    if _row_count(con, table) == 0:
        return []
    bad_values = con.execute(
        f"SELECT DISTINCT {VOUCHER_DIRECTION_COLUMN} FROM {table} "
        f"WHERE {VOUCHER_DIRECTION_COLUMN} IS NULL "
        f"OR {VOUCHER_DIRECTION_COLUMN} NOT IN ('payment', 'receipt')"
    ).fetchall()
    if not bad_values:
        return []
    return [Finding(
        check="data.voucher_direction", severity="violation",
        expected="only 'payment' or 'receipt'",
        actual=f"found {sorted(str(v[0]) for v in bad_values)}",
    )]


def check_activity_nsap_empty(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 5: activity_nsap is expected empty; non-empty is informational."""

    table = "activity_nsap"
    if table not in _existing_tables(con):
        return []
    count = _row_count(con, table)
    if count == 0:
        return []
    return [Finding(
        check="data.activity_nsap_empty", severity="informational",
        expected="0 rows", actual=f"{count} row(s)",
        detail="non-empty is unexpected but not treated as a failure",
    )]


def check_fiscal_year_format(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 5: fiscal_year values match 'YYYY-YYYY', never 'YYYY-YY'."""

    findings: list[Finding] = []
    for table in sorted(_existing_tables(con)):
        if FISCAL_YEAR_COLUMN not in _columns(con, table):
            continue
        if _row_count(con, table) == 0:
            continue
        bad_values = con.execute(
            f"SELECT DISTINCT {FISCAL_YEAR_COLUMN} FROM {table} "
            f"WHERE {FISCAL_YEAR_COLUMN} IS NOT NULL "
            f"AND NOT regexp_matches({FISCAL_YEAR_COLUMN}, ?)",
            [FISCAL_YEAR_PATTERN],
        ).fetchall()
        if bad_values:
            findings.append(Finding(
                check=f"data.fiscal_year_format.{table}", severity="violation",
                expected="four-digit-end-year form 'YYYY-YYYY' (e.g. '2025-2026')",
                actual=f"found {sorted(str(v[0]) for v in bad_values)}",
                detail="the short form 'YYYY-YY' silently returns zero rows when filtered on",
            ))
    return findings


def check_dim_code_uniqueness(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Section 5: every (variable, code) pair in dim_code is unique."""

    table = DIM_CODE_TABLE
    if table not in _existing_tables(con):
        return []
    columns = _columns(con, table)
    if not all(col in columns for col in DIM_CODE_COLUMNS):
        return []
    if _row_count(con, table) == 0:
        return []
    cols = ", ".join(DIM_CODE_COLUMNS)
    duplicates = con.execute(
        f"SELECT {cols}, count(*) FROM {table} GROUP BY {cols} HAVING count(*) > 1"
    ).fetchall()
    if not duplicates:
        return []
    return [Finding(
        check="data.dim_code_uniqueness", severity="violation",
        expected=f"unique ({cols}) pairs", actual=f"{len(duplicates)} duplicate pair(s)",
        detail=f"example: {duplicates[:5]!r}",
    )]


def check_derived_relations(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """The consumer relations exist, and each is stored or dynamic as intended.

    Behind its own ``skip_derived`` flag for the reason
    ``check_geography_completeness`` is behind ``skip_geography``: a fixture
    that creates the spec tables to exercise a schema rule is not wrong
    for having no ``v_activity``. A real build is.

    Three things can be wrong, and this checks all three.

    **Absent** means the chatbot has nothing to query -- it reads these and
    nothing else, so a warehouse without them is not consumable however
    complete its facts are.

    **A view where a table was expected** means the join graph is still being
    re-run per question, which is the 36x cost #51 exists to remove (#99,
    #98).

    **A table where a view was expected** is the opposite mistake and is
    quieter, which is why ``DYNAMIC_RELATIONS`` is named rather than merely
    left out of a list. ``v_activity`` adds ``days_since_sanction``, a count
    of days up to ``CURRENT_DATE``; stored, it answers as of the build date
    and drifts for every day the snapshot stays deployed. Nothing fails --
    the number is simply wrong, and grows more wrong.
    """

    relations = {
        name: kind for name, kind in con.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
    }
    findings: list[Finding] = []
    missing = DERIVED_RELATIONS - set(relations)
    if missing:
        findings.append(Finding(
            check="relations.derived", severity="violation",
            expected=f"the {len(DERIVED_RELATIONS)} consumer relations",
            actual=f"missing {sorted(missing)}",
        ))
    not_stored = sorted(
        name for name in (DERIVED_RELATIONS - DYNAMIC_RELATIONS) & set(relations)
        if relations[name] != "BASE TABLE"
    )
    wrongly_stored = sorted(
        name for name in DYNAMIC_RELATIONS & set(relations)
        if relations[name] == "BASE TABLE"
    )
    if wrongly_stored:
        findings.append(Finding(
            check="relations.dynamic", severity="violation",
            expected=f"{sorted(DYNAMIC_RELATIONS)} recomputed per query",
            actual=f"stored as tables: {wrongly_stored}",
            detail="days_since_sanction counts up to CURRENT_DATE; a stored "
                   "copy freezes it at build time and drifts thereafter",
        ))
    if not_stored:
        findings.append(Finding(
            check="relations.materialized", severity="violation",
            expected="stored tables, so the joins are paid once at build time",
            actual=f"still views: {not_stored}",
        ))
    return findings


def check_geography_completeness(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """gram_panchayat carries geography for the whole state.

    Behind its own ``skip_geography`` flag rather than run always, because a
    synthetic fixture holding three GPs is not wrong for having three
    districts -- but deliberately NOT behind ``skip_reconciliation``, which
    every real build must pass today for unrelated reasons (#46, #48, #129).

    #61 shipped 6,794 rows with no district behind a green build, because
    nothing checked. Completeness and cardinality are both asserted: a table
    can be fully populated and still be a partial state, or have the right
    row count and no geography.

    What cardinality does NOT catch is a *mis-join*. Keying the join on
    ``gp_name`` instead of ``gp_lgd_code`` still yields 30 distinct
    ``zp_name`` and 314 distinct ``block_code`` -- just attached to the wrong
    GPs. That hazard is pinned by
    ``test_repeated_gp_name_gets_different_geography``, not from here.
    """

    table = GRAM_PANCHAYAT_TABLE
    if table not in _existing_tables(con):
        return []  # already reported by check_table_existence
    columns = _columns(con, table)
    missing = tuple(col for col in GEOGRAPHY_COLUMNS if col not in columns)
    if missing:
        return [Finding(
            check="geography.columns", severity="violation",
            expected=f"{table} has {list(GEOGRAPHY_COLUMNS)}",
            actual=f"missing {list(missing)}",
        )]

    findings: list[Finding] = []
    total = _row_count(con, table)
    floor = int(EXPECTED_GP_COUNT * MIN_GP_COVERAGE)
    if total < floor:
        findings.append(Finding(
            check="geography.gp_count", severity="violation",
            expected=f"at least {floor} of {EXPECTED_GP_COUNT} GPs",
            actual=str(total),
            detail="a build well short of the roster is a sample, not the state",
        ))
    elif total > EXPECTED_GP_COUNT:
        # The other direction: GP codes the roster does not know mean the
        # build and the reference tree disagree about what Odisha is -- and
        # that tree is also what fills these columns.
        findings.append(Finding(
            check="geography.gp_count", severity="violation",
            expected=f"at most {EXPECTED_GP_COUNT} GPs (the LGD roster)",
            actual=str(total),
        ))
    # Every geography column, not a hand-picked subset: listing three of the
    # six by name let a table with 100% NULL district_code and state_code pass
    # this check clean, which is the exact condition #61 is named for.
    predicate = " OR ".join(f"{col} IS NULL" for col in GEOGRAPHY_COLUMNS)
    blank = con.execute(
        f"SELECT count(*) FROM {table} WHERE {predicate}"
    ).fetchone()[0]
    if blank:
        findings.append(Finding(
            check="geography.populated", severity="violation",
            expected=f"every row has all of {', '.join(GEOGRAPHY_COLUMNS)}",
            actual=f"{blank} row(s) blank",
        ))
    for name, column, expected in (
        ("districts", "zp_name", EXPECTED_DISTRICT_COUNT),
        ("blocks", "block_code", EXPECTED_BLOCK_COUNT),
    ):
        actual = con.execute(
            f"SELECT count(DISTINCT {column}) FROM {table} WHERE {column} IS NOT NULL"
        ).fetchone()[0]
        if not int(expected * MIN_GP_COVERAGE) <= actual <= expected:
            findings.append(Finding(
                check=f"geography.{name}", severity="violation",
                expected=f"{int(expected * MIN_GP_COVERAGE)}-{expected}",
                actual=str(actual), detail=f"distinct {column}",
            ))
    return findings


def check_gp_profile_completeness(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    """gp_profile carries real demographics for the whole state (#123).

    The artifact-level counterpart to ``transform.gp_profile``'s
    ``EmptyRequiredColumn``, and not a duplicate of it. That one runs while
    the table is being built, so it protects nothing about an artifact built
    somewhere else, built by an older version, or hand-patched -- and this
    script is what decides such an artifact is deployable.

    Row count alone is exactly the mistake #61 was: 6,710 keys with every
    measure NULL is the right count and no data. Checked the same three ways
    geography is -- columns present, every row populated, count within the
    roster -- plus non-negativity, because ``clean.to_int`` will happily carry
    a -1 through and a negative population is not a number anyone observed.

    Coverage, not equality: 84 of the 6,794 GPs have no profile upstream at
    all, so demanding one row per GP would refuse a complete build.
    """

    table = GP_PROFILE_TABLE
    if table not in _existing_tables(con):
        return []  # already reported by check_table_existence
    columns = _columns(con, table)
    missing = tuple(col for col in GP_PROFILE_MEASURES if col not in columns)
    if missing:
        return [Finding(
            check="gp_profile.columns", severity="violation",
            expected=f"{table} has {list(GP_PROFILE_MEASURES)}",
            actual=f"missing {list(missing)}",
        )]

    findings: list[Finding] = []
    total = _row_count(con, table)
    floor = int(EXPECTED_GP_COUNT * MIN_GP_COVERAGE)
    if not floor <= total <= EXPECTED_GP_COUNT:
        findings.append(Finding(
            check="gp_profile.gp_count", severity="violation",
            expected=f"between {floor} and {EXPECTED_GP_COUNT} rows",
            actual=str(total),
            detail="a build well short of the roster is a sample, not the state",
        ))
    blank = con.execute(
        f"SELECT count(*) FROM {table} WHERE "
        + " OR ".join(f"{col} IS NULL" for col in GP_PROFILE_MEASURES)
    ).fetchone()[0]
    if blank:
        findings.append(Finding(
            check="gp_profile.populated", severity="violation",
            expected=f"every row has all of {', '.join(GP_PROFILE_MEASURES)}",
            actual=f"{blank} row(s) with a null measure",
        ))
    negative = con.execute(
        f"SELECT count(*) FROM {table} WHERE "
        + " OR ".join(f"{col} < 0" for col in GP_PROFILE_MEASURES)
    ).fetchone()[0]
    if negative:
        findings.append(Finding(
            check="gp_profile.non_negative", severity="violation",
            expected="no negative population or household count",
            actual=f"{negative} row(s) with a negative measure",
        ))
    return findings


def check_reconciliation_totals(
    con: duckdb.DuckDBPyConnection, exempt: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Section 6: the exact published totals from the reference build.

    Skipped entirely by the caller (via ``check_conformance``'s
    ``skip_reconciliation``) when the database under test is a synthetic
    fixture rather than the real build.

    ``exempt`` names individual targets to leave unchecked, for a total whose
    source is known to be incomplete. It exists so that "one total cannot be
    hit yet" stops meaning "check none of them" -- which is how a defect that
    silently doubled activity_expenditure could have reached a green build
    and a green conformance run (#175).

    An exemption is never silent: it reports what it did not check and what
    the target was, so the report says so rather than simply looking clean.
    """

    findings: list[Finding] = []
    actual_tables = _existing_tables(con)
    for check_name, table, column, expected in RECONCILIATION_TARGETS:
        if check_name in exempt:
            findings.append(Finding(
                check=check_name, severity="informational",
                expected=str(expected), actual="not checked",
                detail="exempted for this run; the target is recorded, not asserted",
            ))
            continue
        if table not in actual_tables:
            continue  # already reported by check_table_existence
        if column not in _columns(con, table):
            findings.append(Finding(
                check=check_name, severity="violation",
                expected=f"column {table}.{column} exists", actual="column not found",
            ))
            continue
        actual = _decimal_sum(con, table, column)
        if actual != expected:
            delta = actual - expected
            findings.append(Finding(
                check=check_name, severity="violation",
                expected=str(expected), actual=str(actual),
                detail=f"delta = {delta} (to the paisa)",
            ))
    return findings


# ---------------------------------------------------------------------------
# Aggregation and reporting.
# ---------------------------------------------------------------------------

ALL_CHECKS = (
    check_table_existence,
    check_primary_keys,
    check_voucher_unique_constraint,
    check_activity_expenditure_fk_not_enforced,
    check_required_foreign_keys,
    check_activity_voucher_nullable,
    check_business_key_types,
    check_surrogate_key_types,
    check_money_types,
    check_beneficiary_count_type,
    check_satellite_row_parity,
    check_voucher_direction_values,
    check_activity_nsap_empty,
    check_fiscal_year_format,
    check_dim_code_uniqueness,
)


def check_conformance(
    con: duckdb.DuckDBPyConnection, *,
    skip_reconciliation: bool = False, skip_geography: bool = False,
    skip_derived: bool = False, exempt_reconciliation: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Run every check and return the combined findings, in a stable order.

    Two independent opt-outs, because they are skipped for different reasons
    and folding them into one hid a hole: `voucher` and `dim_code` have no
    loader yet (#46, #48, #129), so every real build today must skip the
    reconciliation totals -- and while geography rode that same flag, no real
    build ever asserted its own scale. A 20-GP build reported "PASS".

    ``skip_reconciliation`` omits the exact published totals; prefer
    ``exempt_reconciliation`` on a real build, which omits named targets only
    and reports each one it skipped (#175).
    ``skip_geography`` omits full-state coverage -- geography AND gp_profile
    demographics, which are the same question about two tables; use it for
    synthetic
    fixtures, which are not wrong for holding three GPs.
    ``skip_derived`` omits the consumer relations (#51), for the same
    reason: a fixture exercising a schema rule need not materialise them.
    """

    findings: list[Finding] = []
    for check in ALL_CHECKS:
        findings.extend(check(con))
    if not skip_derived:
        findings.extend(check_derived_relations(con))
    if not skip_geography:
        # Both are full-state coverage checks and are skipped together: a
        # synthetic fixture holding three GPs is not wrong for having three
        # districts, and it is not wrong for having three profiles either.
        # The flag keeps its name because that is what the CLI and the
        # Makefile pass; what it means is "this is not the state".
        findings.extend(check_geography_completeness(con))
        findings.extend(check_gp_profile_completeness(con))
    if not skip_reconciliation:
        findings.extend(check_reconciliation_totals(con, exempt_reconciliation))
    return findings


def format_report(findings: list[Finding]) -> str:
    """A human-readable report, violations first, grouped from informational."""

    if not findings:
        return "PASS: no deviations from spec found."

    violations = [f for f in findings if f.severity == "violation"]
    informational = [f for f in findings if f.severity == "informational"]

    lines: list[str] = []
    if violations:
        lines.append(f"{len(violations)} VIOLATION(S):")
        lines.extend(f"  {f}" for f in violations)
    else:
        lines.append("No violations found.")
    if informational:
        lines.append("")
        lines.append(f"{len(informational)} informational note(s):")
        lines.extend(f"  {f}" for f in informational)
    return "\n".join(lines)


def has_violations(findings: list[Finding]) -> bool:
    return any(f.severity == "violation" for f in findings)
