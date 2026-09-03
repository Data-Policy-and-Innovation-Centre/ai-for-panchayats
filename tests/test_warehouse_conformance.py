"""Tests for the spec conformance checker (``warehouse.conformance``).

Every check gets one small synthetic fixture that PASSES and one
deliberately-broken fixture that FAILS with the specific finding the check
is supposed to produce. A check whose failure path is never exercised here
is not trusted -- see the module docstring in ``conformance.py``.

All fixtures are built directly with DuckDB DDL in ``tmp_path`` -- nothing
here touches ``data/`` or imports ``warehouse.schema``/``warehouse.transform``,
matching the checker's own independence from those modules.
"""

from __future__ import annotations

from decimal import Decimal

import duckdb

from warehouse.conformance import (
    EXPECTED_ACTIVITY_EXPENDITURE_TOTAL,
    EXPECTED_PLANNED_COST_TOTAL,
    EXPECTED_VOUCHER_AMOUNT_TOTAL,
    check_activity_expenditure_fk_not_enforced,
    check_activity_nsap_empty,
    check_activity_voucher_nullable,
    check_beneficiary_count_type,
    check_business_key_types,
    check_conformance,
    check_dim_code_uniqueness,
    check_fiscal_year_format,
    check_money_types,
    check_primary_keys,
    check_reconciliation_totals,
    check_required_foreign_keys,
    check_satellite_row_parity,
    check_surrogate_key_types,
    check_table_existence,
    check_voucher_direction_values,
    check_voucher_unique_constraint,
    format_report,
    has_violations,
)

# A minimal but fully-conformant baseline: all 19 spec tables, correct PKs,
# correct types, correct constraints, no rows. Individual tests mutate one
# piece of this to prove the corresponding check fires.
FULL_DDL: dict[str, str] = {
    "gram_panchayat": """
        CREATE TABLE gram_panchayat (gp_lgd_code VARCHAR PRIMARY KEY, gp_name VARCHAR)
    """,
    "plan": """
        CREATE TABLE plan (
            plan_code VARCHAR PRIMARY KEY, gp_lgd_code VARCHAR, fiscal_year VARCHAR,
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )
    """,
    "planned_activity": """
        CREATE TABLE planned_activity (
            activity_code VARCHAR PRIMARY KEY, plan_code VARCHAR, total_cost DOUBLE,
            gp_lgd_code VARCHAR,
            FOREIGN KEY (plan_code) REFERENCES plan (plan_code),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )
    """,
    "activity_delegation": (
        "CREATE TABLE activity_delegation (activity_code VARCHAR PRIMARY KEY, "
        "FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code))"
    ),
    "activity_asset": (
        "CREATE TABLE activity_asset (activity_code VARCHAR PRIMARY KEY, "
        "FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code))"
    ),
    "activity_fund": (
        "CREATE TABLE activity_fund (activity_code VARCHAR PRIMARY KEY, "
        "FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code))"
    ),
    "activity_training": (
        "CREATE TABLE activity_training (activity_code VARCHAR PRIMARY KEY, "
        "FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code))"
    ),
    "activity_community_service": (
        "CREATE TABLE activity_community_service (activity_code VARCHAR PRIMARY KEY, "
        "FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code))"
    ),
    "activity_nsap": """
        CREATE TABLE activity_nsap (
            nsap_id INTEGER PRIMARY KEY, activity_code VARCHAR, beneficiary_count INTEGER,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )
    """,
    "activity_expenditure": """
        CREATE TABLE activity_expenditure (
            expenditure_id INTEGER PRIMARY KEY,
            activity_code VARCHAR,
            total_expenditure DECIMAL(16,2),
            gp_lgd_code VARCHAR,
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )
    """,
    "voucher": """
        CREATE TABLE voucher (
            voucher_pk INTEGER PRIMARY KEY,
            gp_lgd_code VARCHAR,
            fiscal_year VARCHAR,
            voucher_no VARCHAR,
            amount DECIMAL(16,2),
            direction VARCHAR,
            UNIQUE (gp_lgd_code, fiscal_year, voucher_no),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )
    """,
    "activity_voucher": """
        CREATE TABLE activity_voucher (
            expenditure_id INTEGER, voucher_pk INTEGER, activity_code VARCHAR,
            FOREIGN KEY (expenditure_id) REFERENCES activity_expenditure (expenditure_id),
            FOREIGN KEY (voucher_pk) REFERENCES voucher (voucher_pk)
        )
    """,
    "admin_approval": """
        CREATE TABLE admin_approval (
            row_id INTEGER PRIMARY KEY, activity_code VARCHAR, gp_lgd_code VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )
    """,
    "admin_approval_scheme": """
        CREATE TABLE admin_approval_scheme (
            row_id INTEGER PRIMARY KEY, parent_row_id INTEGER,
            FOREIGN KEY (parent_row_id) REFERENCES admin_approval (row_id)
        )
    """,
    "technical_approval": """
        CREATE TABLE technical_approval (
            row_id INTEGER PRIMARY KEY, activity_code VARCHAR, gp_lgd_code VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )
    """,
    "physical_progress": """
        CREATE TABLE physical_progress (
            row_id INTEGER PRIMARY KEY, activity_code VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )
    """,
    "dim_code": "CREATE TABLE dim_code (variable VARCHAR, code VARCHAR, PRIMARY KEY (variable, code))",
    "dim_welfare_scheme": "CREATE TABLE dim_welfare_scheme (scheme_code VARCHAR PRIMARY KEY)",
    "dim_lsdg_theme": "CREATE TABLE dim_lsdg_theme (theme_code VARCHAR)",
}


