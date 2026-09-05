"""Relational schema for the panchayat DuckDB warehouse.

Every table, key, and constraint lives here in one place, in the tradition
of PR #9 (``origin/Abhigyan_database``): the schema is reviewable without
spelunking through notebook cells.

This module now tracks the project's authoritative ER diagram / database
description (19 tables in four groups: dimensions; planning core + its 1:1
satellites; expenditure and accounting; approvals and physical progress;
plus three lookup tables) rather than the earlier from-first-principles
design. Departures from that spec are deliberate and documented where they
occur below: ``gp_profile`` is a twentieth table the spec does not have
(GP demographics, #123), the ``activity_code`` foreign key on ``activity_expenditure``
is intentionally unenforced, the ``plan_code`` foreign key the spec gives
for that same table is deliberately NOT added, and ``dim_lsdg_theme`` has
no declared primary key (the spec itself gives none).

This change was scoped to six specific, explicitly enumerated divergences
between the prior DDL and the spec (see the accompanying report). Beyond
those six, the spec's full column lists for several tables mention fields
that are not present here and that this module does NOT add:
``activity_asset.{main_asset_category,main_asset_subcategory,
main_asset_unit_type,main_asset_unit_count,asset_details_raw,
asset_loc_overflow_json}`` (the four ``main_asset_*`` fields already exist
on ``planned_activity`` in this codebase, not on ``activity_asset``);
``activity_fund.fund_overflow_json``; ``activity_training.
training_capacity_raw``; and ``activity_community_service.
community_service_raw``. These ARE confirmed to be real columns in the
target tables (unlike the nsap_id question below, which turned out to be
a real column too, once checked) -- they are left out for a different
reason: none of them has any corresponding source-field mapping in
``transform.py`` yet, so adding the column here would only ever produce an
all-NULL column with no working loader. That is loader work belonging with
the relevant source adapter, not schema work, and is deliberately deferred
rather than faked with an all-NULL column.

``gram_panchayat``'s six geography columns were on that deferred list until
their loader arrived (#61). They are present now because
``transform.gram_panchayat`` populates them from the LGD reference tree --
column and loader together, which is the bar the paragraph above sets.

PRIMARY KEYS -- single run per build
---------------------------------------------------------------------------
Every primary key is the bare business key the spec gives (``activity_code``,
``plan_code``, ``row_id``, ...), never prefixed with lineage. This is a
deliberate change from an earlier design that prefixed almost every key with
``(source_system, source_run_id, ...)`` so two ingest runs could coexist in
one database. The consuming views join on the bare business code, so a
lineage-prefixed key only fanned out every join for a capability nothing
downstream uses.

The warehouse therefore holds **exactly one run's worth of facts per
build.** ``source_system``/``source_run_id`` remain on every table that
already carried them, as ordinary lineage *columns* -- they are valuable for
provenance and are never dropped -- but they are no longer part of any key.
Selecting two snapshots into one build whose business keys collide (two
source systems both minting ``activity_code`` "7", for instance) is
deliberately *not* handled by picking a winner: the second ``INSERT`` trips
the primary key and fails the whole build, by design (see
``build.build_into``'s transactional rollback). If multi-run coexistence is
ever needed, it requires reintroducing lineage into the key deliberately,
with the resulting join fan-out accepted as a tradeoff -- not a silent
default.

MONEY TYPES -- DOUBLE for planning, DECIMAL(16, 2) for the ledger
---------------------------------------------------------------------------
Money is *not* one type throughout. Planning-side figures (activity cost
estimates, sanctioned/proposed amounts, technical and administrative
approval costs) are advisory numbers produced during plan authoring, not
ledger postings, and are stored as ``DOUBLE``. Only the tables that model an
actual accounting trail -- ``activity_expenditure``, ``voucher``, and
``activity_voucher`` -- store money as ``DECIMAL(16, 2)``, because summing a
ledger under float64 accumulates rounding error a reconciliation cannot
tolerate. (An earlier revision of this module asserted "Money is
DECIMAL(18,2), never DOUBLE" as a blanket rule; that was wrong for the
planning-side tables and has been corrected here rather than left
contradicting the code.)

Identifiers are ``VARCHAR`` throughout (except the handful of integer
surrogate keys called out per-table below), per the same reasoning PR #9
documents: pandas coerces a nullable numeric code to float and silently
drops leading zeros.

INTEGER SURROGATE KEYS
---------------------------------------------------------------------------
``activity_expenditure.expenditure_id``, ``activity_nsap.nsap_id``, and
``voucher.voucher_pk`` are INTEGER surrogates: all three are real, published
columns in the target schema (not artifacts invented by this codebase, the
way ``activity_asset``/``activity_fund``'s old ``row_id`` was -- see the
comments on those two tables), because the source data gives each of these
three tables no natural row identity of its own beyond a wide business
tuple (an expenditure line only as one row among many for a
GP/plan/activity/serial-number tuple; an NSAP beneficiary line only as one
row among many for an activity/category/age-band/gender tuple; a voucher
only by a compound business tuple that itself only carries a UNIQUE
constraint, not a key -- see ``voucher`` below).

``expenditure_id`` and ``nsap_id`` are both assigned the same way: by
their respective ``transform`` function (``transform.activity_expenditure``,
``transform.activity_nsap``) via an explicit, caller-supplied ``start_id``,
advanced by ``build.populate``'s running counter after each snapshot, so
every row loaded across a build's snapshots gets a distinct, dense id
without a database round trip. ``voucher.voucher_pk`` has no loader wired
yet (no canonical "voucher" kind exists in ``transform.py``/``build.py`` yet
-- see the module report for this change), so it is declared as a plain
``INTEGER PRIMARY KEY`` without a generator; whichever adapter first
populates this table needs to assign it the same start_id-and-counter way.

DIM_CODE PROVENANCE -- keep, don't launder
---------------------------------------------------------------------------
``dim_code`` carries ``source`` and ``confidence`` columns that the ER
diagram itself omits. They are kept anyway: of the real code->label
mappings this table holds, the overwhelming majority are not confirmed by a
primary source (many are 'Derived', a meaningful share 'Unresolved', a few
even 'Conflict'). Adding columns is non-breaking for any view that selects
by name, and an analytical consumer decoding a numeric code into a label
must be able to tell a confirmed mapping from a guess rather than have that
distinction silently discarded to match a diagram that never modeled it.

Ordering matters twice, in opposite directions. ``CREATE_ORDER`` runs
parents before children so every ``FOREIGN KEY`` resolves at create time.
``RESET_ORDER`` drops children before parents so a rebuild is rerunnable.
"""

