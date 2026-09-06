
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make the repo root (for `config`) and src/ (for `ingest.*`) importable.
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
from egramswaraj_expenditure import (  # noqa: E402
    ExpenditureScraper, ExpenditureConfig, DEFAULT_YEARS,
)

OUTPUT_DIR = directories.RAW_DATA / "egramswaraj_expenditure"
PLAN_CSV = directories.RAW_DATA / "plan.csv"
LGD_FILE = directories.EXTERNAL_DATA / "Cleaned_LGD_codes.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape eGramSwaraj activity-wise expenditure.")
    p.add_argument("--years", nargs="*", default=None,
                   help=f"Fiscal years to keep (default: {' '.join(DEFAULT_YEARS)}). "
                        "Pass 'all' for every year present in plan.csv.")
    p.add_argument("--plan-csv", default=None, help="Override path to plan.csv.")
    p.add_argument("--workers", type=int, default=10, help="Parallel requests (default 10).")
    p.add_argument("--delay", type=float, default=0.1,
                   help="Politeness pause per worker after a hit (default 0.1s).")
    p.add_argument("--save-html", action="store_true", help="Save every page's HTML (debug).")
    p.add_argument("--combine", action="store_true",
                   help="Only merge existing per_plan/ CSVs into the combined files.")
    p.add_argument("--no-progress", action="store_true", help="Disable the tqdm bar.")
    p.add_argument("--verbose", action="store_true",
                   help="Keep detailed logs on the console instead of only logs/main.log.")
    return p.parse_args()


def build_config(args: argparse.Namespace) -> ExpenditureConfig:
    plan_csv = Path(args.plan_csv) if args.plan_csv else PLAN_CSV

    cookie_val = getattr(settings, "EGRAMSWARAJ_JSESSIONID", None) or os.getenv("EGRAMSWARAJ_JSESSIONID")
    if not cookie_val and not args.combine:
        logger.error("Set EGRAMSWARAJ_JSESSIONID in your .env (or export it):  "
                     'EGRAMSWARAJ_JSESSIONID="<value from browser>"')
        sys.exit(1)
    if not args.combine:
        if not plan_csv.exists():
            logger.error("plan.csv not found at {} — place it there or pass --plan-csv.", plan_csv)
            sys.exit(1)
        if not LGD_FILE.exists():
            logger.error("LGD file not found at {} — place Cleaned_LGD_codes.json there.", LGD_FILE)
            sys.exit(1)

    if args.years is None:
        years: set[str] | None = set(DEFAULT_YEARS)
    elif len(args.years) == 1 and args.years[0].lower() == "all":
        years = None
    else:
        years = set(args.years)

    return ExpenditureConfig(
        cookie={"JSESSIONID": cookie_val or ""},
        lgd_file=LGD_FILE,
        plan_csv=plan_csv,
        output_dir=OUTPUT_DIR,
        years=years,
        workers=args.workers,
        delay=args.delay,
        save_html=args.save_html,
    )


def main() -> None:
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    scraper = ExpenditureScraper(build_config(args))

    if args.combine:
        scraper.combine()
        return

    if not args.verbose:
        stop_logging_to_console()

    stats = scraper.run(progress=not args.no_progress)

    if not args.verbose:
        resume_logging_to_console()
    logger.info("Done: {}", stats)


if __name__ == "__main__":
    main()