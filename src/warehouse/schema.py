"""Relational schema for the panchayat DuckDB warehouse.

Every table, key, and constraint lives here in one place, in the tradition
of PR #9 (``origin/Abhigyan_database``): the schema is reviewable without
spelunking through notebook cells.

Two departures from that donor, driven by what the canonical Parquet the
normalizer actually produces:

* No bare business code (``activity_code``, ``plan_code``, a bare ``row_id``)
  is a global primary key. A code minted by one source-system run is not
  guaranteed unique against a different run's codes, so every fact table's
  key is a composite that includes ``(source_system, source_run_id)``. This
  is also what makes cross-source and cross-run provenance queryable rather
  than merely stored.
* ``activity_asset`` and ``activity_fund`` are one-to-many child tables
  (an activity may fund more than one scheme, or list more than one asset),
  matching the normalizer's own nested-array handling
  (``pipeline.normalize`` turns a JSON list into a child table). PR #9
  modeled these 1:1, which silently collapses a genuinely repeated activity
  down to one row.

Money is ``DECIMAL(18, 2)``, never ``DOUBLE``: the transform layer parses
amounts into ``decimal.Decimal`` (see ``clean.to_decimal_money``) specifically
so a rupee amount is exact from the source string to the stored value.
Identifiers are ``VARCHAR`` throughout, per the same reasoning PR #9 documents:
pandas coerces a nullable numeric code to float and silently drops leading
zeros.

Ordering matters twice, in opposite directions. ``CREATE_ORDER`` runs
parents before children so every ``FOREIGN KEY`` resolves at create time.
``RESET_ORDER`` drops children before parents so a rebuild is rerunnable.
"""

from __future__ import annotations

MONEY = "DECIMAL(18,2)"

