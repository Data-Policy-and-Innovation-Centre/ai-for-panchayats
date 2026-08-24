from __future__ import annotations

import hashlib
import re

import pandas as pd

from .clean import (
    clean_activity_code,
    clean_amount,
    clean_column_names,
    clean_date,
    clean_identifier,
    clean_integer,
    clean_lgd_code,
    clean_financial_year,
    clean_numeric,
    clean_strings,
    normalize_nulls,
)


# =====================================================================
# Generic helpers
# =====================================================================


def _ensure_columns(
    df: pd.DataFrame,
    columns: list[str],
) -> pd.DataFrame:
    """
    Ensure requested columns exist.

    Any missing column is created with pd.NA.
    """

    out = df.copy()

    for column in columns:
        if column not in out.columns:
            out[column] = pd.NA

    return out


def _stable_int_id(
    *values: object,
) -> int:
    """
    Create a deterministic integer identifier from one or more values.
    """

    text = "|".join(
        ""
        if pd.isna(value)
        else str(value)
        for value in values
    )

    digest = hashlib.sha1(
        text.encode("utf-8")
    ).hexdigest()[:15]

    return int(
        digest,
        16,
    )


def _strip_leading_zeroes(
    series: pd.Series,
) -> pd.Series:
    """
    Remove unnecessary leading zeroes from identifier values.
    """

    s = clean_identifier(
        series
    )

    return (
        s
        .str.lstrip("0")
        .replace(
            "",
            "0",
        )
    )


def _first_coordinate(
    series: pd.Series,
) -> pd.Series:
    """
    Keep the first coordinate when multiple coordinates occur
    in one raw field.
    """

    return pd.to_numeric(
        series
        .astype("string")
        .str.split(",")
        .str[0]
        .str.strip(),
        errors="coerce",
    )


def _year_from_voucher_no(
    value: object,
):
    """
    Extract fiscal year from a voucher number.

    Examples
    --------
    XVFC/2021-22/P/2
        -> 2021-2022

    5THSFC/2021-22/P/1
        -> 2021-2022
    """

    if pd.isna(value):
        return pd.NA

    text = str(
        value
    ).strip()

    match = re.search(
        r"/(\d{4})-(\d{2})/",
        text,
    )

    if not match:
        return pd.NA

    start_year = int(
        match.group(1)
    )

    return (
        f"{start_year}-"
        f"{start_year + 1}"
    )


def _split_pipe(
    value: object,
) -> list[str]:
    """
    Split pipe-separated values used in activity-wise
    expenditure voucher fields.
    """

    if pd.isna(value):
        return []

    text = str(
        value
    ).strip()

    if not text:
        return []

    return [
        item.strip()
        for item in re.split(
            r"\s*\|\s*",
            text,
        )
        if item.strip()
    ]


def _first_non_null(
    series: pd.Series,
):
    """
    Return the first non-null value from a pandas Series.
    """

    values = series.dropna()

    if values.empty:
        return pd.NA

    return values.iloc[0]


# =====================================================================
# Source cleaning: planning
# =====================================================================


