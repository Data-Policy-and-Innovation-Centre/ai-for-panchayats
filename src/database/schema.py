"""Relational schema for the panchayat read model.

One definition of every table, its keys and its constraints, so the schema is
reviewable in a single file rather than spread across notebook cells.

Ordering matters twice and in opposite directions: CREATE_ORDER runs parents
first, RESET_ORDER children first. RESET_ORDER must include every table that
references another, or DuckDB refuses to drop the parent and the rebuild is not
rerunnable.

Money is DECIMAL(16,2) and identifiers are VARCHAR throughout. Codes are never
numeric: pandas would coerce them and silently drop leading zeros.
"""

from __future__ import annotations

DDL: dict[str, str] = {
    "gram_panchayat": """
        CREATE TABLE gram_panchayat (
            gp_lgd_code   VARCHAR PRIMARY KEY,
            gp_name       VARCHAR,
            state_code    VARCHAR,
            state_name    VARCHAR,
            district_code VARCHAR,
            zp_name       VARCHAR,
            block_code    VARCHAR,
            block_name    VARCHAR
        )""",
    "plan": """
        CREATE TABLE plan (
            plan_code        VARCHAR PRIMARY KEY,
            gp_lgd_code      VARCHAR,
            fiscal_year      VARCHAR,
            plan_type        VARCHAR,
            approval_date    TIMESTAMP,
            plan_code_status VARCHAR,
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "planned_activity": """
        CREATE TABLE planned_activity (
            activity_code        VARCHAR PRIMARY KEY,
            plan_code            VARCHAR,
            gp_lgd_code          VARCHAR,
            fiscal_year          VARCHAR,
            source_file          VARCHAR,
            activity_type        BIGINT,
            activity_name        VARCHAR,
            activity_desc        VARCHAR,
            focus_area           BIGINT,
            activity_for         BIGINT,
            work_type            BIGINT,
            is_costless_activity BIGINT,
            total_cost           DECIMAL(16,2),
            operation_type       DOUBLE,
            operation_remarks    VARCHAR,
            output_type          BIGINT,
            activity_status      BIGINT,
            FOREIGN KEY (plan_code)   REFERENCES plan (plan_code),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "activity_delegation": """
        CREATE TABLE activity_delegation (
            activity_code              VARCHAR PRIMARY KEY,
            is_delegated               DOUBLE,
            delegated_unit_code        DOUBLE,
            delegated_unit_type        DOUBLE,
            delegated_unit_level       DOUBLE,
            delegated_unit_category    DOUBLE,
            is_shareable               BOOLEAN,
            delegated_parent_unit_code DOUBLE,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    "activity_asset": """
        CREATE TABLE activity_asset (
            activity_code             VARCHAR PRIMARY KEY,
            main_asset_category       DOUBLE,
            main_asset_subcategory    DOUBLE,
            main_asset_unit_type      DOUBLE,
            main_asset_unit_count     DOUBLE,
            asset_type                DOUBLE,
            asset_category            DOUBLE,
            asset_subcategory         DOUBLE,
            asset_coverage_code       VARCHAR,
            asset_name                DOUBLE,
            asset_unit_type           DOUBLE,
            asset_unit_count          DOUBLE,
            asset_unit_cost           DOUBLE,
            asset_parameter_type      DOUBLE,
            asset_details_raw         DOUBLE,
            asset_loc_code            DOUBLE,
            asset_loc_unit_code       DOUBLE,
            asset_loc_unit_type       DOUBLE,
            asset_loc_unit_count      DOUBLE,
            asset_loc_unit_cost_total DOUBLE,
            asset_loc_overflow_json   VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    "activity_fund": """
        CREATE TABLE activity_fund (
            activity_code                 VARCHAR PRIMARY KEY,
            fund_scheme_code              DOUBLE,
            fund_component_code           DOUBLE,
            fund_tied_general             DOUBLE,
            fund_tied_sc                  DOUBLE,
            fund_tied_st                  DOUBLE,
            fund_untied_general           DOUBLE,
            fund_untied_sc                DOUBLE,
            fund_untied_st                DOUBLE,
            fund_amount_total             DOUBLE,
            fund_tied_abandoned_general   DOUBLE,
            fund_tied_abandoned_sc        DOUBLE,
            fund_tied_abandoned_st        DOUBLE,
            fund_untied_abandoned_general DOUBLE,
            fund_untied_abandoned_sc      DOUBLE,
            fund_untied_abandoned_st      DOUBLE,
            fund_overflow_json            VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    "activity_training": """
        CREATE TABLE activity_training (
            activity_code           VARCHAR PRIMARY KEY,
            training_capacity_raw   DOUBLE,
            training_category_code  DOUBLE,
            training_organiser_code DOUBLE,
            training_subject        VARCHAR,
            training_trainees_total DOUBLE,
            training_duration_days  DOUBLE,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    "activity_community_service": """
        CREATE TABLE activity_community_service (
            activity_code                    VARCHAR PRIMARY KEY,
            community_service_raw            DOUBLE,
            community_service_code           DOUBLE,
            community_service_duration       DOUBLE,
            community_beneficiaries_expected DOUBLE,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    "activity_nsap": """
        CREATE TABLE activity_nsap (
            nsap_id           INTEGER PRIMARY KEY,
            activity_code     VARCHAR,
            category          VARCHAR,
            age_band          VARCHAR,
            gender            VARCHAR,
            beneficiary_count DECIMAL(16,2),
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    # activity_code carries a foreign key here, unlike the notebook version.
    # Expenditure rows whose activity is absent from the planning extract are
    # quarantined rather than loaded, so the constraint holds and the dropped
    # rows stay countable instead of vanishing.
    "activity_expenditure": """
        CREATE TABLE activity_expenditure (
            expenditure_id            INTEGER PRIMARY KEY,
            activity_code             VARCHAR,
            plan_code                 VARCHAR,
            gp_lgd_code               VARCHAR,
            fiscal_year               VARCHAR,
            s_no                      BIGINT,
            scheme_name               VARCHAR,
            approved_cost_action_plan DECIMAL(16,2),
            technical_approved_cost   DECIMAL(16,2),
            admin_approved_cost       DECIMAL(16,2),
            general                   DECIMAL(16,2),
            sc                        DECIMAL(16,2),
            st                        DECIMAL(16,2),
            total_expenditure         DECIMAL(16,2),
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code),
            FOREIGN KEY (plan_code)     REFERENCES plan (plan_code),
            FOREIGN KEY (gp_lgd_code)   REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    # Uniqueness is (gp_lgd_code, fiscal_year, voucher_no). voucher_id collides
    # across panchayats and using it as the key silently dropped real vouchers.
    "voucher": """
        CREATE TABLE voucher (
            voucher_pk  INTEGER PRIMARY KEY,
            gp_lgd_code VARCHAR,
            fiscal_year VARCHAR,
            voucher_no  VARCHAR,
            voucher_id  VARCHAR,
            direction   VARCHAR,
            type        VARCHAR,
            date        DATE,
            month       VARCHAR,
            amount      DECIMAL(16,2),
            UNIQUE (gp_lgd_code, fiscal_year, voucher_no),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    # Both bridge parents are constrained. voucher_pk is nullable: a voucher
    # named in the expenditure feed may genuinely not appear in the accounting
    # extract, and that gap is measured by the validation gate rather than
    # hidden by dropping the constraint.
    "activity_voucher": """
        CREATE TABLE activity_voucher (
            expenditure_id INTEGER,
            voucher_pk     INTEGER,
            gp_lgd_code    VARCHAR,
            fiscal_year    VARCHAR,
            voucher_no     VARCHAR,
            voucher_date   DATE,
            voucher_cost   DECIMAL(16,2),
            FOREIGN KEY (expenditure_id) REFERENCES activity_expenditure (expenditure_id),
            FOREIGN KEY (voucher_pk)     REFERENCES voucher (voucher_pk)
        )""",
    "admin_approval": """
        CREATE TABLE admin_approval (
            row_id                     VARCHAR PRIMARY KEY,
            gp_lgd_code                VARCHAR,
            gp_name                    VARCHAR,
            plan_year                  VARCHAR,
            doc_type                   VARCHAR,
            source_file                VARCHAR,
            activity_code              VARCHAR,
            work_plan_year             VARCHAR,
            adm_approval_no            VARCHAR,
            adm_approval_sanction_date TIMESTAMP,
            work_proposed_cost         DECIMAL(16,2),
            adm_approval_authority     VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code),
            FOREIGN KEY (gp_lgd_code)   REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "admin_approval_scheme": """
        CREATE TABLE admin_approval_scheme (
            row_id                  VARCHAR PRIMARY KEY,
            parent_row_id           VARCHAR,
            pos                     BIGINT,
            activity_code           VARCHAR,
            scheme_code             VARCHAR,
            scheme_component_code   VARCHAR,
            fund_sanctioned_general DECIMAL(16,2),
            fund_sanctioned_sc      DECIMAL(16,2),
            fund_sanctioned_st      DECIMAL(16,2),
            fund_sanctioned_total   DECIMAL(16,2),
            FOREIGN KEY (parent_row_id) REFERENCES admin_approval (row_id)
        )""",
    "technical_approval": """
        CREATE TABLE technical_approval (
            row_id                  VARCHAR PRIMARY KEY,
            gp_lgd_code             VARCHAR,
            gp_name                 VARCHAR,
            plan_year               VARCHAR,
            doc_type                VARCHAR,
            source_file             VARCHAR,
            activity_code           VARCHAR,
            tec_approval_required   VARCHAR,
            tec_approval_cost       DECIMAL(16,2),
            tec_approval_authority  VARCHAR,
            tec_approval_order_no   VARCHAR,
            tec_approval_order_date TIMESTAMP,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code),
            FOREIGN KEY (gp_lgd_code)   REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "physical_progress": """
        CREATE TABLE physical_progress (
            row_id              VARCHAR PRIMARY KEY,
            parent_row_id       VARCHAR,
            pos                 BIGINT,
            activity_code       VARCHAR,
            file_upload_id      VARCHAR,
            longitude           DOUBLE,
            latitude            DOUBLE,
            n_coords            INTEGER,
            longitude_raw       VARCHAR,
            latitude_raw        VARCHAR,
            plan_unit_type_code VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    "dim_code": """
        CREATE TABLE dim_code (
            variable    VARCHAR,
            code        VARCHAR,
            description VARCHAR,
            source      VARCHAR,
            confidence  VARCHAR,
            PRIMARY KEY (variable, code)
        )""",
    "dim_welfare_scheme": """
        CREATE TABLE dim_welfare_scheme (
            scheme_code VARCHAR PRIMARY KEY,
            scheme_name VARCHAR
        )""",
    "dim_lsdg_theme": """
        CREATE TABLE dim_lsdg_theme (
            focus_area_name VARCHAR,
            lsdg_theme      VARCHAR,
            distinct_themes DOUBLE,
            n_rows          DOUBLE
        )""",
    # Rows rejected by a foreign key or a uniqueness rule, with the reason.
    # Nothing is discarded silently; every dropped row is countable here.
    "quarantine": """
        CREATE TABLE quarantine (
            table_name  VARCHAR,
            reason      VARCHAR,
            key_column  VARCHAR,
            key_value   VARCHAR,
            row_count   BIGINT
        )""",
}

# Parents before children.
CREATE_ORDER = list(DDL)

# Children before parents. Dropping in this order is what makes a rebuild
# rerunnable once the approval and progress tables exist.
RESET_ORDER = list(reversed(CREATE_ORDER))

FACT_TABLES = [t for t in CREATE_ORDER if t != "quarantine"]
