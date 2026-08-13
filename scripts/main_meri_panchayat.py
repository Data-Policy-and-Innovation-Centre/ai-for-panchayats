#!/usr/bin/env python3
"""Run the Meri Panchayat ingestion adapters in order.

Each adapter is imported and called in this process, so an import error or a
FetchError surfaces immediately instead of being reported as a skipped script.
The pipeline exits non-zero on the first failure and never claims success after
one.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ingest.meri_panchayat.config import MissingCredential

STAGES = [
    "village_population",
    "panchayat_funds",
    "panchayat_payment_register",
    "action_plans",
    "activity_summary",
    "beneficiaries",
    "work_activities",
]

logger = logging.getLogger("meri_panchayat")


def run_stage(name: str) -> None:
    module = __import__(f"ingest.meri_panchayat.{name}", fromlist=["main"])
    module.main()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stages", nargs="+", choices=STAGES, default=STAGES,
                        metavar="STAGE",
                        help=f"stages to run (default: all of {' '.join(STAGES)})")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-8s | %(message)s")

    for stage in args.stages:
        logger.info("Running %s", stage)
        try:
            run_stage(stage)
        except MissingCredential as exc:
            logger.error("%s: %s", stage, exc)
            return 2
        except Exception:
            logger.exception("%s failed; aborting before a partial state is "
                             "published", stage)
            return 1
        logger.info("Completed %s", stage)

    logger.info("Pipeline completed: %s", ", ".join(args.stages))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