def clean_planning(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Standardize flattened eGramSwaraj planning data.

    Supports:
    - plan information
    - activities
    - assets
    - asset locations
    - funds
    - training
    - delegation
    - community services
    - NSAP
    """

    out = df.copy()

    # -----------------------------------------------------------------
    # Rename before generic column normalization because the raw API
    # fields use mixed camelCase / underscores.
    # -----------------------------------------------------------------

    out = out.rename(
        columns={
            # ---------------------------------------------------------
            # Panchayat
            # ---------------------------------------------------------

            "lgd_code":
                "gp_lgd_code",

            "gram_panchayat_name":
                "gp_name",

            # ---------------------------------------------------------
            # Plan
            # ---------------------------------------------------------

            "planCode":
                "plan_code",

            "plan_typ":
                "plan_type",

            "planCodeStts":
                "plan_code_status",

            # ---------------------------------------------------------
            # Activity
            # ---------------------------------------------------------

            "activityCd":
                "activity_code",

            "activityType":
                "activity_type",

            "activityName":
                "activity_name",

            "activityDesc":
                "activity_desc",

            "focusArea":
                "focus_area",

            "activityFor":
                "activity_for",

            "workTyp":
                "work_type",

            "activityForCostlessFlag":
                "is_costless_activity",

            "totalCost":
                "total_cost",

            "operationType":
                "operation_type",

            "operationRemarks":
                "operation_remarks",

            "outputTyp":
                "output_type",

            "activityStts":
                "activity_status",

            # ---------------------------------------------------------
            # Delegation
            # ---------------------------------------------------------

            "dlagtdFlag":
                "is_delegated",

            "dlagtdPlnUntCd":
                "delegated_unit_code",

            "dlagtdPlnUntTyp":
                "delegated_unit_type",

            "dlagtdPlnUntLvl":
                "delegated_unit_level",

            "dlagtdPlnUntCat":
                "delegated_unit_category",

            "shareable":
                "is_shareable",

            "dlagtdPerentPlnUntCd":
                "delegated_parent_unit_code",

            # ---------------------------------------------------------
            # Main asset
            # ---------------------------------------------------------

            "mainAstCtgry":
                "main_asset_category",

            "mainAstSubCtgry":
                "main_asset_subcategory",

            "mainAstUntTyp":
                "main_asset_unit_type",

            "mainAstNumOfUnt":
                "main_asset_unit_count",

            # ---------------------------------------------------------
            # Asset details
            # ---------------------------------------------------------

            "assetDetails":
                "asset_details_raw",

            "assetDetails_astTyp":
                "asset_type",

            "assetDetails_astCtgry":
                "asset_category",

            "assetDetails_astSubCtgry":
                "asset_subcategory",

            "assetDetails_astCvrgCd":
                "asset_coverage_code",

            "assetDetails_astNm":
                "asset_name",

            "assetDetails_astUntTyp":
                "asset_unit_type",

            "assetDetails_astNumOfUnt":
                "asset_unit_count",

            "assetDetails_astUnitCost":
                "asset_unit_cost",

            "assetDetails_astParameterTyp":
                "asset_parameter_type",

            # ---------------------------------------------------------
            # Asset location
            # ---------------------------------------------------------

            "assetDetails_assetLocationDetails_astLocCd":
                "asset_loc_code",

            "assetDetails_assetLocationDetails_astPlnUntCd":
                "asset_loc_unit_code",

            "assetDetails_assetLocationDetails_astPlnUntTyp":
                "asset_loc_unit_type",

            "assetDetails_assetLocationDetails_astNoOfUnt":
                "asset_loc_unit_count",

            "assetDetails_assetLocationDetails_astUnitCostTot":
                "asset_loc_unit_cost_total",

            "assetDetails_assetLocationDetails":
                "asset_loc_overflow_json",

            # ---------------------------------------------------------
            # Fund
            # ---------------------------------------------------------

            "fundList_schemeCode":
                "fund_scheme_code",

            "fundList_componentCode":
                "fund_component_code",

            "fundList_tiedAmountGen":
                "fund_tied_general",

            "fundList_tiedAmountSc":
                "fund_tied_sc",

            "fundList_tiedAmountSt":
                "fund_tied_st",

            "fundList_untiedAmountGen":
                "fund_untied_general",

            "fundList_untiedAmountSc":
                "fund_untied_sc",

            "fundList_untiedAmountSt":
                "fund_untied_st",

            "fundList_amountTotal":
                "fund_amount_total",

            "fundList_tiedAbundonAmountGen":
                "fund_tied_abandoned_general",

            "fundList_tiedAbundonAmountSc":
                "fund_tied_abandoned_sc",

            "fundList_tiedAbundonAmountSt":
                "fund_tied_abandoned_st",

            "fundList_untiedAbundonAmountGen":
                "fund_untied_abandoned_general",

            "fundList_untiedAbundonAmountSc":
                "fund_untied_abandoned_sc",

            "fundList_untiedAbundonAmountSt":
                "fund_untied_abandoned_st",

            "fundList":
                "fund_overflow_json",

            # ---------------------------------------------------------
            # Training
            # ---------------------------------------------------------

            "trainingCapacity":
                "training_capacity_raw",

            "trainingCapacity_trngCatCd":
                "training_category_code",

            "trainingCapacity_trngOrgByCd":
                "training_organiser_code",

            "trainingCapacity_trngSubject":
                "training_subject",

            "trainingCapacity_totTrainees":
                "training_trainees_total",

            "trainingCapacity_totDurationDays":
                "training_duration_days",

            # ---------------------------------------------------------
            # Community service
            # ---------------------------------------------------------

            "communityService":
                "community_service_raw",

            "communityService_serviCd":
                "community_service_code",

            "communityService_serviDuration":
                "community_service_duration",

            "communityService_totalexpBeneficiares":
                "community_beneficiaries_expected",

            # ---------------------------------------------------------
            # NSAP
            # ---------------------------------------------------------

            "activityNsap_old_age_below_eighty_male":
                "nsap_old_age_lt80_male",

            "activityNsap_old_age_below_eighty_female":
                "nsap_old_age_lt80_female",

            "activityNsap_old_age_below_eighty_transgender":
                "nsap_old_age_lt80_transgender",

            "activityNsap_old_age_greater_eighty_male":
                "nsap_old_age_ge80_male",

            "activityNsap_old_age_greater_eighty_female":
                "nsap_old_age_ge80_female",

            "activityNsap_old_age_greater_eighty_transgender":
                "nsap_old_age_ge80_transgender",

            "activityNsap_disabled_male":
                "nsap_disabled_male",

            "activityNsap_disabled_female":
                "nsap_disabled_female",

            "activityNsap_disabled_transgender":
                "nsap_disabled_transgender",

            "activityNsap_widow_male":
                "nsap_widow_male",

            "activityNsap_widow_female":
                "nsap_widow_female",

            # Source typo uses window rather than widow.
            "activityNsap_window_transgender":
                "nsap_widow_transgender",
        }
    )

    out = normalize_nulls(
        out
    )

    out = clean_column_names(
        out
    )

    # -----------------------------------------------------------------
    # Identifiers
    # -----------------------------------------------------------------

    identifier_columns = [
        "row_id",
        "gp_lgd_code",
        "plan_code",
        "activity_code",

        "asset_coverage_code",
        "asset_loc_code",
        "asset_loc_unit_code",

        "fund_scheme_code",
        "fund_component_code",

        "training_category_code",
        "training_organiser_code",

        "community_service_code",

        "delegated_unit_code",
        "delegated_parent_unit_code",
    ]

    for column in identifier_columns:

        if column in out.columns:

            out[column] = clean_identifier(
                out[
                    column
                ]
            )

    # -----------------------------------------------------------------
    # Fiscal year
    # -----------------------------------------------------------------

    if "plan_year" in out.columns:

        out["fiscal_year"] = clean_financial_year(
            out[
                "plan_year"
            ]
        )

    # -----------------------------------------------------------------
    # Numeric fields
    # -----------------------------------------------------------------

    numeric_columns = [
        "total_cost",

        "main_asset_unit_count",

        "asset_unit_count",
        "asset_unit_cost",

        "asset_loc_unit_count",
        "asset_loc_unit_cost_total",

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

        "training_trainees_total",
        "training_duration_days",

        "community_service_duration",
        "community_beneficiaries_expected",

        "nsap_old_age_lt80_male",
        "nsap_old_age_lt80_female",
        "nsap_old_age_lt80_transgender",

        "nsap_old_age_ge80_male",
        "nsap_old_age_ge80_female",
        "nsap_old_age_ge80_transgender",

        "nsap_disabled_male",
        "nsap_disabled_female",
        "nsap_disabled_transgender",

        "nsap_widow_male",
        "nsap_widow_female",
        "nsap_widow_transgender",
    ]

    for column in numeric_columns:

        if column in out.columns:

            out[column] = clean_numeric(
                out[
                    column
                ]
            )

    return out


# =====================================================================
# Source cleaning: expenditure
# =====================================================================


def clean_expenditure(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Standardize all-GP activity-wise expenditure data.

    Expected source columns
    -----------------------
    planYear
    stateName
    zpName
    blockName
    gpName
    gpCode
    planType
    approvalDate
    planCode
    planCodeStatus
    S.No.
    Activity Code
    Activity Name
    Activity For
    Focus Area
    Approved Cost in Action Plan
    Technical Approved Cost
    Admin Approved Cost
    Scheme Name
    General
    SC
    ST
    Total Expenditure
    Voucher Date
    Voucher No
    Voucher Cost
    """

    out = df.copy()

    # -----------------------------------------------------------------
    # Raw expenditure source -> standardized internal names
    # -----------------------------------------------------------------

    out = out.rename(
        columns={
            "planYear":
                "plan_year",

            "stateName":
                "state_name",

            "zpName":
                "zp_name",

            "blockName":
                "block_name",

            "gpName":
                "gp_name",

            "gpCode":
                "gp_lgd_code",

            "planType":
                "plan_type",

            "approvalDate":
                "approval_date",

            "planCode":
                "plan_code",

            "planCodeStatus":
                "plan_code_status",

            "S.No.":
                "s_no",

            "Activity Code":
                "activity_code",

            "Activity Name":
                "activity_name",

            "Activity For":
                "activity_for",

            "Focus Area":
                "focus_area",

            "Approved Cost in Action Plan":
                "approved_cost_action_plan",

            "Technical Approved Cost":
                "technical_approved_cost",

            "Admin Approved Cost":
                "admin_approved_cost",

            "Scheme Name":
                "scheme_name",

            "General":
                "general",

            "SC":
                "sc",

            "ST":
                "st",

            "Total Expenditure":
                "total_expenditure",

            "Voucher Date":
                "voucher_date_list",

            "Voucher No":
                "voucher_no_list",

            "Voucher Cost":
                "voucher_cost_list",
        }
    )

    out = normalize_nulls(
        out
    )

    out = clean_column_names(
        out
    )

    # -----------------------------------------------------------------
    # Identifiers
    # -----------------------------------------------------------------

    identifier_columns = [
        "gp_lgd_code",
        "plan_code",
        "activity_code",
    ]

    for column in identifier_columns:

        if column in out.columns:

            out[column] = clean_identifier(
                out[
                    column
                ]
            )

    # -----------------------------------------------------------------
    # Fiscal year
    #
    # Important:
    # planYear is the year in which the activity belongs to the GPDP.
    #
    # Voucher year may be later than planYear.
    # Voucher fiscal year is independently extracted when building
    # activity_voucher.
    # -----------------------------------------------------------------

    if "plan_year" in out.columns:

        out["fiscal_year"] = clean_financial_year(
            out[
                "plan_year"
            ]
        )

    # -----------------------------------------------------------------
    # Approval date
    # -----------------------------------------------------------------

    if "approval_date" in out.columns:

        out["approval_date"] = pd.to_datetime(
            out[
                "approval_date"
            ],
            errors="coerce",
        )

    # -----------------------------------------------------------------
    # Serial number
    # -----------------------------------------------------------------

    if "s_no" in out.columns:

        out["s_no"] = clean_integer(
            out[
                "s_no"
            ]
        )

    # -----------------------------------------------------------------
    # Financial fields
    # -----------------------------------------------------------------

    amount_columns = [
        "approved_cost_action_plan",
        "technical_approved_cost",
        "admin_approved_cost",
        "general",
        "sc",
        "st",
        "total_expenditure",
    ]

    for column in amount_columns:

        if column in out.columns:

            out[column] = clean_amount(
                out[
                    column
                ]
            )

    # -----------------------------------------------------------------
    # Descriptive fields
    # -----------------------------------------------------------------

    string_columns = [
        "state_name",
        "zp_name",
        "block_name",
        "gp_name",

        "plan_type",
        "plan_code_status",

        "activity_name",
        "activity_for",
        "focus_area",

        "scheme_name",

        "voucher_date_list",
        "voucher_no_list",
        "voucher_cost_list",
    ]

    for column in string_columns:

        if column in out.columns:

            out[column] = clean_strings(
                out[
                    column
                ]
            )

    return out


# =====================================================================
# Source cleaning: vouchers/accounting
# =====================================================================


def clean_vouchers(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Standardize all-GP accounting voucher source.

    Expected raw columns
    --------------------
    district_name
    district_code
    block_name
    block_code
    gp_name
    gp_code
    year
    year_status
    opening_balance
    kind
    month
    date
    voucher_no
    voucher_type
    amount
    voucher_id
    """

    out = df.copy()

    out = out.rename(
        columns={
            "gp_code":
                "gp_lgd_code",

            "year":
                "fiscal_year",

            "kind":
                "direction",

            "voucher_type":
                "type",
        }
    )

    out = normalize_nulls(
        out
    )

    out = clean_column_names(
        out
    )

    # -----------------------------------------------------------------
    # Identifiers
    # -----------------------------------------------------------------

    identifier_columns = [
        "gp_lgd_code",
        "district_code",
        "block_code",
        "voucher_no",
        "voucher_id",
    ]

    for column in identifier_columns:

        if column in out.columns:

            out[column] = clean_identifier(
                out[
                    column
                ]
            )

    # -----------------------------------------------------------------
    # Fiscal year
    # -----------------------------------------------------------------

    if "fiscal_year" in out.columns:

        out["fiscal_year"] = clean_financial_year(
            out[
                "fiscal_year"
            ]
        )

    # -----------------------------------------------------------------
    # Date
    # -----------------------------------------------------------------

    if "date" in out.columns:

        out["date"] = clean_date(
            out[
                "date"
            ],
            dayfirst=True,
        )

    # -----------------------------------------------------------------
    # Amounts
    # -----------------------------------------------------------------

    for column in [
        "opening_balance",
        "amount",
    ]:

        if column in out.columns:

            out[column] = clean_amount(
                out[
                    column
                ]
            )

    # -----------------------------------------------------------------
    # Receipt/payment direction
    # -----------------------------------------------------------------

    if "direction" in out.columns:

        out["direction"] = (
            out[
                "direction"
            ]
            .astype("string")
            .str.strip()
            .str.lower()
        )

        out["direction"] = (
            out[
                "direction"
            ]
            .replace(
                {
                    "receipts":
                        "receipt",

                    "payments":
                        "payment",

                    "r":
                        "receipt",

                    "p":
                        "payment",
                }
            )
        )

    # -----------------------------------------------------------------
    # Strings
    # -----------------------------------------------------------------

    string_columns = [
        "gp_name",
        "district_name",
        "block_name",
        "year_status",
        "month",
        "type",
    ]

    for column in string_columns:

        if column in out.columns:

            out[column] = clean_strings(
                out[
                    column
                ]
            )

    return out


# =====================================================================
# 1. gram_panchayat
# =====================================================================


GRAM_PANCHAYAT_COLUMNS = [
    "gp_lgd_code",
    "gp_name",
    "state_code",
    "state_name",
    "district_code",
    "zp_name",
    "block_code",
    "block_name",
]


def gram_panchayat(
    planning: pd.DataFrame,
    expenditure: pd.DataFrame | None = None,
    vouchers: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Create one row per Gram Panchayat.

    Planning remains the primary source, while expenditure and
    accounting sources may fill additional descriptive fields.
    """

    frames: list[pd.DataFrame] = []

    planning_part = _ensure_columns(
        planning,
        GRAM_PANCHAYAT_COLUMNS,
    )

    frames.append(
        planning_part[
            GRAM_PANCHAYAT_COLUMNS
        ]
    )

    if expenditure is not None:

        expenditure_part = _ensure_columns(
            expenditure,
            GRAM_PANCHAYAT_COLUMNS,
        )

        frames.append(
            expenditure_part[
                GRAM_PANCHAYAT_COLUMNS
            ]
        )

    if vouchers is not None:

        voucher_part = _ensure_columns(
            vouchers,
            GRAM_PANCHAYAT_COLUMNS,
        )

        frames.append(
            voucher_part[
                GRAM_PANCHAYAT_COLUMNS
            ]
        )

    combined = pd.concat(
        frames,
        ignore_index=True,
    )

    combined["gp_lgd_code"] = clean_lgd_code(
        combined[
            "gp_lgd_code"
        ]
    )

    combined = combined.dropna(
        subset=[
            "gp_lgd_code",
        ]
    )

    result = (
        combined
        .groupby(
            "gp_lgd_code",
            as_index=False,
            dropna=False,
        )
        .agg(
            {
                column:
                    _first_non_null

                for column
                in GRAM_PANCHAYAT_COLUMNS

                if column !=
                "gp_lgd_code"
            }
        )
    )

    return result[
        GRAM_PANCHAYAT_COLUMNS
    ]


# =====================================================================
# 2. plan
# =====================================================================


PLAN_COLUMNS = [
    "plan_code",
    "gp_lgd_code",
    "fiscal_year",
    "plan_type",
    "approval_date",
    "plan_code_status",
]


def plan(
    planning: pd.DataFrame,
    expenditure: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Create one row per plan_code.
    """

    frames: list[pd.DataFrame] = []

    planning_part = _ensure_columns(
        planning,
        PLAN_COLUMNS,
    )

    frames.append(
        planning_part[
            PLAN_COLUMNS
        ]
    )

    if expenditure is not None:

        expenditure_part = _ensure_columns(
            expenditure,
            PLAN_COLUMNS,
        )

        frames.append(
            expenditure_part[
                PLAN_COLUMNS
            ]
        )

    combined = pd.concat(
        frames,
        ignore_index=True,
    )

    combined["plan_code"] = clean_identifier(
        combined[
            "plan_code"
        ]
    )

    combined["gp_lgd_code"] = clean_lgd_code(
        combined[
            "gp_lgd_code"
        ]
    )

    combined["fiscal_year"] = clean_financial_year(
        combined[
            "fiscal_year"
        ]
    )

    combined = combined.dropna(
        subset=[
            "plan_code",
        ]
    )

    result = (
        combined
        .groupby(
            "plan_code",
            as_index=False,
        )
        .agg(
            {
                "gp_lgd_code":
                    _first_non_null,

                "fiscal_year":
                    _first_non_null,

                "plan_type":
                    _first_non_null,

                "approval_date":
                    _first_non_null,

                "plan_code_status":
                    _first_non_null,
            }
        )
    )

    return result[
        PLAN_COLUMNS
    ]


# =====================================================================
# 3. planned_activity
# =====================================================================


PLANNED_ACTIVITY_COLUMNS = [
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
]


def planned_activity(
    planning: pd.DataFrame,
) -> pd.DataFrame:
    """
    Create one row per planning activity_code.
    """

    out = _ensure_columns(
        planning,
        PLANNED_ACTIVITY_COLUMNS,
    )

    out = out[
        PLANNED_ACTIVITY_COLUMNS
    ].copy()

    out["activity_code"] = clean_activity_code(
        out[
            "activity_code"
        ]
    )

    out["plan_code"] = clean_identifier(
        out[
            "plan_code"
        ]
    )

    out["gp_lgd_code"] = clean_lgd_code(
        out[
            "gp_lgd_code"
        ]
    )

    out["fiscal_year"] = clean_financial_year(
        out[
            "fiscal_year"
        ]
    )

    out["total_cost"] = clean_amount(
        out[
            "total_cost"
        ]
    )

    return (
        out
        .dropna(
            subset=[
                "activity_code",
            ]
        )
        .drop_duplicates(
            subset=[
                "activity_code",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )


# =====================================================================
# 4-8. Planning satellite tables
# =====================================================================


SATELLITES = {
    # -----------------------------------------------------------------
    # activity_asset
    # -----------------------------------------------------------------

    "activity_asset": [
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

    # -----------------------------------------------------------------
    # activity_fund
    # -----------------------------------------------------------------

    "activity_fund": [
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

    # -----------------------------------------------------------------
    # activity_training
    # -----------------------------------------------------------------

    "activity_training": [
        "training_capacity_raw",
        "training_category_code",
        "training_organiser_code",
        "training_subject",
        "training_trainees_total",
        "training_duration_days",
    ],

    # -----------------------------------------------------------------
    # activity_delegation
    # -----------------------------------------------------------------

    "activity_delegation": [
        "is_delegated",
        "delegated_unit_code",
        "delegated_unit_type",
        "delegated_unit_level",
        "delegated_unit_category",
        "is_shareable",
        "delegated_parent_unit_code",
    ],

    # -----------------------------------------------------------------
    # activity_community_service
    # -----------------------------------------------------------------

    "activity_community_service": [
        "community_service_raw",
        "community_service_code",
        "community_service_duration",
        "community_beneficiaries_expected",
    ],
}


def satellite(
    planning: pd.DataFrame,
    table_name: str,
) -> pd.DataFrame:
    """
    Build a planning satellite table.
    """

    if table_name not in SATELLITES:

        raise ValueError(
            f"Unknown satellite table: {table_name}"
        )

    satellite_fields = SATELLITES[
        table_name
    ]

    columns = [
        "activity_code",
        *satellite_fields,
    ]

    out = _ensure_columns(
        planning,
        columns,
    )

    out = out[
        columns
    ].copy()

    out["activity_code"] = clean_activity_code(
        out[
            "activity_code"
        ]
    )

    # Retain only activities for which at least one field
    # belonging to this satellite exists.
    has_data = (
        out[
            satellite_fields
        ]
        .notna()
        .any(
            axis=1
        )
    )

    out = out[
        has_data
    ].copy()

    return (
        out
        .dropna(
            subset=[
                "activity_code",
            ]
        )
        .drop_duplicates(
            subset=[
                "activity_code",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )


# =====================================================================
# 9. activity_nsap
# =====================================================================


NSAP_MAP = {
    "nsap_old_age_lt80_male":
        (
            "old_age",
            "lt80",
            "male",
        ),

    "nsap_old_age_lt80_female":
        (
            "old_age",
            "lt80",
            "female",
        ),

    "nsap_old_age_lt80_transgender":
        (
            "old_age",
            "lt80",
            "transgender",
        ),

    "nsap_old_age_ge80_male":
        (
            "old_age",
            "ge80",
            "male",
        ),

    "nsap_old_age_ge80_female":
        (
            "old_age",
            "ge80",
            "female",
        ),

    "nsap_old_age_ge80_transgender":
        (
            "old_age",
            "ge80",
            "transgender",
        ),

    "nsap_disabled_male":
        (
            "disabled",
            "na",
            "male",
        ),

    "nsap_disabled_female":
        (
            "disabled",
            "na",
            "female",
        ),

    "nsap_disabled_transgender":
        (
            "disabled",
            "na",
            "transgender",
        ),

    "nsap_widow_male":
        (
            "widow",
            "na",
            "male",
        ),

    "nsap_widow_female":
        (
            "widow",
            "na",
            "female",
        ),

    "nsap_widow_transgender":
        (
            "widow",
            "na",
            "transgender",
        ),
}


ACTIVITY_NSAP_COLUMNS = [
    "nsap_id",
    "activity_code",
    "category",
    "age_band",
    "gender",
    "beneficiary_count",
]


def activity_nsap(
    planning: pd.DataFrame,
) -> pd.DataFrame:
    """
    Convert NSAP beneficiary columns to long format.
    """

    required = [
        "activity_code",
        *NSAP_MAP.keys(),
    ]

    source = _ensure_columns(
        planning,
        required,
    )

    source = source[
        required
    ].copy()

    melted = source.melt(
        id_vars=[
            "activity_code",
        ],
        var_name=
        "source_column",
        value_name=
        "beneficiary_count",
    )

    melted["category"] = (
        melted[
            "source_column"
        ]
        .map(
            lambda value:
                NSAP_MAP[
                    value
                ][0]
        )
    )

    melted["age_band"] = (
        melted[
            "source_column"
        ]
        .map(
            lambda value:
                NSAP_MAP[
                    value
                ][1]
        )
    )

    melted["gender"] = (
        melted[
            "source_column"
        ]
        .map(
            lambda value:
                NSAP_MAP[
                    value
                ][2]
        )
    )

    melted["beneficiary_count"] = clean_numeric(
        melted[
            "beneficiary_count"
        ]
    )

    melted["activity_code"] = clean_activity_code(
        melted[
            "activity_code"
        ]
    )

    melted = melted[
        melted[
            "activity_code"
        ].notna()
        &
        melted[
            "beneficiary_count"
        ].notna()
        &
        (
            melted[
                "beneficiary_count"
            ] != 0
        )
    ].copy()

    melted["nsap_id"] = melted.apply(
        lambda row:
            _stable_int_id(
                row[
                    "activity_code"
                ],
                row[
                    "category"
                ],
                row[
                    "age_band"
                ],
                row[
                    "gender"
                ],
            ),
        axis=1,
    )

    return (
        melted[
            ACTIVITY_NSAP_COLUMNS
        ]
        .drop_duplicates(
            subset=[
                "activity_code",
                "category",
                "age_band",
                "gender",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )


# =====================================================================
# 10. activity_expenditure
# =====================================================================


ACTIVITY_EXPENDITURE_COLUMNS = [
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
]


def activity_expenditure(
    expenditure: pd.DataFrame,
) -> pd.DataFrame:
    """
    Build activity-wise expenditure table.

    Grain
    -----
    One activity expenditure record.

    Primary identifier
    ------------------
    expenditure_id is deterministic from:

        gp_lgd_code
        plan_code
        activity_code
        s_no
    """

    source_columns = [
        column
        for column
        in ACTIVITY_EXPENDITURE_COLUMNS
        if column !=
        "expenditure_id"
    ]

    out = _ensure_columns(
        expenditure,
        source_columns,
    )

    out = out[
        source_columns
    ].copy()

    out["activity_code"] = clean_activity_code(
        out[
            "activity_code"
        ]
    )

    out["plan_code"] = clean_identifier(
        out[
            "plan_code"
        ]
    )

    out["gp_lgd_code"] = clean_lgd_code(
        out[
            "gp_lgd_code"
        ]
    )

    out["fiscal_year"] = clean_financial_year(
        out[
            "fiscal_year"
        ]
    )

    # -----------------------------------------------------------------
    # Stable expenditure ID
    # -----------------------------------------------------------------

    out["expenditure_id"] = out.apply(
        lambda row:
            _stable_int_id(
                row[
                    "gp_lgd_code"
                ],
                row[
                    "plan_code"
                ],
                row[
                    "activity_code"
                ],
                row[
                    "s_no"
                ],
            ),
        axis=1,
    )

    # -----------------------------------------------------------------
    # Ensure no duplicate transformed expenditure records
    # -----------------------------------------------------------------

    out = (
        out
        .drop_duplicates(
            subset=[
                "expenditure_id",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )

    return out[
        ACTIVITY_EXPENDITURE_COLUMNS
    ]


# =====================================================================
# 11. voucher
# =====================================================================


VOUCHER_COLUMNS = [
    "voucher_pk",

    "gp_lgd_code",
    "gp_name",

    "district_code",
    "district_name",

    "block_code",
    "block_name",

    "fiscal_year",
    "year_status",

    "opening_balance",
    "total_receipts",
    "total_payments",

    "voucher_no",
    "voucher_id",

    "direction",
    "type",

    "date",
    "month",

    "amount",
]


def voucher(
    vouchers: pd.DataFrame,
) -> pd.DataFrame:
    """
    Build accounting voucher table.

    Grain
    -----
    One voucher per:

        gp_lgd_code
        + fiscal_year
        + voucher_no

    Repeated GP-year values
    -----------------------
    opening_balance
    total_receipts
    total_payments

    total_receipts and total_payments are calculated from all
    accounting transactions for the GP and fiscal year.
    """

    source_columns = [
        "gp_lgd_code",
        "gp_name",

        "district_code",
        "district_name",

        "block_code",
        "block_name",

        "fiscal_year",
        "year_status",

        "opening_balance",

        "voucher_no",
        "voucher_id",

        "direction",
        "type",

        "date",
        "month",

        "amount",
    ]

    out = _ensure_columns(
        vouchers,
        source_columns,
    )

    out = out[
        source_columns
    ].copy()

    # -----------------------------------------------------------------
    # Keys
    # -----------------------------------------------------------------

    out["gp_lgd_code"] = clean_lgd_code(
        out[
            "gp_lgd_code"
        ]
    )

    out["fiscal_year"] = clean_financial_year(
        out[
            "fiscal_year"
        ]
    )

    out["voucher_no"] = clean_identifier(
        out[
            "voucher_no"
        ]
    )

    out["voucher_id"] = clean_identifier(
        out[
            "voucher_id"
        ]
    )

    # -----------------------------------------------------------------
    # Amounts
    # -----------------------------------------------------------------

    out["amount"] = clean_amount(
        out[
            "amount"
        ]
    )

    out["opening_balance"] = clean_amount(
        out[
            "opening_balance"
        ]
    )

    # -----------------------------------------------------------------
    # Direction
    # -----------------------------------------------------------------

    out["direction"] = (
        out[
            "direction"
        ]
        .astype("string")
        .str.strip()
        .str.lower()
    )

    # -----------------------------------------------------------------
    # Voucher rows must have a usable identity
    # -----------------------------------------------------------------

    out = out.dropna(
        subset=[
            "gp_lgd_code",
            "fiscal_year",
            "voucher_no",
        ]
    ).copy()

    # -----------------------------------------------------------------
    # Temporary transaction-specific amounts
    # -----------------------------------------------------------------

    out["_receipt_amount"] = (
        out[
            "amount"
        ]
        .where(
            out[
                "direction"
            ].eq(
                "receipt"
            ),
            0,
        )
        .fillna(0)
    )

    out["_payment_amount"] = (
        out[
            "amount"
        ]
        .where(
            out[
                "direction"
            ].eq(
                "payment"
            ),
            0,
        )
        .fillna(0)
    )

    # -----------------------------------------------------------------
    # GP-year accounting summary
    # -----------------------------------------------------------------

    gp_year_totals = (
        out
        .groupby(
            [
                "gp_lgd_code",
                "fiscal_year",
            ],
            as_index=False,
            dropna=False,
        )
        .agg(
            opening_balance=(
                "opening_balance",
                _first_non_null,
            ),

            total_receipts=(
                "_receipt_amount",
                "sum",
            ),

            total_payments=(
                "_payment_amount",
                "sum",
            ),
        )
    )

    # -----------------------------------------------------------------
    # Remove helper fields
    # -----------------------------------------------------------------

    out = out.drop(
        columns=[
            "_receipt_amount",
            "_payment_amount",
        ]
    )

    # -----------------------------------------------------------------
    # One accounting voucher per GP/year/voucher_no
    # -----------------------------------------------------------------

    out = (
        out
        .drop_duplicates(
            subset=[
                "gp_lgd_code",
                "fiscal_year",
                "voucher_no",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )

    # Remove row-level opening balance because we now use the
    # standardized GP-year value.
    out = out.drop(
        columns=[
            "opening_balance",
        ]
    )

    out = out.merge(
        gp_year_totals,
        on=[
            "gp_lgd_code",
            "fiscal_year",
        ],
        how="left",
        validate="many_to_one",
    )

    # -----------------------------------------------------------------
    # Stable voucher primary key
    # -----------------------------------------------------------------

    out["voucher_pk"] = out.apply(
        lambda row:
            _stable_int_id(
                row[
                    "gp_lgd_code"
                ],
                row[
                    "fiscal_year"
                ],
                row[
                    "voucher_no"
                ],
            ),
        axis=1,
    )

    out = _ensure_columns(
        out,
        VOUCHER_COLUMNS,
    )

    return (
        out[
            VOUCHER_COLUMNS
        ]
        .reset_index(
            drop=True
        )
    )


# =====================================================================
# 12. activity_voucher
# =====================================================================


ACTIVITY_VOUCHER_COLUMNS = [
    "expenditure_id",
    "voucher_pk",

    "gp_lgd_code",
    "fiscal_year",

    "voucher_no",
    "voucher_line_no",

    "voucher_date",
    "voucher_cost",
]


def activity_voucher(
    expenditure_source: pd.DataFrame,
    expenditure_table: pd.DataFrame,
    voucher_table: pd.DataFrame,
) -> pd.DataFrame:
    """
    Create bridge between activity expenditure and accounting vouchers.

    Activity-wise expenditure contains voucher information in
    pipe-separated fields.

    voucher_line_no is required because one expenditure record may
    contain more than one voucher allocation and the same voucher
    may legitimately occur multiple times with different voucher costs.

    Important
    ---------
    expenditure fiscal_year is the GPDP plan year.

    Voucher fiscal_year is extracted from voucher_no itself.

    Example:
        activity plan year:
            2020-2021

        voucher number:
            XVFC/2021-22/P/2

        voucher fiscal year:
            2021-2022

    This allows matching activity expenditure against accounting
    vouchers even when expenditure occurs in a later financial year
    than the original GPDP plan.
    """

    required = [
        "voucher_no_list",
        "voucher_date_list",
        "voucher_cost_list",
        "gp_lgd_code",
    ]

    source = _ensure_columns(
        expenditure_source,
        required,
    )

    source = source[
        required
    ].copy()

    if len(source) != len(
        expenditure_table
    ):

        raise ValueError(
            "Expenditure source and transformed expenditure "
            "table contain different row counts. "
            "activity_voucher requires row-aligned expenditure data."
        )

    source["expenditure_id"] = (
        expenditure_table[
            "expenditure_id"
        ]
        .to_numpy()
    )

    rows: list[dict] = []

    for _, row in source.iterrows():

        voucher_numbers = _split_pipe(
            row[
                "voucher_no_list"
            ]
        )

        voucher_dates = _split_pipe(
            row[
                "voucher_date_list"
            ]
        )

        voucher_costs = _split_pipe(
            row[
                "voucher_cost_list"
            ]
        )

        for position, voucher_no in enumerate(
            voucher_numbers
        ):

            voucher_date = (
                voucher_dates[
                    position
                ]
                if position <
                len(voucher_dates)
                else pd.NA
            )

            voucher_cost = (
                voucher_costs[
                    position
                ]
                if position <
                len(voucher_costs)
                else pd.NA
            )

            rows.append(
                {
                    "expenditure_id":
                        row[
                            "expenditure_id"
                        ],

                    "gp_lgd_code":
                        row[
                            "gp_lgd_code"
                        ],

                    "fiscal_year":
                        _year_from_voucher_no(
                            voucher_no
                        ),

                    "voucher_no":
                        voucher_no,

                    "voucher_line_no":
                        position + 1,

                    "voucher_date":
                        voucher_date,

                    "voucher_cost":
                        voucher_cost,
                }
            )

    if not rows:

        return pd.DataFrame(
            columns=
            ACTIVITY_VOUCHER_COLUMNS
        )

    bridge = pd.DataFrame(
        rows
    )

    # -----------------------------------------------------------------
    # Normalize bridge key fields
    # -----------------------------------------------------------------

    bridge["gp_lgd_code"] = clean_lgd_code(
        bridge[
            "gp_lgd_code"
        ]
    )

    bridge["voucher_no"] = clean_identifier(
        bridge[
            "voucher_no"
        ]
    )

    bridge["fiscal_year"] = clean_financial_year(
        bridge[
            "fiscal_year"
        ]
    )

    bridge["voucher_line_no"] = pd.to_numeric(
        bridge[
            "voucher_line_no"
        ],
        errors="coerce",
    ).astype(
        "Int64"
    )

    bridge["voucher_date"] = clean_date(
        bridge[
            "voucher_date"
        ],
        dayfirst=True,
    )

    bridge["voucher_cost"] = clean_amount(
        bridge[
            "voucher_cost"
        ]
    )

    # -----------------------------------------------------------------
    # Link to accounting vouchers
    # -----------------------------------------------------------------

    voucher_lookup = (
        voucher_table[
            [
                "voucher_pk",
                "gp_lgd_code",
                "fiscal_year",
                "voucher_no",
            ]
        ]
        .drop_duplicates(
            subset=[
                "gp_lgd_code",
                "fiscal_year",
                "voucher_no",
            ],
            keep="last",
        )
    )

    bridge = bridge.merge(
        voucher_lookup,
        on=[
            "gp_lgd_code",
            "fiscal_year",
            "voucher_no",
        ],
        how="left",
        validate="many_to_one",
    )

    return (
        bridge[
            ACTIVITY_VOUCHER_COLUMNS
        ]
        .reset_index(
            drop=True
        )
    )


# =====================================================================
# 13. admin_approval
# =====================================================================


ADMIN_APPROVAL_COLUMNS = [
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
]


def admin_approval(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Transform administrative approval source.
    """

    out = df.rename(
        columns={
            "lgd_code":
                "gp_lgd_code",

            "gram_panchayat_name":
                "gp_name",

            "activityCd":
                "activity_code",

            "wrkPlnYr":
                "work_plan_year",

            "wrkAdmApprNo":
                "adm_approval_no",

            "wrkAdmApprSnctnOrdrDt":
                "adm_approval_sanction_date",

            "wrkProposedCost":
                "work_proposed_cost",

            "wrkAdmApprIssAuthrty":
                "adm_approval_authority",
        }
    )

    out = normalize_nulls(
        out
    )

    out = clean_column_names(
        out
    )

    out = _ensure_columns(
        out,
        ADMIN_APPROVAL_COLUMNS,
    )

    out = out[
        ADMIN_APPROVAL_COLUMNS
    ].copy()

    for column in [
        "row_id",
        "gp_lgd_code",
        "activity_code",
        "adm_approval_no",
    ]:

        out[column] = clean_identifier(
            out[
                column
            ]
        )

    out["adm_approval_no"] = _strip_leading_zeroes(
        out[
            "adm_approval_no"
        ]
    )

    out["adm_approval_sanction_date"] = pd.to_datetime(
        out[
            "adm_approval_sanction_date"
        ],
        errors="coerce",
        format="mixed",
    )

    out["work_proposed_cost"] = clean_amount(
        out[
            "work_proposed_cost"
        ]
    )

    return (
        out
        .dropna(
            subset=[
                "row_id",
            ]
        )
        .drop_duplicates(
            subset=[
                "row_id",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )


# =====================================================================
# 14. admin_approval_scheme
# =====================================================================


ADMIN_APPROVAL_SCHEME_COLUMNS = [
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
]


def admin_approval_scheme(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Transform administrative approval scheme/fund source.
    """

    out = df.rename(
        columns={
            "activityCd":
                "activity_code",

            "wrkSchmCd":
                "scheme_code",

            "wrkSchmCmpntCd":
                "scheme_component_code",

            "wrkAdmApprFndSnctnGen":
                "fund_sanctioned_general",

            "wrkAdmApprFndSnctnSc":
                "fund_sanctioned_sc",

            "wrkAdmApprFndSnctnSt":
                "fund_sanctioned_st",

            "fndAllctnSchmTot":
                "fund_sanctioned_total",
        }
    )

    out = normalize_nulls(
        out
    )

    out = clean_column_names(
        out
    )

    out = _ensure_columns(
        out,
        ADMIN_APPROVAL_SCHEME_COLUMNS,
    )

    out = out[
        ADMIN_APPROVAL_SCHEME_COLUMNS
    ].copy()

    for column in [
        "row_id",
        "parent_row_id",
        "activity_code",
        "scheme_code",
        "scheme_component_code",
    ]:

        out[column] = clean_identifier(
            out[
                column
            ]
        )

    out["pos"] = clean_integer(
        out[
            "pos"
        ]
    )

    for column in [
        "fund_sanctioned_general",
        "fund_sanctioned_sc",
        "fund_sanctioned_st",
        "fund_sanctioned_total",
    ]:

        out[column] = clean_amount(
            out[
                column
            ]
        )

    return (
        out
        .dropna(
            subset=[
                "row_id",
            ]
        )
        .drop_duplicates(
            subset=[
                "row_id",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )


# =====================================================================
# 15. technical_approval
# =====================================================================


TECHNICAL_APPROVAL_COLUMNS = [
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
]


def technical_approval(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Transform technical approval source.
    """

    out = df.rename(
        columns={
            "lgd_code":
                "gp_lgd_code",

            "gram_panchayat_name":
                "gp_name",

            "activityCd":
                "activity_code",

            "wrkTecApprReqFlg":
                "tec_approval_required",

            "wrkTecApprCost":
                "tec_approval_cost",

            "wrkTecApprIssAuthrty":
                "tec_approval_authority",

            "wrkTecApprOrdrNo":
                "tec_approval_order_no",

            "wrkTecApprOrdrDt":
                "tec_approval_order_date",
        }
    )

    out = normalize_nulls(
        out
    )

    out = clean_column_names(
        out
    )

    out = _ensure_columns(
        out,
        TECHNICAL_APPROVAL_COLUMNS,
    )

    out = out[
        TECHNICAL_APPROVAL_COLUMNS
    ].copy()

    for column in [
        "row_id",
        "gp_lgd_code",
        "activity_code",
        "tec_approval_order_no",
    ]:

        out[column] = clean_identifier(
            out[
                column
            ]
        )

    out["tec_approval_order_no"] = _strip_leading_zeroes(
        out[
            "tec_approval_order_no"
        ]
    )

    out["tec_approval_order_date"] = pd.to_datetime(
        out[
            "tec_approval_order_date"
        ],
        errors="coerce",
        format="mixed",
    )

    out["tec_approval_cost"] = clean_amount(
        out[
            "tec_approval_cost"
        ]
    )

    return (
        out
        .dropna(
            subset=[
                "row_id",
            ]
        )
        .drop_duplicates(
            subset=[
                "row_id",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )


# =====================================================================
# 16. physical_progress
# =====================================================================


PHYSICAL_PROGRESS_COLUMNS = [
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
]


def physical_progress(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Transform physical progress / geo-coordinate source.
    """

    out = df.rename(
        columns={
            "activityCd":
                "activity_code",

            "fileUploadId":
                "file_upload_id",

            "plnunttypecode":
                "plan_unit_type_code",
        }
    )

    out = normalize_nulls(
        out
    )

    out = clean_column_names(
        out
    )

    source_columns = [
        "row_id",
        "parent_row_id",
        "pos",

        "activity_code",

        "file_upload_id",

        "longitude",
        "latitude",

        "plan_unit_type_code",
    ]

    out = _ensure_columns(
        out,
        source_columns,
    )

    out = out[
        source_columns
    ].copy()

    for column in [
        "row_id",
        "parent_row_id",
        "activity_code",
        "file_upload_id",
        "plan_unit_type_code",
    ]:

        out[column] = clean_identifier(
            out[
                column
            ]
        )

    out["pos"] = clean_integer(
        out[
            "pos"
        ]
    )

    # -----------------------------------------------------------------
    # Retain source coordinate strings
    # -----------------------------------------------------------------

    out["latitude_raw"] = (
        out[
            "latitude"
        ]
        .astype(
            "string"
        )
    )

    out["longitude_raw"] = (
        out[
            "longitude"
        ]
        .astype(
            "string"
        )
    )

    # -----------------------------------------------------------------
    # Number of latitude coordinates
    # -----------------------------------------------------------------

    lat_non_null = (
        out[
            "latitude_raw"
        ].notna()
    )

    out["n_coords"] = pd.Series(
        pd.NA,
        index=out.index,
        dtype="Int64",
    )

    out.loc[
        lat_non_null,
        "n_coords",
    ] = (
        out.loc[
            lat_non_null,
            "latitude_raw",
        ]
        .str.count(",")
        .fillna(0)
        .astype("Int64")
        + 1
    )

    # -----------------------------------------------------------------
    # Use first available coordinate as normalized location
    # -----------------------------------------------------------------

    out["latitude"] = _first_coordinate(
        out[
            "latitude"
        ]
    )

    out["longitude"] = _first_coordinate(
        out[
            "longitude"
        ]
    )

    out = _ensure_columns(
        out,
        PHYSICAL_PROGRESS_COLUMNS,
    )

    return (
        out[
            PHYSICAL_PROGRESS_COLUMNS
        ]
        .dropna(
            subset=[
                "row_id",
            ]
        )
        .drop_duplicates(
            subset=[
                "row_id",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )


# =====================================================================
# 17. dim_code
# =====================================================================


DIM_CODE_COLUMNS = [
    "variable",
    "code",
    "description",
    "source",
    "confidence",
]


def dim_code(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Transform code-description workbook into dim_code.
    """

    out = df.rename(
        columns={
            "variabe_codes":
                "code",

            "codes_desc":
                "description",
        }
    )

    out = normalize_nulls(
        out
    )

    out = clean_column_names(
        out
    )

    out = _ensure_columns(
        out,
        DIM_CODE_COLUMNS,
    )

    out = out[
        DIM_CODE_COLUMNS
    ].copy()

    out["variable"] = clean_strings(
        out[
            "variable"
        ]
    )

    out["code"] = clean_identifier(
        out[
            "code"
        ]
    )

    out["description"] = clean_strings(
        out[
            "description"
        ]
    )

    out["source"] = clean_strings(
        out[
            "source"
        ]
    )

    out["confidence"] = clean_strings(
        out[
            "confidence"
        ]
    )

    return (
        out
        .dropna(
            subset=[
                "variable",
                "code",
            ]
        )
        .drop_duplicates(
            subset=[
                "variable",
                "code",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )


# =====================================================================
# 18. dim_welfare_scheme
# =====================================================================


DIM_WELFARE_SCHEME_COLUMNS = [
    "scheme_code",
    "scheme_name",
]


def dim_welfare_scheme(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Transform welfare scheme master.
    """

    out = normalize_nulls(
        df.copy()
    )

    out = clean_column_names(
        out
    )

    out = _ensure_columns(
        out,
        DIM_WELFARE_SCHEME_COLUMNS,
    )

    out = out[
        DIM_WELFARE_SCHEME_COLUMNS
    ].copy()

    out["scheme_code"] = clean_identifier(
        out[
            "scheme_code"
        ]
    )

    out["scheme_name"] = clean_strings(
        out[
            "scheme_name"
        ]
    )

    return (
        out
        .dropna(
            subset=[
                "scheme_code",
            ]
        )
        .drop_duplicates(
            subset=[
                "scheme_code",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )


# =====================================================================
# 19. dim_lsdg_theme
# =====================================================================


DIM_LSDG_THEME_COLUMNS = [
    "focus_area_name",
    "lsdg_theme",
    "distinct_themes",
    "n_rows",
]


def dim_lsdg_theme(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Transform focus-area to LSDG-theme mapping.
    """

    out = df.rename(
        columns={
            "Focus Area":
                "focus_area_name",

            "focus area":
                "focus_area_name",

            "Dominant LSDG Theme":
                "lsdg_theme",

            "dominant LSDG theme":
                "lsdg_theme",

            "Distinct Themes Seen":
                "distinct_themes",

            "distinct themes seen":
                "distinct_themes",

            "Rows":
                "n_rows",

            "rows":
                "n_rows",
        }
    )

    out = normalize_nulls(
        out
    )

    out = clean_column_names(
        out
    )

    out = _ensure_columns(
        out,
        DIM_LSDG_THEME_COLUMNS,
    )

    out = out[
        DIM_LSDG_THEME_COLUMNS
    ].copy()

    out["focus_area_name"] = clean_strings(
        out[
            "focus_area_name"
        ]
    )

    out["lsdg_theme"] = clean_strings(
        out[
            "lsdg_theme"
        ]
    )

    out["distinct_themes"] = clean_numeric(
        out[
            "distinct_themes"
        ]
    )

    out["n_rows"] = clean_numeric(
        out[
            "n_rows"
        ]
    )

    return (
        out
        .dropna(
            subset=[
                "focus_area_name",
            ]
        )
        .drop_duplicates(
            subset=[
                "focus_area_name",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )