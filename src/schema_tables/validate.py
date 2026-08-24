from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


# ============================================================
# Validation result
# ============================================================

@dataclass
class ValidationResult:
    check: str
    table: str
    passed: bool
    detail: str


class ValidationFailed(Exception):
    """
    Raised when one or more validation checks fail.
    """

    def __init__(self, results: list[ValidationResult]):
        self.results = results

        failures = [
            r
            for r in results
            if not r.passed
        ]

        message = "\n".join(
            f"{r.table}: {r.check} -> {r.detail}"
            for r in failures
        )

        super().__init__(
            f"{len(failures)} validation check(s) failed:\n{message}"
        )


# ============================================================
# Schema definitions
# ============================================================

REQUIRED_COLUMNS = {

    "gram_panchayat": [
        "gp_lgd_code",
        "gp_name",
        "state_code",
        "state_name",
        "district_code",
        "zp_name",
        "block_code",
        "block_name",
    ],

    "plan": [
        "plan_code",
        "gp_lgd_code",
        "fiscal_year",
        "plan_type",
        "approval_date",
        "plan_code_status",
    ],

    "planned_activity": [
        "activity_code",
        "plan_code",
        "gp_lgd_code",
        "fiscal_year",
        "source_file",
        "activity_type",
        "activity_name",
        "activity_desc",
        "focus_area",
        "activity_for",
        "work_type",
        "is_costless_activity",
        "total_cost",
        "operation_type",
        "operation_remarks",
        "output_type",
        "activity_status",
    ],

    "activity_delegation": [
        "activity_code",
        "is_delegated",
        "delegated_unit_code",
        "delegated_unit_type",
        "delegated_unit_level",
        "delegated_unit_category",
        "is_shareable",
        "delegated_parent_unit_code",
    ],

    "activity_asset": [
        "activity_code",
        "main_asset_category",
        "main_asset_subcategory",
        "main_asset_unit_type",
        "main_asset_unit_count",
        "asset_type",
        "asset_category",
        "asset_subcategory",
        "asset_coverage_code",
        "asset_name",
        "asset_unit_type",
        "asset_unit_count",
        "asset_unit_cost",
        "asset_parameter_type",
        "asset_details_raw",
        "asset_loc_code",
        "asset_loc_unit_code",
        "asset_loc_unit_type",
        "asset_loc_unit_count",
        "asset_loc_unit_cost_total",
        "asset_loc_overflow_json",
    ],

    "activity_fund": [
        "activity_code",
        "fund_scheme_code",
        "fund_component_code",
        "fund_tied_general",
        "fund_tied_sc",
        "fund_tied_st",
        "fund_untied_general",
        "fund_untied_sc",
        "fund_untied_st",
        "fund_amount_total",
        "fund_tied_abandoned_general",
        "fund_tied_abandoned_sc",
        "fund_tied_abandoned_st",
        "fund_untied_abandoned_general",
        "fund_untied_abandoned_sc",
        "fund_untied_abandoned_st",
        "fund_overflow_json",
    ],

    "activity_training": [
        "activity_code",
        "training_capacity_raw",
        "training_category_code",
        "training_organiser_code",
        "training_subject",
        "training_trainees_total",
        "training_duration_days",
    ],

    "activity_community_service": [
        "activity_code",
        "community_service_raw",
        "community_service_code",
        "community_service_duration",
        "community_beneficiaries_expected",
    ],

    "activity_nsap": [
        "nsap_id",
        "activity_code",
        "category",
        "age_band",
        "gender",
        "beneficiary_count",
    ],

    "activity_expenditure": [
        "expenditure_id",
        "activity_code",
        "plan_code",
        "gp_lgd_code",
        "fiscal_year",
        "s_no",
        "scheme_name",
        "approved_cost_action_plan",
        "technical_approved_cost",
        "admin_approved_cost",
        "general",
        "sc",
        "st",
        "total_expenditure",
    ],

    "voucher": [
        "voucher_pk",
        "gp_lgd_code",
        "fiscal_year",
        "voucher_no",
        "voucher_id",
        "direction",
        "type",
        "date",
        "month",
        "amount",
    ],

    "activity_voucher": [
        "expenditure_id",
        "voucher_pk",
        "gp_lgd_code",
        "fiscal_year",
        "voucher_no",
        "voucher_date",
        "voucher_cost",
    ],

    "admin_approval": [
        "row_id",
        "gp_lgd_code",
        "gp_name",
        "plan_year",
        "doc_type",
        "source_file",
        "activity_code",
        "work_plan_year",
        "adm_approval_no",
        "adm_approval_sanction_date",
        "work_proposed_cost",
        "adm_approval_authority",
    ],

    "admin_approval_scheme": [
        "row_id",
        "parent_row_id",
        "pos",
        "activity_code",
        "scheme_code",
        "scheme_component_code",
        "fund_sanctioned_general",
        "fund_sanctioned_sc",
        "fund_sanctioned_st",
        "fund_sanctioned_total",
    ],

    "technical_approval": [
        "row_id",
        "gp_lgd_code",
        "gp_name",
        "plan_year",
        "doc_type",
        "source_file",
        "activity_code",
        "tec_approval_required",
        "tec_approval_cost",
        "tec_approval_authority",
        "tec_approval_order_no",
        "tec_approval_order_date",
    ],

    "physical_progress": [
        "row_id",
        "parent_row_id",
        "pos",
        "activity_code",
        "file_upload_id",
        "longitude",
        "latitude",
        "n_coords",
        "longitude_raw",
        "latitude_raw",
        "plan_unit_type_code",
    ],

    "dim_code": [
        "variable",
        "code",
        "description",
        "source",
        "confidence",
    ],

    "dim_welfare_scheme": [
        "scheme_code",
        "scheme_name",
    ],

    "dim_lsdg_theme": [
        "focus_area_name",
        "lsdg_theme",
        "distinct_themes",
        "n_rows",
    ],
}


