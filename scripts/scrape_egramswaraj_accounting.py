#!/usr/bin/env python3
"""Scrape eGramSwaraj (Accounting) vouchers for Odisha GPs into data/raw.

    export EGRAMSWARAJ_JSESSIONID="paste-value-from-browser"

"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make the repo root (for `config`) and src/ (for `scraper`) importable, so the
# script runs with a plain `python scripts/...` without needing an install step.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from loguru import logger  # noqa: E402

from config import (  # noqa: E402
    directories,
    settings,
    stop_logging_to_console,
    resume_logging_to_console,
)
from ingest.egramswaraj_accounting import Scraper, ScraperConfig, DEFAULT_FIN_YEARS  # noqa: E402

# Both outputs live under data/raw, as requested.
OUTPUT_DIR = directories.RAW_DATA / "egramswaraj_accounting"
MASTER_CSV = directories.RAW_DATA / "egramswaraj_accounting_all_vouchers.csv"
LGD_FILE = directories.EXTERNAL_DATA / "Cleaned_LGD_codes.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape eGramSwaraj accounting vouchers.")
    p.add_argument("--districts", nargs="*", default=None,
                   help="District LGD codes to limit to (default: all Odisha).")
    p.add_argument("--years", nargs="*", default=None,
                   help=f"Financial years (default: {' '.join(DEFAULT_FIN_YEARS)}).")
    p.add_argument("--workers", type=int, default=4, help="Concurrent GPs (default 4).")
    p.add_argument("--min-interval", type=float, default=0.25,
                   help="Seconds between request starts, global cap (default 0.25 ~4 req/s).")
    p.add_argument("--csv-only", action="store_true",
                   help="Only rebuild the combined CSV from existing JSON; no scraping.")
    p.add_argument("--no-progress", action="store_true", help="Disable the tqdm bar.")
    p.add_argument("--verbose", action="store_true",
                   help="Keep detailed logs on the console instead of only logs/main.log.")
    return p.parse_args()


def build_config(args: argparse.Namespace) -> ScraperConfig:
    cookie_val = getattr(settings, "EGRAMSWARAJ_JSESSIONID", None) or os.getenv("EGRAMSWARAJ_JSESSIONID")
    if not cookie_val and not args.csv_only:
        logger.error("Set EGRAMSWARAJ_JSESSIONID in your .env (or export it):  "
                     'EGRAMSWARAJ_JSESSIONID="<value from browser>"')
        sys.exit(1)
    if not LGD_FILE.exists() and not args.csv_only:
        logger.error("LGD file not found at {} — place Cleaned_LGD_codes.json there.", LGD_FILE)
        sys.exit(1)

    return ScraperConfig(
        cookie={"JSESSIONID": cookie_val or ""},
        lgd_file=LGD_FILE,
        output_dir=OUTPUT_DIR,
        master_csv=MASTER_CSV,
        districts=args.districts or [],
        fin_years=args.years or list(DEFAULT_FIN_YEARS),
        workers=args.workers,
        min_interval=args.min_interval,
    )


def main() -> None:
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    scraper = Scraper(build_config(args))

    if args.csv_only:
        scraper.build_master_csv()
        return

    
    if not args.verbose:
        stop_logging_to_console()

    summary = scraper.run(progress=not args.no_progress)

    if not args.verbose:
        resume_logging_to_console()
    logger.info("Done: {}", summary)


if __name__ == "__main__":
    main()