#!/usr/bin/env python3
"""CLI for the eGramSwaraj flattener.

    python scripts/run_egram_flatten.py --kinds PL --limit-gps 5
    python scripts/run_egram_flatten.py --drop-batches
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ingest.egramSwaraj_Flatten.config import (BATCH_SIZE, INPUT_DIR, KINDS,
                                               MASTER_MAX_ROWS, OUTPUT_DIR,
                                               configure_logging)
from ingest.egramSwaraj_Flatten.extractor import process


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input-dir", type=Path, default=INPUT_DIR,
                        help=f"GP folders to read (default: {INPUT_DIR})")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR,
                        help=f"where CSVs go (default: {OUTPUT_DIR})")
    parser.add_argument("--kinds", nargs="+", choices=KINDS, default=list(KINDS),
                        metavar="KIND",
                        help=f"document types to flatten (default: all of "
                             f"{' '.join(KINDS)})")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"source JSONs per batch CSV (default: {BATCH_SIZE})")
    parser.add_argument("--no-master", action="store_true",
                        help="stop after the batches; skip the master CSVs")
    parser.add_argument("--master-max-rows", type=int, default=MASTER_MAX_ROWS,
                        help=f"split a master beyond this many rows into _pNNN "
                             f"parts; 0 disables (default: {MASTER_MAX_ROWS})")
    parser.add_argument("--drop-batches", action="store_true",
                        help="delete the batch CSVs once the masters are built")
    parser.add_argument("--limit-gps", type=int, default=None,
                        help="process only the first N GP folders (test runs)")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(verbose=args.verbose)

    if args.batch_size < 1:
        raise SystemExit("--batch-size must be at least 1")

    process(
        args.input_dir,
        args.output_dir,
        tuple(args.kinds),
        args.limit_gps,
        batch_size=args.batch_size,
        master=not args.no_master,
        master_max_rows=args.master_max_rows,
        keep_batches=not args.drop_batches,
    )


if __name__ == "__main__":
    main(sys.argv[1:])