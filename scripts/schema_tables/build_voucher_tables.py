from __future__ import annotations

import logging
import sys

from schema_tables import config, transform
from schema_tables.export import (
    export_tables,
    print_export_summary,
)
from schema_tables.utils import (
    read_required_csv,
    release_memory,
)


# =====================================================================
# Logging
# =====================================================================


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)

logger = logging.getLogger(
    __name__
)


# =====================================================================
# Build
# =====================================================================


def build() -> int:
    """
    Build:

        voucher
        activity_voucher

    Sources
    -------
    voucher:
        all_vouchers.csv

    activity_voucher:
        expenditure_all.csv
        +
        voucher table

    The accounting source contains opening balance at GP-year level.
    The voucher transformation additionally calculates:

        total_receipts
        total_payments

    and repeats all three GP-year accounting summary values on each
    voucher row.
    """

    # ---------------------------------------------------------
    # Read consolidated accounting voucher source
    # ---------------------------------------------------------

    raw_vouchers = read_required_csv(
        config.VOUCHERS_CSV,
        "accounting vouchers",
    )

    logger.info(
        "Raw accounting voucher rows: %s",
        f"{len(raw_vouchers):,}",
    )

    # ---------------------------------------------------------
    # Clean voucher source
    # ---------------------------------------------------------

    cleaned_vouchers = transform.clean_vouchers(
        raw_vouchers
    )

    del raw_vouchers

    release_memory()

    # ---------------------------------------------------------
    # Transform voucher table
    # ---------------------------------------------------------

    voucher_table = transform.voucher(
        cleaned_vouchers
    )

    logger.info(
        "voucher rows: %s",
        f"{len(voucher_table):,}",
    )

    # ---------------------------------------------------------
    # Diagnostic accounting summary
    # ---------------------------------------------------------

    gp_year_summary = (
        voucher_table[
            [
                "gp_lgd_code",
                "fiscal_year",
                "opening_balance",
                "total_receipts",
                "total_payments",
            ]
        ]
        .drop_duplicates(
            subset=[
                "gp_lgd_code",
                "fiscal_year",
            ]
        )
    )

    logger.info(
        "GP-year accounting summaries: %s",
        f"{len(gp_year_summary):,}",
    )

    logger.info(
        "Total receipts across GP-years: %.2f",
        gp_year_summary[
            "total_receipts"
        ]
        .fillna(0)
        .sum(),
    )

    logger.info(
        "Total payments across GP-years: %.2f",
        gp_year_summary[
            "total_payments"
        ]
        .fillna(0)
        .sum(),
    )

    del gp_year_summary
    del cleaned_vouchers

    release_memory()

    # ---------------------------------------------------------
    # Read expenditure source
    #
    # This source contains the activity-level voucher lists.
    # ---------------------------------------------------------

    raw_expenditure = read_required_csv(
        config.EXPENDITURE_CSV,
        "activity expenditure",
    )

    logger.info(
        "Raw activity expenditure rows: %s",
        f"{len(raw_expenditure):,}",
    )

    expenditure_source = (
        transform.clean_expenditure(
            raw_expenditure
        )
    )

    del raw_expenditure

    release_memory()

    # ---------------------------------------------------------
    # Build expenditure table only in memory
    #
    # We need its expenditure_id values for the bridge.
    # This does NOT export/rebuild activity_expenditure.csv.
    # ---------------------------------------------------------

    expenditure_table = (
        transform.activity_expenditure(
            expenditure_source
        )
    )

    # ---------------------------------------------------------
    # Build activity-voucher bridge
    # ---------------------------------------------------------

    activity_voucher_table = (
        transform.activity_voucher(
            expenditure_source=
            expenditure_source,

            expenditure_table=
            expenditure_table,

            voucher_table=
            voucher_table,
        )
    )

    logger.info(
        "activity_voucher rows: %s",
        f"{len(activity_voucher_table):,}",
    )

    # ---------------------------------------------------------
    # Bridge diagnostics
    # ---------------------------------------------------------

    bridge_with_pk = (
        activity_voucher_table[
            "voucher_pk"
        ]
        .notna()
        .sum()
    )

    bridge_without_pk = (
        activity_voucher_table[
            "voucher_pk"
        ]
        .isna()
        .sum()
    )

    logger.info(
        "activity_voucher matched to accounting voucher: %s",
        f"{bridge_with_pk:,}",
    )

    logger.info(
        "activity_voucher without accounting match: %s",
        f"{bridge_without_pk:,}",
    )

    del expenditure_source
    del expenditure_table

    release_memory()

    # ---------------------------------------------------------
    # Export
    # ---------------------------------------------------------

    results = export_tables(
        {
            "voucher":
                voucher_table,

            "activity_voucher":
                activity_voucher_table,
        }
    )

    print_export_summary(
        results
    )

    del voucher_table
    del activity_voucher_table

    release_memory()

    return 0


# =====================================================================
# CLI
# =====================================================================


def main() -> int:
    return build()


if __name__ == "__main__":

    sys.exit(
        main()
    )