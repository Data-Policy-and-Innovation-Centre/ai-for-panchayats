#!/usr/bin/env python3

from __future__ import annotations

import argparse
import logging
import sys

from schema_tables import config
from schema_tables import transform

from schema_tables.export import (
    export_tables,
    print_export_summary,
)

from schema_tables.utils import (
    read_required_csv,
    release_memory,
)


logger = logging.getLogger(
    "build_expenditure_tables"
)


def build(
    sample_rows: int | None = None,
) -> int:

    raw = read_required_csv(
        config.EXPENDITURE_CSV,
        "activity expenditure",
        nrows=sample_rows,
    )

    expenditure = (
        transform.clean_expenditure(
            raw
        )
    )

    del raw

    table = (
        transform.activity_expenditure(
            expenditure
        )
    )

    logger.info(
        "activity_expenditure rows: %s",
        f"{len(table):,}",
    )

    report = export_tables(
        {
            "activity_expenditure":
                table,
        }
    )

    print_export_summary(
        report
    )

    del expenditure
    del table

    release_memory()

    return 0


def main() -> int:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--sample-rows",
        type=int,
        default=None,
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)-8s | "
            "%(message)s"
        ),
    )

    return build(
        sample_rows=args.sample_rows
    )


if __name__ == "__main__":
    sys.exit(main())