# ============================================================
# Primary / business keys
# ============================================================

UNIQUE_KEYS = {

    "gram_panchayat": [
        "gp_lgd_code",
    ],

    "plan": [
        "plan_code",
    ],

    "planned_activity": [
        "activity_code",
    ],

    "activity_delegation": [
        "activity_code",
    ],

    "activity_asset": [
        "activity_code",
    ],

    "activity_fund": [
        "activity_code",
    ],

    "activity_training": [
        "activity_code",
    ],

    "activity_community_service": [
        "activity_code",
    ],

    "activity_nsap": [
        "nsap_id",
    ],

    "activity_expenditure": [
        "expenditure_id",
    ],

    "voucher": [
        "gp_lgd_code",
        "fiscal_year",
        "voucher_no",
    ],

    "admin_approval": [
        "row_id",
    ],

    "admin_approval_scheme": [
        "row_id",
    ],

    "technical_approval": [
        "row_id",
    ],

    "physical_progress": [
        "row_id",
    ],

    "dim_code": [
        "variable",
        "code",
    ],

    "dim_welfare_scheme": [
        "scheme_code",
    ],

    "dim_lsdg_theme": [
        "focus_area_name",
    ],
}


# ============================================================
# Generic validation checks
# ============================================================

def check_required_columns(
    table_name: str,
    df: pd.DataFrame,
) -> ValidationResult:

    required = REQUIRED_COLUMNS[table_name]

    missing = [
        col
        for col in required
        if col not in df.columns
    ]

    return ValidationResult(
        check="required_columns",
        table=table_name,
        passed=len(missing) == 0,
        detail=(
            "all required columns present"
            if not missing
            else f"missing: {missing}"
        ),
    )


def check_duplicate_keys(
    table_name: str,
    df: pd.DataFrame,
) -> ValidationResult:

    keys = UNIQUE_KEYS.get(table_name)

    if not keys:
        return ValidationResult(
            check="duplicate_keys",
            table=table_name,
            passed=True,
            detail="no unique key check defined",
        )

    missing = [
        key
        for key in keys
        if key not in df.columns
    ]

    if missing:
        return ValidationResult(
            check="duplicate_keys",
            table=table_name,
            passed=False,
            detail=f"key columns missing: {missing}",
        )

    valid = df.dropna(
        subset=keys,
    )

    duplicate_count = (
        valid
        .duplicated(
            subset=keys,
            keep=False,
        )
        .sum()
    )

    return ValidationResult(
        check="duplicate_keys",
        table=table_name,
        passed=duplicate_count == 0,
        detail=f"{duplicate_count} duplicate row(s)",
    )


