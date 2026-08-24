from __future__ import annotations

import argparse
import logging
import sys

from schema_tables import config, transform
from schema_tables.export import export_tables, print_export_summary
from schema_tables.utils import read_required_csv, release_memory


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)

logger = logging.getLogger(__name__)


def build(
    sample_rows: int | None = None,
) -> int:
    """
    Build the physical_progress schema table from the
    eGramSwaraj physical progress source.
    """

    raw = read_required_csv(
        config.PHYSICAL_PROGRESS_CSV,
        "physical progress",
        nrows=sample_rows,
    )

    table = transform.physical_progress(
        raw
    )

    logger.info(
        "physical_progress rows: %s",
        f"{len(table):,}",
    )

    results = export_tables(
        {
            "physical_progress": table,
        }
    )

    print_export_summary(
        results
    )

    del raw
    del table

    release_memory()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build physical progress schema table."
        )
    )

    parser.add_argument(
        "--sample-rows",
        type=int,
        default=None,
        help=(
            "Optional number of source rows to process."
        ),
    )

    args = parser.parse_args()

    return build(
        sample_rows=args.sample_rows
    )


if __name__ == "__main__":
    sys.exit(
        main()
    )