from __future__ import annotations

# Planning-side figures: cost estimates and sanctioned/proposed amounts
# produced during plan authoring, not ledger postings. See the module
# docstring's "MONEY TYPES" section for why these are DOUBLE, not DECIMAL.
PLANNING_MONEY = "DOUBLE"

# Ledger-facing money: activity_expenditure, voucher, activity_voucher. Kept
# exact because these tables model an actual accounting trail that gets
# summed and reconciled.
LEDGER_MONEY = "DECIMAL(16,2)"

DDL: dict[str, str] = {
    # Geography is populated from the LGD reference tree, not from the
    # canonical snapshots: the normalizer only recovers (code, name) from the
    # scraper's folder name. See warehouse.geography for why that is a
    # conformed reference join rather than a source kind.
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
    # One row per GP, from the eGramSwaraj panchayat profile extract (#123).
    # Ten of the file's 99 columns: the nine population counts and the
    # household count. The amenities, education, health, infrastructure,
    # sports and learning-centre blocks are deliberately left in the CSV
    # until something asks for them -- a column nothing reads is a column
    # nothing notices going wrong.
    #
    # Right after gram_panchayat so the FOREIGN KEY resolves at CREATE time.
    "gp_profile": """
        CREATE TABLE gp_profile (
            source_system           VARCHAR,
            source_run_id           VARCHAR,
            gp_lgd_code             VARCHAR PRIMARY KEY,
            total_population        INTEGER,
            male_population         INTEGER,
            female_population       INTEGER,
            transgender_population  INTEGER,
            children_population     INTEGER,
            sc_population           INTEGER,
            st_population           INTEGER,
            obc_population          INTEGER,
            general_population      INTEGER,
            households              INTEGER,
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "plan": """
        CREATE TABLE plan (
            source_system    VARCHAR,
            source_run_id    VARCHAR,
            plan_code        VARCHAR PRIMARY KEY,
            gp_lgd_code      VARCHAR,
            fiscal_year      VARCHAR,
            plan_type        VARCHAR,
            plan_code_status VARCHAR,
            approval_date    TIMESTAMP,
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "planned_activity": f"""
        CREATE TABLE planned_activity (
            source_system         VARCHAR,
            source_run_id         VARCHAR,
            activity_code         VARCHAR PRIMARY KEY,
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
            total_cost            {PLANNING_MONEY},
            operation_type        VARCHAR,
            operation_remarks     VARCHAR,
            output_type           VARCHAR,
            activity_status       VARCHAR,
            main_asset_category    VARCHAR,
            main_asset_subcategory VARCHAR,
            main_asset_unit_type   VARCHAR,
            main_asset_unit_count  VARCHAR,
            FOREIGN KEY (plan_code) REFERENCES plan (plan_code),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "activity_delegation": """
        CREATE TABLE activity_delegation (
            source_system              VARCHAR,
            source_run_id              VARCHAR,
            activity_code              VARCHAR PRIMARY KEY,
            is_delegated               VARCHAR,
            delegated_unit_code        VARCHAR,
            delegated_unit_type        VARCHAR,
            delegated_unit_level       VARCHAR,
            delegated_unit_category    VARCHAR,
            is_shareable               VARCHAR,
            delegated_parent_unit_code VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    # Strictly 1:1 with planned_activity -- no row_id column at all. An
    # earlier revision of this module modeled this as a one-to-many child
    # table (keyed on an invented row_id) to avoid collapsing a genuinely
    # repeated JSON array entry; the real table has no such per-row
    # identity and is one row per activity, so it is now keyed on
    # activity_code directly. A second asset line for the same activity is
    # a conflicting duplicate, quarantined like any other, not a second
    # legitimate row.
    "activity_asset": f"""
        CREATE TABLE activity_asset (
            source_system             VARCHAR,
            source_run_id             VARCHAR,
            activity_code             VARCHAR PRIMARY KEY,
            asset_type                VARCHAR,
            asset_category            VARCHAR,
            asset_subcategory         VARCHAR,
            asset_coverage_code       VARCHAR,
            asset_name                VARCHAR,
            asset_unit_type           VARCHAR,
            asset_unit_count          VARCHAR,
            asset_unit_cost           {PLANNING_MONEY},
            asset_parameter_type      VARCHAR,
            asset_loc_code            VARCHAR,
            asset_loc_unit_code       VARCHAR,
            asset_loc_unit_type       VARCHAR,
            asset_loc_unit_count      VARCHAR,
            asset_loc_unit_cost_total {PLANNING_MONEY},
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    # Strictly 1:1 with planned_activity, same reasoning as activity_asset
    # above.
    "activity_fund": f"""
        CREATE TABLE activity_fund (
            source_system                 VARCHAR,
            source_run_id                 VARCHAR,
            activity_code                 VARCHAR PRIMARY KEY,
            fund_scheme_code              VARCHAR,
            fund_component_code           VARCHAR,
            fund_tied_general             {PLANNING_MONEY},
            fund_tied_sc                  {PLANNING_MONEY},
            fund_tied_st                  {PLANNING_MONEY},
            fund_untied_general           {PLANNING_MONEY},
            fund_untied_sc                {PLANNING_MONEY},
            fund_untied_st                {PLANNING_MONEY},
            fund_amount_total             {PLANNING_MONEY},
            fund_tied_abandoned_general   {PLANNING_MONEY},
            fund_tied_abandoned_sc        {PLANNING_MONEY},
            fund_tied_abandoned_st        {PLANNING_MONEY},
            fund_untied_abandoned_general {PLANNING_MONEY},
            fund_untied_abandoned_sc      {PLANNING_MONEY},
            fund_untied_abandoned_st      {PLANNING_MONEY},
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    "activity_training": """
        CREATE TABLE activity_training (
            source_system            VARCHAR,
            source_run_id            VARCHAR,
            activity_code            VARCHAR PRIMARY KEY,
            training_category_code   VARCHAR,
            training_organiser_code  VARCHAR,
            training_subject         VARCHAR,
            training_trainees_total  VARCHAR,
            training_duration_days   VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    "activity_community_service": """
        CREATE TABLE activity_community_service (
            source_system                    VARCHAR,
            source_run_id                    VARCHAR,
            activity_code                    VARCHAR PRIMARY KEY,
            community_service_code           VARCHAR,
            community_service_duration       VARCHAR,
            community_beneficiaries_expected VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    # One row per non-zero beneficiary category, not one wide row per
    # activity: a wide "male count / female count / ..." row makes "how many
    # widows were served" a column-name lookup instead of a filter.
    #
    # nsap_id is a real, published column in the target schema (confirmed
    # against the real table header, which lists it first) -- not an
    # invented artifact of this codebase the way activity_asset/
    # activity_fund's old row_id was. It is an INTEGER surrogate assigned
    # the same way activity_expenditure.expenditure_id is (see the module
    # docstring's "INTEGER SURROGATE KEYS" section): by
    # transform.activity_nsap via a caller-supplied start_id, advanced by
    # build.populate's running counter. The table is legitimately EMPTY in
    # every real build to date (all source NSAP/PMAY-G columns are null in
    # the real planning file), so the id-assignment logic is tested at the
    # unit level rather than through an end-to-end populated build.
    "activity_nsap": """
        CREATE TABLE activity_nsap (
            nsap_id           INTEGER PRIMARY KEY,
            source_system     VARCHAR,
            source_run_id     VARCHAR,
            activity_code     VARCHAR,
            category          VARCHAR,
            age_band          VARCHAR,
            gender            VARCHAR,
            beneficiary_count INTEGER,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    # Expenditure ("recommended"/activity-wise expenditure, source kind RE).
    # Renamed from recommended_expenditure to activity_expenditure: a
    # downstream view (v_exp) and ~324 query templates built on top of it
    # select FROM activity_expenditure by name, so the old name broke
    # every one of them. expenditure_id is an INTEGER surrogate (see the
    # module docstring's "INTEGER SURROGATE KEYS" section for how it is
    # assigned) because the source gives this table no row identity of its
    # own beyond (gp_lgd_code, plan_code, activity_code, s_no), which is a
    # composite awkward to use as a foreign-key target from
    # activity_voucher.
    #
    # activity_code -> planned_activity is deliberately NOT a foreign key:
    # 20 real rows carry an activity_code absent from planned_activity.
    # Forcing the constraint would either be silently vacuous or reject
    # rows on an assumption the data itself disproves.
    #
    # A FOREIGN KEY from plan_code to plan is part of the spec as given,
    # but is deliberately NOT added here: this table's plan_code values are
    # not verified to always resolve to a loaded plan row (the same
    # normalizer "unmapped" caveat that motivates skipping the
    # activity_code FK applies, and this repo's synthetic fixtures
    # routinely populate activity_expenditure/RE without a matching plan
    # row). Adding it without evidence it actually holds against real data
    # risks turning a previously-loadable row into a hard build failure.
    # See the accompanying report's "spec conflicts" section.
    "activity_expenditure": f"""
        CREATE TABLE activity_expenditure (
            expenditure_id             INTEGER PRIMARY KEY,
            source_system              VARCHAR,
            source_run_id              VARCHAR,
            gp_lgd_code                VARCHAR,
            plan_code                  VARCHAR,
            activity_code              VARCHAR,
            fiscal_year                VARCHAR,
            s_no                       VARCHAR,
            scheme_name                VARCHAR,
            approved_cost_action_plan  {LEDGER_MONEY},
            technical_approved_cost    {LEDGER_MONEY},
            admin_approved_cost        {LEDGER_MONEY},
            general                    {LEDGER_MONEY},
            sc                         {LEDGER_MONEY},
            st                         {LEDGER_MONEY},
            total_expenditure          {LEDGER_MONEY},
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    # No loader is wired for this table yet (no canonical "voucher"/
    # accounting kind exists in transform.py/build.py at the time of this
    # change -- see the accompanying report). The DDL is added now so the
    # warehouse's structural shape matches the spec; whichever adapter
    # first populates it needs to assign voucher_pk the same
    # caller-supplied-counter way activity_expenditure.expenditure_id is
    # assigned (see the module docstring).
    #
    # voucher_id is intentionally NOT unique or a key: it collides across
    # gp/year in the real data and is stored as a plain descriptive column.
    # The real identity is the UNIQUE constraint below, verified against
    # real data.
    "voucher": f"""
        CREATE TABLE voucher (
            voucher_pk  INTEGER PRIMARY KEY,
            gp_lgd_code VARCHAR,
            fiscal_year VARCHAR,
            voucher_no  VARCHAR,
            voucher_id  VARCHAR,
            direction   VARCHAR CHECK (direction IN ('payment', 'receipt')),
            type        VARCHAR,
            date        TIMESTAMP,
            month       VARCHAR,
            amount      {LEDGER_MONEY},
            UNIQUE (gp_lgd_code, fiscal_year, voucher_no),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    # No loader is wired for this table yet, same caveat as voucher above.
    # NO PRIMARY KEY, per the spec: this is a pure bridge table between
    # expenditure lines and vouchers. voucher_pk is deliberately NULLABLE
    # -- 488 real bridge rows cite FY 2026-27 vouchers the accounting
    # extract does not reach, and are legitimately unmatched rather than
    # invalid; do not tighten this to NOT NULL and do not drop those rows.
    "activity_voucher": f"""
        CREATE TABLE activity_voucher (
            expenditure_id INTEGER,
            voucher_pk     INTEGER,
            gp_lgd_code    VARCHAR,
            fiscal_year    VARCHAR,
            voucher_no     VARCHAR,
            voucher_date   TIMESTAMP,
            voucher_cost   {LEDGER_MONEY},
            FOREIGN KEY (expenditure_id) REFERENCES activity_expenditure (expenditure_id),
            FOREIGN KEY (voucher_pk) REFERENCES voucher (voucher_pk)
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
            row_id                     VARCHAR PRIMARY KEY,
            gp_lgd_code                VARCHAR,
            gp_name                    VARCHAR,
            plan_year                  VARCHAR,
            source_file                VARCHAR,
            activity_code              VARCHAR,
            work_plan_year             VARCHAR,
            adm_approval_no            VARCHAR,
            adm_approval_sanction_date TIMESTAMP,
            work_proposed_cost         {PLANNING_MONEY},
            adm_approval_authority     VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "admin_approval_scheme": f"""
        CREATE TABLE admin_approval_scheme (
            source_system           VARCHAR,
            source_run_id           VARCHAR,
            row_id                  VARCHAR PRIMARY KEY,
            parent_row_id           VARCHAR,
            activity_code           VARCHAR,
            scheme_code              VARCHAR,
            scheme_component_code   VARCHAR,
            fund_sanctioned_general {PLANNING_MONEY},
            fund_sanctioned_sc      {PLANNING_MONEY},
            fund_sanctioned_st      {PLANNING_MONEY},
            fund_sanctioned_total   {PLANNING_MONEY},
            FOREIGN KEY (parent_row_id) REFERENCES admin_approval (row_id)
        )""",
    "technical_approval": f"""
        CREATE TABLE technical_approval (
            source_system            VARCHAR,
            source_run_id            VARCHAR,
            row_id                   VARCHAR PRIMARY KEY,
            gp_lgd_code              VARCHAR,
            gp_name                  VARCHAR,
            plan_year                VARCHAR,
            source_file              VARCHAR,
            activity_code            VARCHAR,
            tec_approval_required    VARCHAR,
            tec_approval_cost        {PLANNING_MONEY},
            tec_approval_authority   VARCHAR,
            tec_approval_order_no    VARCHAR,
            tec_approval_order_date  TIMESTAMP,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code),
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code)
        )""",
    "physical_progress": """
        CREATE TABLE physical_progress (
            source_system       VARCHAR,
            source_run_id       VARCHAR,
            row_id               VARCHAR PRIMARY KEY,
            activity_code        VARCHAR,
            file_upload_id       VARCHAR,
            longitude            DOUBLE,
            latitude             DOUBLE,
            n_coords             INTEGER,
            longitude_raw        VARCHAR,
            latitude_raw         VARCHAR,
            plan_unit_type_code  VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code)
        )""",
    # Lookup: decodes a (variable, code) pair used elsewhere in the
    # warehouse into a human-readable description. source/confidence are
    # kept even though the ER diagram omits them -- see the module
    # docstring's "DIM_CODE PROVENANCE" section: most real mappings are
    # not confirmed, and an analytical consumer must be able to tell a
    # confirmed mapping from a guess.
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
    # No primary key: the spec gives none for this lookup (unlike dim_code
    # and dim_welfare_scheme, which do). Taken at face value rather than
    # inventing one.
    # focus_area -> LSDG theme is many-to-many in the source: 9 of the 17
    # focus areas carry activities under more than one theme. The reference
    # file records a single theme per focus area, so this table is a
    # *reduction*, and `distinct_themes` is what says so -- 3 for Sanitation,
    # 1 for Roads. Carried for the same reason dim_code carries source and
    # confidence: a consumer presenting a label must be able to tell a clean
    # mapping from a collapsed one. `source_rows` is the support behind it.
    "dim_lsdg_theme": """
        CREATE TABLE dim_lsdg_theme (
            focus_area_name VARCHAR,
            lsdg_theme      VARCHAR,
            distinct_themes INTEGER,
            source_rows     INTEGER
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

# Tables with no per-run lineage columns at all: gram_panchayat and
# dim_welfare_scheme/dim_lsdg_theme are conformed dimensions/lookups with no
# source-run identity, and voucher/activity_voucher/dim_code are new
# additions whose spec-given column list has none either (see the module
# docstring). validate.check_provenance skips these rather than querying a
# column that does not exist.
NO_LINEAGE_TABLES = frozenset({
    "gram_panchayat", "voucher", "activity_voucher",
    "dim_code", "dim_welfare_scheme", "dim_lsdg_theme",
})

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
    # RE is getLbAllocatedAmountData -- budgetary allocation, not
    # expenditure (src/ingest/egramSwaraj_API/config.py). It has no target
    # table yet: activity_expenditure is filled from the separate
    # expenditure extract in #49, and this kind's own allocation rows are
    # reported unconsumed by build.populate until something claims them.
    # The key still belongs here -- select.py requires every kind to be
    # present in a snapshot -- so the empty value is the accurate one.
    "RE": (),
    # The panchayat profile extract: one flat CSV, its own raw run, its own
    # snapshot (#123). Recognized here so `select` accepts a snapshot that
    # declares it and `build` knows what it fills -- but NOT required, see
    # REQUIRED_KINDS.
    "PROFILE": ("gp_profile",),
    # The accounting extract: nested per-GP-per-year JSON, its own raw run,
    # its own snapshot (#129). Like PROFILE, recognized but NOT required --
    # requiring it would mean no rebuild of the scrape could be done without
    # it. activity_voucher is deliberately not listed: it is filled from the
    # expenditure source (#49), not from this one.
    "VOUCHER": ("voucher",),
}

# The kinds a build cannot be missing. The five eGramSwaraj endpoints arrive
# together from one scrape and depend on each other -- aa/ta/pp are filtered
# against the activities pl produces -- so a selection carrying only some of
# them is a partial normalization, and select.resolve_snapshots refuses it.
#
# PROFILE is not in that set on purpose. It is an independent reference
# extract with its own run, and requiring it would mean no rebuild of the
# scraped data could ever be done without it. Completeness of the *published*
# build is measured where it belongs instead: the row-count guard in
# build_snapshot_manifest.py and deploy.expectations at container startup.
REQUIRED_KINDS: frozenset[str] = frozenset({"PL", "AA", "TA", "PP", "RE"})