def check_null_keys(
    table_name: str,
    df: pd.DataFrame,
) -> ValidationResult:

    keys = UNIQUE_KEYS.get(table_name)

    if not keys:
        return ValidationResult(
            check="null_keys",
            table=table_name,
            passed=True,
            detail="no key defined",
        )

    missing_columns = [
        key
        for key in keys
        if key not in df.columns
    ]

    if missing_columns:
        return ValidationResult(
            check="null_keys",
            table=table_name,
            passed=False,
            detail=f"key columns missing: {missing_columns}",
        )

    null_count = (
        df[keys]
        .isna()
        .any(axis=1)
        .sum()
    )

    return ValidationResult(
        check="null_keys",
        table=table_name,
        passed=null_count == 0,
        detail=f"{null_count} row(s) with null key",
    )


# ============================================================
# Relationship checks
# ============================================================

def check_reference(
    child_name: str,
    child: pd.DataFrame,
    child_column: str,
    parent_name: str,
    parent: pd.DataFrame,
    parent_column: str,
    *,
    allow_null: bool = True,
) -> ValidationResult:

    if child_column not in child.columns:
        return ValidationResult(
            check="foreign_key",
            table=child_name,
            passed=False,
            detail=f"missing child column: {child_column}",
        )

    if parent_column not in parent.columns:
        return ValidationResult(
            check="foreign_key",
            table=child_name,
            passed=False,
            detail=f"missing parent column: {parent_column}",
        )

    child_values = child[child_column]

    if allow_null:
        child_values = child_values.dropna()

    parent_values = set(
        parent[parent_column]
        .dropna()
        .astype(str)
    )

    missing = (
        child_values
        .astype(str)
        .loc[
            ~child_values
            .astype(str)
            .isin(parent_values)
        ]
    )

    missing_unique = missing.unique()

    return ValidationResult(
        check=f"{child_column}->{parent_name}.{parent_column}",
        table=child_name,
        passed=len(missing_unique) == 0,
        detail=(
            "all references valid"
            if len(missing_unique) == 0
            else (
                f"{len(missing_unique)} unmatched value(s); "
                f"examples: {list(missing_unique[:5])}"
            )
        ),
    )


# ============================================================
# Domain-specific checks
# ============================================================

def check_financial_year(
    table_name: str,
    df: pd.DataFrame,
) -> ValidationResult:

    if "fiscal_year" not in df.columns:
        return ValidationResult(
            check="financial_year_format",
            table=table_name,
            passed=True,
            detail="table has no fiscal_year column",
        )

    values = (
        df["fiscal_year"]
        .dropna()
        .astype(str)
    )

    invalid = values[
        ~values.str.match(
            r"^\d{4}-\d{4}$"
        )
    ]

    return ValidationResult(
        check="financial_year_format",
        table=table_name,
        passed=len(invalid) == 0,
        detail=f"{len(invalid)} invalid financial year value(s)",
    )


def check_nonnegative(
    table_name: str,
    df: pd.DataFrame,
    columns: list[str],
) -> list[ValidationResult]:

    results = []

    for column in columns:

        if column not in df.columns:
            continue

        numeric = pd.to_numeric(
            df[column],
            errors="coerce",
        )

        negative_count = (
            numeric < 0
        ).sum()

        results.append(
            ValidationResult(
                check=f"nonnegative_{column}",
                table=table_name,
                passed=negative_count == 0,
                detail=f"{negative_count} negative value(s)",
            )
        )

    return results


