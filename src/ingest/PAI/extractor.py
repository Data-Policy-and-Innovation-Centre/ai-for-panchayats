import json
import sys
import time
from datetime import datetime

from bs4 import BeautifulSoup
from loguru import logger

from .config import (
    STATE_ID, FY_ID, PAGE_URL, REQUEST_DELAY, OUTPUT_DIR,
)
from .api import create_session, fetch_with_retry, get_districts, get_blocks
from .utils import (
    extract_asp_hidden_fields, build_form_data, build_next_page_form,
    parse_gp_table, has_next_page,
)


def scrape_block(session, state_id, district_id, district_name,
                 block_id, block_name, fy_id=FY_ID):
    """Scrape all GP records for a single block, handling pagination."""
    resp = fetch_with_retry(session, "GET", PAGE_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    hidden = extract_asp_hidden_fields(soup)

    form = build_form_data(hidden, state_id, district_id, block_id, fy_id)
    resp = fetch_with_retry(session, "POST", PAGE_URL, data=form)
    soup = BeautifulSoup(resp.text, "html.parser")

    all_records = parse_gp_table(soup, district_name, block_name)

    page = 1
    while has_next_page(soup):
        page += 1
        logger.info("    ↳ Fetching page {} …", page)
        time.sleep(REQUEST_DELAY)

        hidden = extract_asp_hidden_fields(soup)
        form = build_next_page_form(hidden, state_id, district_id, block_id, fy_id)
        resp = fetch_with_retry(session, "POST", PAGE_URL, data=form)
        soup = BeautifulSoup(resp.text, "html.parser")

        page_records = parse_gp_table(soup, district_name, block_name)
        if not page_records:
            break
        all_records.extend(page_records)

    return all_records


def scrape_all():
    """Scrape PAI scores for all GPs in Odisha."""
    logger.info("=" * 60)
    logger.info("PAI Scraper — Odisha (FY 2023-2024)")
    logger.info("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Output directory: {}", OUTPUT_DIR)

    session = create_session()

    districts = get_districts(session)
    if not districts:
        logger.error("No districts found! Check network connectivity or API changes.")
        sys.exit(1)

    all_gp_records = []
    scrape_stats = {
        "state": "Odisha",
        "state_id": STATE_ID,
        "financial_year": "2023-2024",
        "scrape_started_at": datetime.now().isoformat(),
        "total_districts": len(districts),
        "total_blocks": 0,
        "total_gram_panchayats": 0,
    }

    for d_idx, district in enumerate(districts, 1):
        district_id = district["id"]
        district_name = district["name"]
        logger.info(
            "[District {}/{}] {} (id={})",
            d_idx, len(districts), district_name, district_id,
        )

        time.sleep(REQUEST_DELAY)
        blocks = get_blocks(session, district_id)
        logger.info("  Found {} blocks.", len(blocks))
        scrape_stats["total_blocks"] += len(blocks)

        for b_idx, block in enumerate(blocks, 1):
            block_id = block["id"]
            block_name = block["name"]
            logger.info(
                "  [Block {}/{}] {} (id={})",
                b_idx, len(blocks), block_name, block_id,
            )

            time.sleep(REQUEST_DELAY)
            try:
                records = scrape_block(
                    session, STATE_ID, district_id, district_name,
                    block_id, block_name, FY_ID,
                )
                logger.info("    -> {} GPs scraped.", len(records))
                all_gp_records.extend(records)
            except Exception:
                logger.exception(
                    "    ✗ Failed to scrape block {} / {} — skipping.",
                    district_name, block_name,
                )

    scrape_stats["total_gram_panchayats"] = len(all_gp_records)
    scrape_stats["scrape_finished_at"] = datetime.now().isoformat()

    output = {
        "metadata": scrape_stats,
        "data": all_gp_records,
    }

    # TODO: Replace this direct file write with RunPublisher from src/pipeline/
    # once it lands. RunPublisher provides immutable, hash-verified runs with
    # run IDs and manifests. Every other adapter in the repo is being written
    # against it. See reviewer comment on original line 311.
    output_file = OUTPUT_DIR / "odisha_pai_scores.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info("DONE — {} GP records saved to {}", len(all_gp_records), output_file)
    logger.info("Stats: {}", json.dumps(scrape_stats, indent=2))
    logger.info("=" * 60)
