from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import pandas as pd

from schema_tables import config, transform
from schema_tables.utils import (
    append_csv,
    iter_csv_chunks,
    release_memory,
    reset_directory,
    write_empty_csv,
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
# Planning tables
# =====================================================================


PLANNING_TABLES = [
    "gram_panchayat",
    "plan",
    "planned_activity",
    "activity_asset",
    "activity_fund",
    "activity_training",
    "activity_delegation",
    "activity_community_service",
    "activity_nsap",
]


# These tables are written chunk-by-chunk.
CHUNK_TABLES = [
    "planned_activity",
    "activity_asset",
    "activity_fund",
    "activity_training",
    "activity_delegation",
    "activity_community_service",
    "activity_nsap",
]


# =====================================================================
# CSV dtype hints
# =====================================================================


PLANNING_DTYPES = {
    # Raw JSON / nested fields
    "assetDetails":
        "string",

    "assetDetails_assetLocationDetails":
        "string",

    "fundList":
        "string",

    "trainingCapacity":
        "string",

    "communityService":
        "string",

    "activityPmayg":
        "string",

    "activityNsap":
        "string",

    # Fields previously producing DtypeWarning
    "assetDetails_astCvrgCd":
        "string",

    "fundList_schemeCode":
        "string",

    "fundList_componentCode":
        "string",

    "shareable":
        "string",

    "operationRemarks":
        "string",

    "trainingCapacity_trngSubject":
        "string",

    # Identifier-like fields
    "activityCd":
        "string",

    "planCode":
        "string",

    "lgd_code":
        "string",

    "assetDetails_assetLocationDetails_astLocCd":
        "string",

    "assetDetails_assetLocationDetails_astPlnUntCd":
        "string",

    "dlagtdPlnUntCd":
        "string",

    "dlagtdPerentPlnUntCd":
        "string",
}


# =====================================================================
# Helpers
# =====================================================================


def _empty_gp_master() -> pd.DataFrame:
    return pd.DataFrame(
        columns=
        transform.GRAM_PANCHAYAT_COLUMNS
    )


def _empty_plan_master() -> pd.DataFrame:
    return pd.DataFrame(
        columns=
        transform.PLAN_COLUMNS
    )


def _merge_master(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    key: str,
) -> pd.DataFrame:
    """
    Merge small master tables between chunks.

    Incoming records replace older records with the same key.
    """

    if incoming.empty:
        return existing

    if existing.empty:
        return (
            incoming
            .drop_duplicates(
                subset=[
                    key,
                ],
                keep="last",
            )
            .reset_index(
                drop=True
            )
        )

    combined = pd.concat(
        [
            existing,
            incoming,
        ],
        ignore_index=True,
    )

    combined = (
        combined
        .dropna(
            subset=[
                key,
            ]
        )
        .drop_duplicates(
            subset=[
                key,
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )

    return combined


def _table_columns(
    table_name: str,
) -> list[str]:
    """
    Return expected columns for each planning-derived table.
    """

    if table_name == "gram_panchayat":
        return transform.GRAM_PANCHAYAT_COLUMNS

    if table_name == "plan":
        return transform.PLAN_COLUMNS

    if table_name == "planned_activity":
        return transform.PLANNED_ACTIVITY_COLUMNS

    if table_name == "activity_nsap":
        return transform.ACTIVITY_NSAP_COLUMNS

    if table_name in transform.SATELLITES:
        return [
            "activity_code",
            *transform.SATELLITES[
                table_name
            ],
        ]

    raise KeyError(
        f"No column definition for "
        f"{table_name}"
    )


def _commit_outputs(
    staging_dir: Path,
) -> None:
    """
    Atomically replace final planning tables with successfully
    completed staging files.
    """

    config.OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    for table_name in PLANNING_TABLES:

        staged_path = (
            staging_dir
            / f"{table_name}.csv"
        )

        final_path = config.TABLE_PATHS[
            table_name
        ]

        if not staged_path.exists():

            raise FileNotFoundError(
                f"Missing staged table before commit: "
                f"{staged_path}"
            )

        final_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        os.replace(
            staged_path,
            final_path,
        )

        logger.info(
            "Committed %s -> %s",
            table_name,
            final_path,
        )


def _print_summary(
    *,
    source_rows: int,
    table_counts: dict[str, int],
) -> None:

    print(
        "\nPlanning build summary\n"
    )

    print(
        f"{'source planning rows':<36}"
        f"{source_rows:>12,}"
    )

    for table_name in PLANNING_TABLES:

        print(
            f"{table_name:<36}"
            f"{table_counts.get(table_name, 0):>12,}"
        )


# =====================================================================
# Build
# =====================================================================


def build(
    *,
    chunk_size: int = 25_000,
    sample_rows: int | None = None,
) -> int:
    """
    Build all planning-derived schema tables.

    The planning CSV is streamed in chunks so the full multi-million
    row source never needs to be loaded into memory.

    Full run:
        writes to staging first and then commits to schema_tables.

    Sample run:
        writes only to planning_tables_building and does not replace
        final schema tables.
    """

    if chunk_size <= 0:

        raise ValueError(
            "chunk_size must be greater than zero."
        )

    if (
        sample_rows is not None
        and sample_rows <= 0
    ):

        raise ValueError(
            "sample_rows must be greater than zero."
        )

    # ---------------------------------------------------------
    # Confirm planning source
    # ---------------------------------------------------------

    planning_path = config.PLANNING_CSV

    if not planning_path.exists():

        raise FileNotFoundError(
            f"Planning CSV not found: "
            f"{planning_path}"
        )

    logger.info(
        "Planning source: %s",
        planning_path,
    )

    # ---------------------------------------------------------
    # Staging
    # ---------------------------------------------------------

    staging_dir = (
        config.OUTPUT_DIR.parent
        / "planning_tables_building"
    )

    reset_directory(
        staging_dir
    )

    # ---------------------------------------------------------
    # Master tables remain small enough to maintain in memory
    # ---------------------------------------------------------

    gp_master = _empty_gp_master()

    plan_master = _empty_plan_master()

    # ---------------------------------------------------------
    # Track whether each chunk table has already written header
    # ---------------------------------------------------------

    first_write = {
        table_name: True
        for table_name
        in CHUNK_TABLES
    }

    # ---------------------------------------------------------
    # Counts
    # ---------------------------------------------------------

    source_rows = 0

    table_counts = {
        table_name: 0
        for table_name
        in PLANNING_TABLES
    }

    # ---------------------------------------------------------
    # Read planning CSV
    # ---------------------------------------------------------

    logger.info(
        "Streaming planning in chunks of %s rows",
        f"{chunk_size:,}",
    )

    chunks = iter_csv_chunks(
        planning_path,
        "planning",
        chunk_size=chunk_size,
        nrows=sample_rows,
        dtype=PLANNING_DTYPES,
    )

    for chunk_no, raw_chunk in enumerate(
        chunks,
        start=1,
    ):

        source_rows += len(
            raw_chunk
        )

        logger.info(
            "Planning chunk %s | rows=%s | total=%s",
            chunk_no,
            f"{len(raw_chunk):,}",
            f"{source_rows:,}",
        )

        # -----------------------------------------------------
        # Clean current chunk
        # -----------------------------------------------------

        planning = transform.clean_planning(
            raw_chunk
        )

        del raw_chunk

        # -----------------------------------------------------
        # gram_panchayat
        # -----------------------------------------------------

        gp_chunk = transform.gram_panchayat(
            planning
        )

        gp_master = _merge_master(
            gp_master,
            gp_chunk,
            "gp_lgd_code",
        )

        del gp_chunk

        # -----------------------------------------------------
        # plan
        # -----------------------------------------------------

        plan_chunk = transform.plan(
            planning
        )

        plan_master = _merge_master(
            plan_master,
            plan_chunk,
            "plan_code",
        )

        del plan_chunk

        # -----------------------------------------------------
        # planned_activity
        # -----------------------------------------------------

        planned = transform.planned_activity(
            planning
        )

        planned_path = (
            staging_dir
            / "planned_activity.csv"
        )

        first_write[
            "planned_activity"
        ] = append_csv(
            planned,
            planned_path,
            first_write=
            first_write[
                "planned_activity"
            ],
        )

        table_counts[
            "planned_activity"
        ] += len(
            planned
        )

        del planned

        # -----------------------------------------------------
        # activity_asset
        # -----------------------------------------------------

        asset = transform.satellite(
            planning,
            "activity_asset",
        )

        asset_path = (
            staging_dir
            / "activity_asset.csv"
        )

        first_write[
            "activity_asset"
        ] = append_csv(
            asset,
            asset_path,
            first_write=
            first_write[
                "activity_asset"
            ],
        )

        table_counts[
            "activity_asset"
        ] += len(
            asset
        )

        del asset

        # -----------------------------------------------------
        # activity_fund
        # -----------------------------------------------------

        fund = transform.satellite(
            planning,
            "activity_fund",
        )

        fund_path = (
            staging_dir
            / "activity_fund.csv"
        )

        first_write[
            "activity_fund"
        ] = append_csv(
            fund,
            fund_path,
            first_write=
            first_write[
                "activity_fund"
            ],
        )

        table_counts[
            "activity_fund"
        ] += len(
            fund
        )

        del fund

        # -----------------------------------------------------
        # activity_training
        # -----------------------------------------------------

        training = transform.satellite(
            planning,
            "activity_training",
        )

        training_path = (
            staging_dir
            / "activity_training.csv"
        )

        first_write[
            "activity_training"
        ] = append_csv(
            training,
            training_path,
            first_write=
            first_write[
                "activity_training"
            ],
        )

        table_counts[
            "activity_training"
        ] += len(
            training
        )

        del training

        # -----------------------------------------------------
        # activity_delegation
        # -----------------------------------------------------

        delegation = transform.satellite(
            planning,
            "activity_delegation",
        )

        delegation_path = (
            staging_dir
            / "activity_delegation.csv"
        )

        first_write[
            "activity_delegation"
        ] = append_csv(
            delegation,
            delegation_path,
            first_write=
            first_write[
                "activity_delegation"
            ],
        )

        table_counts[
            "activity_delegation"
        ] += len(
            delegation
        )

        del delegation

        # -----------------------------------------------------
        # activity_community_service
        # -----------------------------------------------------

        community = transform.satellite(
            planning,
            "activity_community_service",
        )

        community_path = (
            staging_dir
            / "activity_community_service.csv"
        )

        first_write[
            "activity_community_service"
        ] = append_csv(
            community,
            community_path,
            first_write=
            first_write[
                "activity_community_service"
            ],
        )

        table_counts[
            "activity_community_service"
        ] += len(
            community
        )

        del community

        # -----------------------------------------------------
        # activity_nsap
        # -----------------------------------------------------

        nsap = transform.activity_nsap(
            planning
        )

        nsap_path = (
            staging_dir
            / "activity_nsap.csv"
        )

        first_write[
            "activity_nsap"
        ] = append_csv(
            nsap,
            nsap_path,
            first_write=
            first_write[
                "activity_nsap"
            ],
        )

        table_counts[
            "activity_nsap"
        ] += len(
            nsap
        )

        del nsap
        del planning

        # Release memory after every source chunk.
        release_memory()

    # ---------------------------------------------------------
    # Write small master tables
    # ---------------------------------------------------------

    gp_master = (
        gp_master
        .drop_duplicates(
            subset=[
                "gp_lgd_code",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )

    plan_master = (
        plan_master
        .drop_duplicates(
            subset=[
                "plan_code",
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )

    gp_path = (
        staging_dir
        / "gram_panchayat.csv"
    )

    plan_path = (
        staging_dir
        / "plan.csv"
    )

    gp_master.to_csv(
        gp_path,
        index=False,
    )

    plan_master.to_csv(
        plan_path,
        index=False,
    )

    table_counts[
        "gram_panchayat"
    ] = len(
        gp_master
    )

    table_counts[
        "plan"
    ] = len(
        plan_master
    )

    del gp_master
    del plan_master

    release_memory()

    # ---------------------------------------------------------
    # Make sure every expected table exists even when it has
    # zero records.
    # ---------------------------------------------------------

    for table_name in CHUNK_TABLES:

        path = (
            staging_dir
            / f"{table_name}.csv"
        )

        if not path.exists():

            write_empty_csv(
                path,
                _table_columns(
                    table_name
                ),
            )

            table_counts[
                table_name
            ] = 0

    # ---------------------------------------------------------
    # Summary
    # ---------------------------------------------------------

    _print_summary(
        source_rows=
        source_rows,
        table_counts=
        table_counts,
    )

    # ---------------------------------------------------------
    # Sample mode does NOT alter final schema tables
    # ---------------------------------------------------------

    if sample_rows is not None:

        print(
            "\nSample planning build completed."
        )

        print(
            f"Staging output: "
            f"{staging_dir}"
        )

        return 0

    # ---------------------------------------------------------
    # Commit successful full build
    # ---------------------------------------------------------

    _commit_outputs(
        staging_dir
    )

    print(
        "\nPlanning tables completed successfully."
    )

    return 0


# =====================================================================
# CLI
# =====================================================================


def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Build planning-derived schema tables "
            "from eGramSwaraj planning CSV."
        )
    )

    parser.add_argument(
        "--chunk-size",
        type=int,
        default=25_000,
        help=(
            "Number of planning rows processed per chunk. "
            "Default: 25000."
        ),
    )

    parser.add_argument(
        "--sample-rows",
        type=int,
        default=None,
        help=(
            "Optional number of source rows to process for testing. "
            "Sample builds remain in the staging directory and do "
            "not replace final schema tables."
        ),
    )

    args = parser.parse_args()

    return build(
        chunk_size=
        args.chunk_size,
        sample_rows=
        args.sample_rows,
    )


if __name__ == "__main__":

    sys.exit(
        main()
    )