def check_coordinates(
    df: pd.DataFrame,
) -> list[ValidationResult]:

    results = []

    if "latitude" in df.columns:

        latitude = pd.to_numeric(
            df["latitude"],
            errors="coerce",
        )

        invalid = (
            latitude.notna()
            &
            ~latitude.between(
                -90,
                90,
            )
        ).sum()

        results.append(
            ValidationResult(
                check="latitude_range",
                table="physical_progress",
                passed=invalid == 0,
                detail=f"{invalid} invalid latitude value(s)",
            )
        )

    if "longitude" in df.columns:

        longitude = pd.to_numeric(
            df["longitude"],
            errors="coerce",
        )

        invalid = (
            longitude.notna()
            &
            ~longitude.between(
                -180,
                180,
            )
        ).sum()

        results.append(
            ValidationResult(
                check="longitude_range",
                table="physical_progress",
                passed=invalid == 0,
                detail=f"{invalid} invalid longitude value(s)",
            )
        )

    return results


# ============================================================
# Full validation
# ============================================================

def validate_tables(
    tables: dict[str, pd.DataFrame],
) -> list[ValidationResult]:
    """
    Validate all generated schema tables.

    Parameters
    ----------
    tables:
        Dictionary such as:

        {
            "gram_panchayat": df,
            "plan": df,
            ...
        }
    """

    results: list[ValidationResult] = []

    # --------------------------------------------------------
    # Confirm all 19 tables exist
    # --------------------------------------------------------

    expected_tables = set(REQUIRED_COLUMNS)
    actual_tables = set(tables)

    missing_tables = sorted(
        expected_tables - actual_tables
    )

    results.append(
        ValidationResult(
            check="all_schema_tables_present",
            table="schema",
            passed=len(missing_tables) == 0,
            detail=(
                "all 19 schema tables present"
                if not missing_tables
                else f"missing tables: {missing_tables}"
            ),
        )
    )

    # --------------------------------------------------------
    # Individual table checks
    # --------------------------------------------------------

    for table_name in expected_tables:

        if table_name not in tables:
            continue

        df = tables[table_name]

        results.append(
            check_required_columns(
                table_name,
                df,
            )
        )

        results.append(
            check_duplicate_keys(
                table_name,
                df,
            )
        )

        results.append(
            check_null_keys(
                table_name,
                df,
            )
        )

        results.append(
            check_financial_year(
                table_name,
                df,
            )
        )

    # --------------------------------------------------------
    # Relationship checks
    # --------------------------------------------------------

    gp = tables.get("gram_panchayat")
    plan = tables.get("plan")
    activity = tables.get("planned_activity")

    # plan -> gram_panchayat
    if plan is not None and gp is not None:

        results.append(
            check_reference(
                "plan",
                plan,
                "gp_lgd_code",
                "gram_panchayat",
                gp,
                "gp_lgd_code",
                allow_null=False,
            )
        )

    # planned_activity -> plan
    if activity is not None and plan is not None:

        results.append(
            check_reference(
                "planned_activity",
                activity,
                "plan_code",
                "plan",
                plan,
                "plan_code",
                allow_null=False,
            )
        )

    # planned_activity -> gram_panchayat
    if activity is not None and gp is not None:

        results.append(
            check_reference(
                "planned_activity",
                activity,
                "gp_lgd_code",
                "gram_panchayat",
                gp,
                "gp_lgd_code",
                allow_null=False,
            )
        )

    # --------------------------------------------------------
    # Activity satellite tables -> planned_activity
    # --------------------------------------------------------

    activity_child_tables = [
        "activity_delegation",
        "activity_asset",
        "activity_fund",
        "activity_training",
        "activity_community_service",
        "activity_nsap",
        "activity_expenditure",
        "admin_approval",
        "admin_approval_scheme",
        "technical_approval",
        "physical_progress",
    ]

    if activity is not None:

        for table_name in activity_child_tables:

            child = tables.get(table_name)

            if child is None:
                continue

            results.append(
                check_reference(
                    table_name,
                    child,
                    "activity_code",
                    "planned_activity",
                    activity,
                    "activity_code",
                    allow_null=True,
                )
            )

    # --------------------------------------------------------
    # expenditure -> GP
    # --------------------------------------------------------

    expenditure = tables.get(
        "activity_expenditure"
    )

    if (
        expenditure is not None
        and gp is not None
    ):

        results.append(
            check_reference(
                "activity_expenditure",
                expenditure,
                "gp_lgd_code",
                "gram_panchayat",
                gp,
                "gp_lgd_code",
                allow_null=False,
            )
        )

    # --------------------------------------------------------
    # voucher -> GP
    # --------------------------------------------------------

    voucher = tables.get("voucher")

    if (
        voucher is not None
        and gp is not None
    ):

        results.append(
            check_reference(
                "voucher",
                voucher,
                "gp_lgd_code",
                "gram_panchayat",
                gp,
                "gp_lgd_code",
                allow_null=False,
            )
        )

    # --------------------------------------------------------
    # activity_voucher relationships
    # --------------------------------------------------------

    activity_voucher = tables.get(
        "activity_voucher"
    )

    if activity_voucher is not None:

        if expenditure is not None:

            results.append(
                check_reference(
                    "activity_voucher",
                    activity_voucher,
                    "expenditure_id",
                    "activity_expenditure",
                    expenditure,
                    "expenditure_id",
                    allow_null=False,
                )
            )

        if voucher is not None:

            results.append(
                check_reference(
                    "activity_voucher",
                    activity_voucher,
                    "voucher_pk",
                    "voucher",
                    voucher,
                    "voucher_pk",
                    allow_null=True,
                )
            )

    # --------------------------------------------------------
    # Admin approval scheme -> approval
    # --------------------------------------------------------

    admin_scheme = tables.get(
        "admin_approval_scheme"
    )

    admin = tables.get(
        "admin_approval"
    )

    if (
        admin_scheme is not None
        and admin is not None
    ):

        results.append(
            check_reference(
                "admin_approval_scheme",
                admin_scheme,
                "parent_row_id",
                "admin_approval",
                admin,
                "row_id",
                allow_null=True,
            )
        )

    # --------------------------------------------------------
    # Numeric checks
    # --------------------------------------------------------

    if activity is not None:

        results.extend(
            check_nonnegative(
                "planned_activity",
                activity,
                [
                    "total_cost",
                ],
            )
        )

    if expenditure is not None:

        results.extend(
            check_nonnegative(
                "activity_expenditure",
                expenditure,
                [
                    "approved_cost_action_plan",
                    "technical_approved_cost",
                    "admin_approved_cost",
                    "general",
                    "sc",
                    "st",
                    "total_expenditure",
                ],
            )
        )

    activity_fund = tables.get(
        "activity_fund"
    )

    if activity_fund is not None:

        results.extend(
            check_nonnegative(
                "activity_fund",
                activity_fund,
                [
                    "fund_tied_general",
                    "fund_tied_sc",
                    "fund_tied_st",
                    "fund_untied_general",
                    "fund_untied_sc",
                    "fund_untied_st",
                    "fund_amount_total",
                ],
            )
        )

    physical = tables.get(
        "physical_progress"
    )

    if physical is not None:
        results.extend(
            check_coordinates(
                physical
            )
        )

    return results


# ============================================================
# Validation report
# ============================================================

def validation_report(
    results: list[ValidationResult],
) -> pd.DataFrame:
    """
    Convert validation results to a dataframe for saving/export.
    """

    return pd.DataFrame(
        [
            {
                "table": result.table,
                "check": result.check,
                "passed": result.passed,
                "detail": result.detail,
            }
            for result in results
        ]
    )


def raise_if_invalid(
    results: list[ValidationResult],
) -> None:
    """
    Raise ValidationFailed if any check failed.
    """

    failed = [
        result
        for result in results
        if not result.passed
    ]

    if failed:
        raise ValidationFailed(failed)


def print_validation_summary(
    results: list[ValidationResult],
) -> None:
    """
    Print a concise validation summary.
    """

    passed = sum(
        result.passed
        for result in results
    )

    failed = len(results) - passed

    print(
        f"\nValidation: "
        f"{passed}/{len(results)} checks passed"
    )

    if failed:

        print(
            f"{failed} check(s) failed:\n"
        )

        for result in results:

            if not result.passed:
                print(
                    f"  FAIL | "
                    f"{result.table:<28} | "
                    f"{result.check:<35} | "
                    f"{result.detail}"
                )