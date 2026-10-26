#!/usr/bin/env python3
"""CLI for the eGramSwaraj flattener.

    python scripts/run_egram_flatten.py --kinds PL --limit-gps 5
    python scripts/run_egram_flatten.py --drop-batches
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Bootstrap: add repo root to sys.path so first-party packages are importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest.egramSwaraj_Flatten.config import (
    BATCH_SIZE,
    INPUT_DIR,
    KINDS,
    MASTER_MAX_ROWS,
    OUTPUT_DIR,
    configure_logging,
)
from src.ingest.egramSwaraj_Flatten.extractor import process


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input-dir", type=Path, default=INPUT_DIR,
        help="Root directory holding LGD_* folders (default: %(default)s)",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=OUTPUT_DIR,
        help="Root directory for flattened CSVs (default: %(default)s)",
    )
    parser.add_argument(
        "--kinds", nargs="+", default=list(KINDS),
        choices=list(KINDS),
        help="Document types to process (default: all: %(default)s)",
    )
    parser.add_argument(
        "--limit-gps", type=int, default=None, metavar="N",
        help="Process only the first N GP folders (smoke test)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=BATCH_SIZE, metavar="N",
        help="Number of source JSONs per batch CSV (default: %(default)s)",
    )
    parser.add_argument(
        "--no-master", dest="master", action="store_false", default=True,
        help="Do not build the single concatenated master CSVs",
    )
    parser.add_argument(
        "--master-max-rows", type=int, default=MASTER_MAX_ROWS, metavar="N",
        help="Split masters exceeding this row count; 0 = no cap (default: %(default)s)",
    )
    parser.add_argument(
        "--drop-batches", dest="keep_batches", action="store_false",
        default=True,
        help="Delete intermediate batch CSVs after the master is built",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable DEBUG logging to console and logfile",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(verbose=args.verbose)
    process(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        kinds=tuple(args.kinds),
        limit_gps=args.limit_gps,
        batch_size=args.batch_size,
        master=args.master,
        master_max_rows=args.master_max_rows,
        keep_batches=args.keep_batches,
    )


if __name__ == "__main__":
    main()
