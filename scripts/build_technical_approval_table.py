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
    Build the technical_approval schema table.
    """

    raw = read_required_csv(
        config.TECHNICAL_APPROVAL_CSV,
        "technical approval",
        nrows=sample_rows,
    )

    table = transform.technical_approval(
        raw
    )

    logger.info(
        "technical_approval rows: %s",
        f"{len(table):,}",
    )

    results = export_tables(
        {
            "technical_approval": table,
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
            "Build technical approval schema table."
        )
    )

    parser.add_argument(
        "--sample-rows",
        type=int,
        default=None,
        help="Optional number of source rows to process.",
    )

    args = parser.parse_args()

    return build(
        sample_rows=args.sample_rows
    )


if __name__ == "__main__":
    sys.exit(
        main()
    )