def _connect(tmp_path, name: str = "warehouse.duckdb") -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(tmp_path / name))


def build_full_schema(con: duckdb.DuckDBPyConnection) -> None:
    for ddl in FULL_DDL.values():
        con.execute(ddl)


# DuckDB has no DROP TABLE ... CASCADE for FK dependents, so a test that
# replaces one FULL_DDL table with a deliberately-broken variant has to
# clear out whatever now-added FK depends on it first. Direct dependents
# only; _drop_with_dependents recurses for chains (gram_panchayat ->
# planned_activity -> activity_delegation, etc).
_FK_DEPENDENTS: dict[str, tuple[str, ...]] = {
    "gram_panchayat": (
        "plan", "planned_activity", "activity_expenditure", "voucher",
        "admin_approval", "technical_approval",
    ),
    "plan": ("planned_activity",),
    "planned_activity": (
        "activity_delegation", "activity_asset", "activity_fund", "activity_training",
        "activity_community_service", "activity_nsap", "admin_approval",
        "technical_approval", "physical_progress",
    ),
    "voucher": ("activity_voucher",),
    "activity_expenditure": ("activity_voucher",),
    "admin_approval": ("admin_approval_scheme",),
}


def _drop_with_dependents(con: duckdb.DuckDBPyConnection, table: str) -> None:
    """Drop ``table``, first dropping every table whose FK depends on it.

    The dependents are simply gone afterward (not recreated) -- fine for
    tests that only assert on one specific check function, which already
    skips any table it doesn't find.
    """

    for dependent in _FK_DEPENDENTS.get(table, ()):
        _drop_with_dependents(con, dependent)
    con.execute(f"DROP TABLE IF EXISTS {table}")


# ---------------------------------------------------------------------------
# Section 1: table existence
# ---------------------------------------------------------------------------