DDL: dict[str, str] = {
    "gram_panchayat": """
        CREATE TABLE gram_panchayat (
            gp_lgd_code VARCHAR PRIMARY KEY,
            gp_name     VARCHAR
        )""",
    "plan": """
        CREATE TABLE plan (
            source_system    VARCHAR,
            source_run_id    VARCHAR,
            plan_code        VARCHAR,
            gp_lgd_code      VARCHAR,
            fiscal_year      VARCHAR,
            plan_type        VARCHAR,
            plan_code_status VARCHAR,
            approval_date    TIMESTAMP,
            PRIMARY KEY (source_system, source_run_id, plan_code),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "planned_activity": f"""
        CREATE TABLE planned_activity (
            source_system        VARCHAR,
            source_run_id        VARCHAR,
            activity_code         VARCHAR,
            plan_code             VARCHAR,
            gp_lgd_code           VARCHAR,
            fiscal_year           VARCHAR,
            source_file           VARCHAR,
            activity_type         VARCHAR,
            activity_name         VARCHAR,
            activity_desc         VARCHAR,
            focus_area            VARCHAR,
            activity_for          VARCHAR,
            work_type             VARCHAR,
            is_costless_activity  VARCHAR,
            total_cost            {MONEY},
            operation_type        VARCHAR,
            operation_remarks     VARCHAR,
            output_type           VARCHAR,
            activity_status       VARCHAR,
            main_asset_category    VARCHAR,
            main_asset_subcategory VARCHAR,
            main_asset_unit_type   VARCHAR,
            main_asset_unit_count  VARCHAR,
            PRIMARY KEY (source_system, source_run_id, activity_code),
            FOREIGN KEY (source_system, source_run_id, plan_code)
                REFERENCES plan (source_system, source_run_id, plan_code),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "activity_delegation": """
        CREATE TABLE activity_delegation (
            source_system              VARCHAR,
            source_run_id              VARCHAR,
            activity_code              VARCHAR,
            is_delegated               VARCHAR,
            delegated_unit_code        VARCHAR,
            delegated_unit_type        VARCHAR,
            delegated_unit_level       VARCHAR,
            delegated_unit_category    VARCHAR,
            is_shareable               VARCHAR,
            delegated_parent_unit_code VARCHAR,
            PRIMARY KEY (source_system, source_run_id, activity_code),
            FOREIGN KEY (source_system, source_run_id, activity_code)
                REFERENCES planned_activity (source_system, source_run_id, activity_code)
        )""",
    "activity_training": """
        CREATE TABLE activity_training (
            source_system           VARCHAR,
            source_run_id           VARCHAR,
            activity_code           VARCHAR,
            training_category_code  VARCHAR,
            training_organiser_code VARCHAR,
            training_subject        VARCHAR,
            training_trainees_total VARCHAR,
            training_duration_days  VARCHAR,
            PRIMARY KEY (source_system, source_run_id, activity_code),
            FOREIGN KEY (source_system, source_run_id, activity_code)
                REFERENCES planned_activity (source_system, source_run_id, activity_code)
        )""",
    "activity_community_service": """
        CREATE TABLE activity_community_service (
            source_system                    VARCHAR,
            source_run_id                    VARCHAR,
            activity_code                    VARCHAR,
            community_service_code           VARCHAR,
            community_service_duration       VARCHAR,
            community_beneficiaries_expected VARCHAR,
            PRIMARY KEY (source_system, source_run_id, activity_code),
            FOREIGN KEY (source_system, source_run_id, activity_code)
                REFERENCES planned_activity (source_system, source_run_id, activity_code)
        )""",
    # One row per non-zero beneficiary category, not one wide row per
    # activity: a wide "male count / female count / ..." row makes "how many
    # widows were served" a column-name lookup instead of a filter.
    "activity_nsap": f"""
        CREATE TABLE activity_nsap (
            source_system     VARCHAR,
            source_run_id     VARCHAR,
            activity_code     VARCHAR,
            category          VARCHAR,
            age_band          VARCHAR,
            gender            VARCHAR,
            beneficiary_count {MONEY},
            PRIMARY KEY (source_system, source_run_id, activity_code, category, age_band, gender),
            FOREIGN KEY (source_system, source_run_id, activity_code)
                REFERENCES planned_activity (source_system, source_run_id, activity_code)
        )""",
    # One-to-many: an activity may list more than one asset line. row_id
    # comes straight from the normalizer's own content-derived row identity.
    "activity_asset": f"""
        CREATE TABLE activity_asset (
            source_system             VARCHAR,
            source_run_id             VARCHAR,
            row_id                    VARCHAR,
            activity_code             VARCHAR,
            asset_type                VARCHAR,
            asset_category            VARCHAR,
            asset_subcategory         VARCHAR,
            asset_coverage_code       VARCHAR,
            asset_name                VARCHAR,
            asset_unit_type           VARCHAR,
            asset_unit_count          VARCHAR,
            asset_unit_cost           {MONEY},
            asset_parameter_type      VARCHAR,
            asset_loc_code            VARCHAR,
            asset_loc_unit_code       VARCHAR,
            asset_loc_unit_type       VARCHAR,
            asset_loc_unit_count      VARCHAR,
            asset_loc_unit_cost_total {MONEY},
            PRIMARY KEY (source_system, source_run_id, row_id),
            FOREIGN KEY (source_system, source_run_id, activity_code)
                REFERENCES planned_activity (source_system, source_run_id, activity_code)
        )""",
    # One-to-many: an activity may draw funds from more than one scheme.
    "activity_fund": f"""
        CREATE TABLE activity_fund (
            source_system                 VARCHAR,
            source_run_id                 VARCHAR,
            row_id                        VARCHAR,
            activity_code                 VARCHAR,
            fund_scheme_code              VARCHAR,
            fund_component_code           VARCHAR,
            fund_tied_general             {MONEY},
            fund_tied_sc                  {MONEY},
            fund_tied_st                  {MONEY},
            fund_untied_general           {MONEY},
            fund_untied_sc                {MONEY},
            fund_untied_st                {MONEY},
            fund_amount_total             {MONEY},
            fund_tied_abandoned_general   {MONEY},
            fund_tied_abandoned_sc        {MONEY},
            fund_tied_abandoned_st        {MONEY},
            fund_untied_abandoned_general {MONEY},
            fund_untied_abandoned_sc      {MONEY},
            fund_untied_abandoned_st      {MONEY},
            PRIMARY KEY (source_system, source_run_id, row_id),
            FOREIGN KEY (source_system, source_run_id, activity_code)
                REFERENCES planned_activity (source_system, source_run_id, activity_code)
        )""",
    # Admin approval is its own source-domain table: the eGramSwaraj AA
    # webservice's activity_code is a proven mapping onto planned_activity
    # (the normalizer marks AA rows "mapped"), so the foreign key holds; the
    # rest of the record (approval numbers, authorities, dates) has no
    # equivalent in the planning payload and is not folded into it.
    "admin_approval": f"""
        CREATE TABLE admin_approval (
            source_system              VARCHAR,
            source_run_id              VARCHAR,
            row_id                     VARCHAR,
            gp_lgd_code                VARCHAR,
            gp_name                    VARCHAR,
            plan_year                  VARCHAR,
            source_file                VARCHAR,
            activity_code              VARCHAR,
            work_plan_year             VARCHAR,
            adm_approval_no            VARCHAR,
            adm_approval_sanction_date TIMESTAMP,
            work_proposed_cost         {MONEY},
            adm_approval_authority     VARCHAR,
            PRIMARY KEY (source_system, source_run_id, row_id),
            FOREIGN KEY (source_system, source_run_id, activity_code)
                REFERENCES planned_activity (source_system, source_run_id, activity_code),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "admin_approval_scheme": f"""
        CREATE TABLE admin_approval_scheme (
            source_system           VARCHAR,
            source_run_id           VARCHAR,
            row_id                  VARCHAR,
            parent_row_id           VARCHAR,
            activity_code           VARCHAR,
            scheme_code              VARCHAR,
            scheme_component_code   VARCHAR,
            fund_sanctioned_general {MONEY},
            fund_sanctioned_sc      {MONEY},
            fund_sanctioned_st      {MONEY},
            fund_sanctioned_total   {MONEY},
            PRIMARY KEY (source_system, source_run_id, row_id),
            FOREIGN KEY (source_system, source_run_id, parent_row_id)
                REFERENCES admin_approval (source_system, source_run_id, row_id)
        )""",
    "technical_approval": f"""
        CREATE TABLE technical_approval (
            source_system            VARCHAR,
            source_run_id            VARCHAR,
            row_id                   VARCHAR,
            gp_lgd_code              VARCHAR,
            gp_name                  VARCHAR,
            plan_year                VARCHAR,
            source_file              VARCHAR,
            activity_code            VARCHAR,
            tec_approval_required    VARCHAR,
            tec_approval_cost        {MONEY},
            tec_approval_authority   VARCHAR,
            tec_approval_order_no    VARCHAR,
            tec_approval_order_date  TIMESTAMP,
            PRIMARY KEY (source_system, source_run_id, row_id),
            FOREIGN KEY (source_system, source_run_id, activity_code)
                REFERENCES planned_activity (source_system, source_run_id, activity_code),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "physical_progress": """
        CREATE TABLE physical_progress (
            source_system       VARCHAR,
            source_run_id       VARCHAR,
            row_id               VARCHAR,
            activity_code        VARCHAR,
            file_upload_id       VARCHAR,
            longitude            DOUBLE,
            latitude             DOUBLE,
            n_coords             INTEGER,
            longitude_raw        VARCHAR,
            latitude_raw         VARCHAR,
            plan_unit_type_code  VARCHAR,
            PRIMARY KEY (source_system, source_run_id, row_id),
            FOREIGN KEY (source_system, source_run_id, activity_code)
                REFERENCES planned_activity (source_system, source_run_id, activity_code)
        )""",
    # Recommended (activity-wise) expenditure is deliberately its own
    # source-domain table with no foreign key onto planned_activity: the
    # normalizer marks RE business ids "unmapped" (its activity_code has not
    # been proven to share planned_activity's code space), so forcing a
    # constraint here would either be silently vacuous or reject rows on an
    # unproven assumption. The identity is the natural business key the
    # source itself guarantees: one row per GP, plan, activity and serial
    # number.
    "recommended_expenditure": f"""
        CREATE TABLE recommended_expenditure (
            source_system             VARCHAR,
            source_run_id             VARCHAR,
            gp_lgd_code               VARCHAR,
            plan_code                 VARCHAR,
            activity_code             VARCHAR,
            fiscal_year               VARCHAR,
            s_no                      VARCHAR,
            scheme_name               VARCHAR,
            approved_cost_action_plan {MONEY},
            technical_approved_cost   {MONEY},
            admin_approved_cost       {MONEY},
            general                   {MONEY},
            sc                        {MONEY},
            st                        {MONEY},
            total_expenditure         {MONEY},
            PRIMARY KEY (source_system, source_run_id, gp_lgd_code, plan_code, activity_code, s_no),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    # Rows rejected by a foreign key, a uniqueness rule, or a missing
    # identity field, with the reason. Nothing is discarded silently; every
    # dropped row is countable here.
    "quarantine": """
        CREATE TABLE quarantine (
            source_system     VARCHAR,
            source_run_id     VARCHAR,
            table_name        VARCHAR,
            reason_code       VARCHAR,
            reason            VARCHAR,
            key_column        VARCHAR,
            key_value         VARCHAR,
            row_count         BIGINT
        )""",
}

# Parents before children, so every FOREIGN KEY resolves at CREATE time.
CREATE_ORDER = list(DDL)

# Children before parents, so a rebuild that drops-and-recreates is
# rerunnable once every child/parent pair exists.
RESET_ORDER = list(reversed(CREATE_ORDER))

FACT_TABLES = [table for table in CREATE_ORDER if table != "quarantine"]

# Tables whose rows come straight from one canonical source_kind, keyed by
# the normalizer's own kind label, used to drive both loading and the
# "required dataset populated or explicitly empty" preflight check.
KIND_TABLES: dict[str, tuple[str, ...]] = {
    "PL": (
        "plan", "planned_activity", "activity_delegation", "activity_training",
        "activity_community_service", "activity_nsap", "activity_asset", "activity_fund",
    ),
    "AA": ("admin_approval", "admin_approval_scheme"),
    "TA": ("technical_approval",),
    "PP": ("physical_progress",),
    "RE": ("recommended_expenditure",),
}
