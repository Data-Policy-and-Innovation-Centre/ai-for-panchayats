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
    "build_admin_approval_tables"
)


def build(
    sample_rows: int | None = None,
) -> int:

    admin_raw = read_required_csv(
        config.ADMIN_APPROVAL_CSV,
        "administrative approval",
        nrows=sample_rows,
    )

    admin_scheme_raw = read_required_csv(
        config.ADMIN_APPROVAL_SCHEME_CSV,
        "administrative approval scheme",
        nrows=sample_rows,
    )

    admin_table = (
        transform.admin_approval(
            admin_raw
        )
    )

    admin_scheme_table = (
        transform.admin_approval_scheme(
            admin_scheme_raw
        )
    )

    logger.info(
        "admin_approval rows: %s",
        f"{len(admin_table):,}",
    )

    logger.info(
        "admin_approval_scheme rows: %s",
        f"{len(admin_scheme_table):,}",
    )

    report = export_tables(
        {
            "admin_approval":
                admin_table,

            "admin_approval_scheme":
                admin_scheme_table,
        }
    )

    print_export_summary(
        report
    )

    del admin_raw
    del admin_scheme_raw
    del admin_table
    del admin_scheme_table

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