def test_table_existence_passes_on_conformant_schema(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    assert check_table_existence(con) == []
    con.close()


def test_table_existence_flags_missing_table(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("DROP TABLE dim_lsdg_theme")
    findings = check_table_existence(con)
    assert any(
        f.check == "tables.missing" and "dim_lsdg_theme" in f.expected for f in findings
    )
    con.close()


def test_table_existence_flags_unexpected_extra_table(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("CREATE TABLE surprise_table (x INTEGER)")
    findings = check_table_existence(con)
    assert any(
        f.check == "tables.unexpected" and "surprise_table" in f.actual for f in findings
    )
    con.close()


def test_table_existence_reports_quarantine_as_informational_only(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("CREATE TABLE quarantine (reason VARCHAR)")
    findings = check_table_existence(con)
    assert not has_violations(findings)
    assert any(f.check == "tables.internal" and f.severity == "informational" for f in findings)
    con.close()


# ---------------------------------------------------------------------------
# Section 2: primary keys
# ---------------------------------------------------------------------------

def test_primary_keys_pass_on_conformant_schema(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    assert check_primary_keys(con) == []
    con.close()


def test_primary_keys_flag_wrong_pk_column(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    _drop_with_dependents(con, "voucher")
    con.execute("""
        CREATE TABLE voucher (
            voucher_pk INTEGER, gp_lgd_code VARCHAR, fiscal_year VARCHAR,
            voucher_no VARCHAR PRIMARY KEY, amount DECIMAL(16,2), direction VARCHAR
        )
    """)
    findings = check_primary_keys(con)
    assert any(f.check == "primary_key.voucher" for f in findings)
    con.close()


def test_primary_keys_flag_unexpected_pk_on_activity_voucher(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("DROP TABLE activity_voucher")
    con.execute("CREATE TABLE activity_voucher (voucher_pk INTEGER PRIMARY KEY, activity_code VARCHAR)")
    findings = check_primary_keys(con)
    assert any(
        f.check == "primary_key.activity_voucher" and f.expected == "no primary key"
        for f in findings
    )
    con.close()


# ---------------------------------------------------------------------------
# Section 3: constraints
# ---------------------------------------------------------------------------

def test_voucher_unique_constraint_passes_on_conformant_schema(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    assert check_voucher_unique_constraint(con) == []
    con.close()


def test_voucher_unique_constraint_flags_missing_unique(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    _drop_with_dependents(con, "voucher")
    con.execute("""
        CREATE TABLE voucher (
            voucher_pk INTEGER PRIMARY KEY, gp_lgd_code VARCHAR, fiscal_year VARCHAR,
            voucher_no VARCHAR, amount DECIMAL(16,2), direction VARCHAR
        )
    """)
    findings = check_voucher_unique_constraint(con)
    assert len(findings) == 1
    assert findings[0].check == "constraint.voucher_unique"
    assert findings[0].severity == "violation"
    con.close()


def test_activity_expenditure_fk_check_passes_when_not_enforced(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    assert check_activity_expenditure_fk_not_enforced(con) == []
    con.close()


def test_activity_expenditure_fk_check_flags_enforced_fk(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    _drop_with_dependents(con, "activity_expenditure")
    con.execute("""
        CREATE TABLE activity_expenditure (
            expenditure_id INTEGER PRIMARY KEY,
            activity_code VARCHAR,
            total_expenditure DECIMAL(16,2),
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )
    """)
    findings = check_activity_expenditure_fk_not_enforced(con)
    assert len(findings) == 1
    assert findings[0].check == "constraint.activity_expenditure_fk_not_enforced"
    assert findings[0].severity == "violation"
    con.close()


def test_required_foreign_keys_pass_on_conformant_schema(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    assert check_required_foreign_keys(con) == []
    con.close()


def test_required_foreign_keys_flag_missing_fk(tmp_path):
    """Before this check existed, a warehouse could drop every *required*
    FK -- e.g. planned_activity.plan_code -> plan below -- and still pass
    conformance, because the only FK check that existed
    (``check_activity_expenditure_fk_not_enforced``) verifies the single
    *forbidden* relationship, never a required one. Recreating
    planned_activity here with only the gp_lgd_code FK (dropping
    plan_code's) reproduces exactly that deviation and must now be
    rejected.
    """

    con = _connect(tmp_path)
    build_full_schema(con)
    _drop_with_dependents(con, "planned_activity")
    con.execute("""
        CREATE TABLE planned_activity (
            activity_code VARCHAR PRIMARY KEY, plan_code VARCHAR, total_cost DOUBLE,
            gp_lgd_code VARCHAR,
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )
    """)
    findings = check_required_foreign_keys(con)
    assert any(
        f.check == "constraint.foreign_key.planned_activity.plan_code" and f.severity == "violation"
        for f in findings
    )
    con.close()


def test_activity_voucher_nullable_passes_on_conformant_schema(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    assert check_activity_voucher_nullable(con) == []
    con.close()


def test_activity_voucher_nullable_flags_not_null(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("DROP TABLE activity_voucher")
    con.execute("CREATE TABLE activity_voucher (voucher_pk INTEGER NOT NULL, activity_code VARCHAR)")
    findings = check_activity_voucher_nullable(con)
    assert len(findings) == 1
    assert findings[0].check == "constraint.activity_voucher_nullable"
    assert findings[0].severity == "violation"
    con.close()


# ---------------------------------------------------------------------------
# Section 4: types
# ---------------------------------------------------------------------------

def test_business_key_types_pass_on_conformant_schema(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    assert check_business_key_types(con) == []
    con.close()


def test_business_key_types_flag_integer_gp_lgd_code(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    _drop_with_dependents(con, "gram_panchayat")
    con.execute("CREATE TABLE gram_panchayat (gp_lgd_code INTEGER PRIMARY KEY, gp_name VARCHAR)")
    findings = check_business_key_types(con)
    assert any(
        f.check == "type.business_key.gram_panchayat.gp_lgd_code" and f.actual == "INTEGER"
        for f in findings
    )
    con.close()


def test_surrogate_key_types_pass_on_conformant_schema(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    assert check_surrogate_key_types(con) == []
    con.close()


def test_surrogate_key_types_flag_varchar_expenditure_id(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    _drop_with_dependents(con, "activity_expenditure")
    con.execute("""
        CREATE TABLE activity_expenditure (
            expenditure_id VARCHAR PRIMARY KEY, activity_code VARCHAR, total_expenditure DECIMAL(16,2)
        )
    """)
    findings = check_surrogate_key_types(con)
    assert any(
        f.check == "type.surrogate_key.activity_expenditure.expenditure_id" and f.actual == "VARCHAR"
        for f in findings
    )
    con.close()


def test_money_types_pass_on_conformant_schema(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    assert check_money_types(con) == []
    con.close()


def test_money_types_flag_double_expenditure_amount(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    _drop_with_dependents(con, "activity_expenditure")
    con.execute("""
        CREATE TABLE activity_expenditure (
            expenditure_id INTEGER PRIMARY KEY, activity_code VARCHAR, total_expenditure DOUBLE
        )
    """)
    findings = check_money_types(con)
    assert any(
        f.check == "type.money.activity_expenditure.total_expenditure"
        and f.expected == "DECIMAL(16,2)" and f.actual == "DOUBLE"
        for f in findings
    )
    con.close()


def test_money_types_flag_decimal_planned_cost(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    _drop_with_dependents(con, "planned_activity")
    con.execute("""
        CREATE TABLE planned_activity (
            activity_code VARCHAR PRIMARY KEY, plan_code VARCHAR, total_cost DECIMAL(16,2)
        )
    """)
    findings = check_money_types(con)
    assert any(
        f.check == "type.money.planned_activity.total_cost"
        and f.expected == "DOUBLE" and f.actual == "DECIMAL(16,2)"
        for f in findings
    )
    con.close()


def test_money_types_flags_missing_required_column(tmp_path):
    """A voucher table with no ``amount`` column at all previously PASSED
    this check: ``if column not in columns: continue`` silently skipped
    the type comparison rather than reporting the column's absence. A
    warehouse structurally missing a required money column must be
    rejected before its type is ever inspected.
    """

    con = _connect(tmp_path)
    build_full_schema(con)
    _drop_with_dependents(con, "voucher")
    con.execute("""
        CREATE TABLE voucher (
            voucher_pk INTEGER PRIMARY KEY, gp_lgd_code VARCHAR, fiscal_year VARCHAR,
            voucher_no VARCHAR, direction VARCHAR,
            UNIQUE (gp_lgd_code, fiscal_year, voucher_no)
        )
    """)
    findings = check_money_types(con)
    assert any(
        f.check == "type.money.voucher.amount" and f.actual == "column not found"
        and f.severity == "violation"
        for f in findings
    )
    con.close()


def test_beneficiary_count_type_passes_on_conformant_schema(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    assert check_beneficiary_count_type(con) == []
    con.close()


def test_beneficiary_count_type_flags_decimal(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("DROP TABLE activity_nsap")
    con.execute("""
        CREATE TABLE activity_nsap (
            nsap_id INTEGER PRIMARY KEY, activity_code VARCHAR, beneficiary_count DECIMAL(16,2)
        )
    """)
    findings = check_beneficiary_count_type(con)
    assert len(findings) == 1
    assert findings[0].check == "type.beneficiary_count"
    assert findings[0].severity == "violation"
    con.close()


def test_beneficiary_count_type_flags_missing_required_column(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("DROP TABLE activity_nsap")
    con.execute("CREATE TABLE activity_nsap (nsap_id INTEGER PRIMARY KEY, activity_code VARCHAR)")
    findings = check_beneficiary_count_type(con)
    assert len(findings) == 1
    assert findings[0].check == "type.beneficiary_count"
    assert findings[0].actual == "column not found"
    assert findings[0].severity == "violation"
    con.close()


# ---------------------------------------------------------------------------
# Section 5: data-level invariants
# ---------------------------------------------------------------------------

def test_satellite_row_parity_passes_when_empty(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    assert check_satellite_row_parity(con) == []
    con.close()


def test_satellite_row_parity_passes_when_matched(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("INSERT INTO planned_activity VALUES ('A1', NULL, 10.0, NULL), ('A2', NULL, 20.0, NULL)")
    for table in (
        "activity_delegation", "activity_asset", "activity_fund",
        "activity_training", "activity_community_service",
    ):
        con.execute(f"INSERT INTO {table} VALUES ('A1'), ('A2')")
    assert check_satellite_row_parity(con) == []
    con.close()


def test_satellite_row_parity_flags_orphan_and_missing_rows(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("INSERT INTO planned_activity VALUES ('A1', NULL, 10.0, NULL), ('A2', NULL, 20.0, NULL)")
    # A real enforced FK would reject 'ORPHAN' outright; recreate
    # activity_delegation without it here so this test can exercise
    # check_satellite_row_parity's own orphan-detection in isolation, as
    # defense-in-depth for a build where the FK constraint itself was
    # (wrongly) not enforced -- exactly the deviation
    # check_required_foreign_keys catches separately.
    con.execute("DROP TABLE activity_delegation")
    con.execute("CREATE TABLE activity_delegation (activity_code VARCHAR PRIMARY KEY)")
    # A1 has no delegation row (missing); ORPHAN has one with no parent.
    con.execute("INSERT INTO activity_delegation VALUES ('A2'), ('ORPHAN')")
    findings = check_satellite_row_parity(con)
    checks_fired = {f.check for f in findings}
    assert "data.satellite_orphans.activity_delegation" in checks_fired
    assert "data.satellite_missing.activity_delegation" in checks_fired
    assert all(f.severity == "violation" for f in findings)
    con.close()


def test_voucher_direction_passes_on_valid_values(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("INSERT INTO gram_panchayat VALUES ('GP1', 'Test GP')")
    con.execute("""
        INSERT INTO voucher VALUES
            (1, 'GP1', '2025-2026', 'V1', 100.00, 'payment'),
            (2, 'GP1', '2025-2026', 'V2', 50.00, 'receipt')
    """)
    assert check_voucher_direction_values(con) == []
    con.close()


def test_voucher_direction_flags_invalid_value(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("INSERT INTO gram_panchayat VALUES ('GP1', 'Test GP')")
    con.execute("""
        INSERT INTO voucher VALUES (1, 'GP1', '2025-2026', 'V1', 100.00, 'refund')
    """)
    findings = check_voucher_direction_values(con)
    assert len(findings) == 1
    assert findings[0].check == "data.voucher_direction"
    assert findings[0].severity == "violation"
    con.close()


def test_activity_nsap_empty_passes_when_empty(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    assert check_activity_nsap_empty(con) == []
    con.close()


def test_activity_nsap_empty_reports_informational_when_populated(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("INSERT INTO activity_nsap VALUES (1, NULL, 5)")
    findings = check_activity_nsap_empty(con)
    assert len(findings) == 1
    assert findings[0].check == "data.activity_nsap_empty"
    assert findings[0].severity == "informational"
    con.close()


def test_fiscal_year_format_passes_on_valid_values(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("INSERT INTO plan VALUES ('P1', NULL, '2025-2026')")
    assert check_fiscal_year_format(con) == []
    con.close()


def test_fiscal_year_format_flags_short_form(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("INSERT INTO plan VALUES ('P1', NULL, '2025-26')")
    findings = check_fiscal_year_format(con)
    assert any(f.check == "data.fiscal_year_format.plan" and f.severity == "violation" for f in findings)
    con.close()


def test_dim_code_uniqueness_passes_on_unique_pairs(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("INSERT INTO dim_code VALUES ('var_a', 'code_1'), ('var_a', 'code_2')")
    assert check_dim_code_uniqueness(con) == []
    con.close()


def test_dim_code_uniqueness_flags_duplicate_pair(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    # A real conformant dim_code has a PRIMARY KEY that would itself reject
    # this insert; drop it here to exercise the data-level check in
    # isolation, as defense-in-depth for a build where that constraint was
    # (wrongly) not enforced -- exactly the deviation check_primary_keys
    # catches separately.
    con.execute("DROP TABLE dim_code")
    con.execute("CREATE TABLE dim_code (variable VARCHAR, code VARCHAR)")
    con.execute("INSERT INTO dim_code VALUES ('var_a', 'code_1'), ('var_a', 'code_1')")
    findings = check_dim_code_uniqueness(con)
    assert len(findings) == 1
    assert findings[0].check == "data.dim_code_uniqueness"
    assert findings[0].severity == "violation"
    con.close()


# ---------------------------------------------------------------------------
# Section 6: reconciliation totals
# ---------------------------------------------------------------------------

def test_reconciliation_totals_pass_when_exact(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("INSERT INTO gram_panchayat VALUES ('GP1', 'Test GP')")
    con.execute(
        "INSERT INTO voucher VALUES (1, 'GP1', '2025-2026', 'V1', ?, 'payment')",
        [EXPECTED_VOUCHER_AMOUNT_TOTAL],
    )
    con.execute(
        "INSERT INTO activity_expenditure VALUES (1, 'A1', ?, NULL)",
        [EXPECTED_ACTIVITY_EXPENDITURE_TOTAL],
    )
    con.execute(
        "INSERT INTO planned_activity VALUES ('A1', NULL, ?, NULL)",
        [float(EXPECTED_PLANNED_COST_TOTAL)],
    )
    assert check_reconciliation_totals(con) == []
    con.close()


def test_reconciliation_totals_flag_delta_to_the_paisa(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("INSERT INTO gram_panchayat VALUES ('GP1', 'Test GP')")
    off_by = EXPECTED_VOUCHER_AMOUNT_TOTAL + Decimal("0.01")
    con.execute(
        "INSERT INTO voucher VALUES (1, 'GP1', '2025-2026', 'V1', ?, 'payment')",
        [off_by],
    )
    con.execute(
        "INSERT INTO activity_expenditure VALUES (1, 'A1', ?, NULL)",
        [EXPECTED_ACTIVITY_EXPENDITURE_TOTAL],
    )
    con.execute(
        "INSERT INTO planned_activity VALUES ('A1', NULL, ?, NULL)",
        [float(EXPECTED_PLANNED_COST_TOTAL)],
    )
    findings = check_reconciliation_totals(con)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.check == "reconciliation.voucher_amount_total"
    assert finding.severity == "violation"
    assert "0.01" in finding.detail
    con.close()


def test_reconciliation_skippable_for_synthetic_fixtures(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    # No voucher/expenditure/planned-cost rows inserted at all -- would
    # normally fail every reconciliation total (0.00 != the real totals).
    findings = check_conformance(con, skip_reconciliation=True)
    assert not any(f.check.startswith("reconciliation.") for f in findings)
    con.close()


def test_reconciliation_included_by_default_and_fails_on_empty_fixture(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    findings = check_conformance(con, skip_reconciliation=False)
    reconciliation_findings = [f for f in findings if f.check.startswith("reconciliation.")]
    assert len(reconciliation_findings) == 3
    assert all(f.severity == "violation" for f in reconciliation_findings)
    con.close()


# ---------------------------------------------------------------------------
# Aggregation and reporting
# ---------------------------------------------------------------------------

def test_check_conformance_passes_on_fully_conformant_populated_fixture(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("INSERT INTO gram_panchayat VALUES ('GP1', 'Test GP')")
    con.execute(
        "INSERT INTO voucher VALUES (1, 'GP1', '2025-2026', 'V1', ?, 'payment')",
        [EXPECTED_VOUCHER_AMOUNT_TOTAL],
    )
    con.execute(
        "INSERT INTO activity_expenditure VALUES (1, 'A1', ?, NULL)",
        [EXPECTED_ACTIVITY_EXPENDITURE_TOTAL],
    )
    con.execute(
        "INSERT INTO planned_activity VALUES ('A1', NULL, ?, NULL)",
        [float(EXPECTED_PLANNED_COST_TOTAL)],
    )
    con.execute("INSERT INTO activity_delegation VALUES ('A1')")
    con.execute("INSERT INTO activity_asset VALUES ('A1')")
    con.execute("INSERT INTO activity_fund VALUES ('A1')")
    con.execute("INSERT INTO activity_training VALUES ('A1')")
    con.execute("INSERT INTO activity_community_service VALUES ('A1')")
    findings = check_conformance(con, skip_reconciliation=False)
    assert findings == []
    assert not has_violations(findings)
    assert format_report(findings) == "PASS: no deviations from spec found."
    con.close()


def test_check_conformance_fails_and_reports_on_broken_fixture(tmp_path):
    con = _connect(tmp_path)
    build_full_schema(con)
    con.execute("DROP TABLE dim_welfare_scheme")
    findings = check_conformance(con, skip_reconciliation=True)
    assert has_violations(findings)
    report = format_report(findings)
    assert "VIOLATION" in report
    assert "dim_welfare_scheme" in report
    con.close()


def test_check_conformance_rejects_schema_previously_passed_as_conformant(tmp_path):
    """Reproduces the exact structural gap both Codex findings describe: a
    warehouse missing a required money column (voucher.amount) and missing
    a required FK (planned_activity.plan_code -> plan). Before the fixes
    in this module, ``check_money_types`` silently skipped the absent
    column and no check verified required FKs at all, so
    ``check_conformance`` on this fixture would have returned ``[]`` --
    a PASSING report for a structurally non-conformant warehouse. Both
    gaps are now violations.
    """

    con = _connect(tmp_path)
    build_full_schema(con)
    _drop_with_dependents(con, "voucher")
    con.execute("""
        CREATE TABLE voucher (
            voucher_pk INTEGER PRIMARY KEY, gp_lgd_code VARCHAR, fiscal_year VARCHAR,
            voucher_no VARCHAR, direction VARCHAR,
            UNIQUE (gp_lgd_code, fiscal_year, voucher_no)
        )
    """)
    _drop_with_dependents(con, "planned_activity")
    con.execute("""
        CREATE TABLE planned_activity (
            activity_code VARCHAR PRIMARY KEY, plan_code VARCHAR, total_cost DOUBLE,
            gp_lgd_code VARCHAR,
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )
    """)
    findings = check_conformance(con, skip_reconciliation=True)
    checks_fired = {f.check for f in findings}
    assert "type.money.voucher.amount" in checks_fired
    assert "constraint.foreign_key.planned_activity.plan_code" in checks_fired
    assert has_violations(findings)
    con.close()
