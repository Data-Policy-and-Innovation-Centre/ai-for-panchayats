from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from bs4 import BeautifulSoup
from loguru import logger

from src.pipeline.manifest import RunPublisher

from .api import create_session, fetch_with_retry, get_blocks, get_districts
from .config import (
    FY_ID,
    OUTPUT_DIR,
    PAGE_URL,
    REQUEST_DELAY,
    STATE_ID,
)
from .utils import (
    build_form_data,
    build_next_page_form,
    extract_asp_hidden_fields,
    has_next_page,
    parse_gp_table,
)


def scrape_block(
    session,
    state_id: str,
    district_id: str,
    district_name: str,
    block_id: str,
    block_name: str,
    fy_id: str = FY_ID,
) -> list[dict]:
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


def scrape_all(
    raw_root: Path | str | None = None,
    run_id: str | None = None,
) -> Path:
    """Scrape PAI scores for all GPs in Odisha and publish an immutable run."""
    logger.info("=" * 60)
    logger.info("PAI Scraper — Odisha (FY 2023-2024)")
    logger.info("=" * 60)

    session = create_session()

    districts = get_districts(session)
    if not districts:
        logger.error("No districts found! Check network connectivity or API changes.")
        sys.exit(1)

    all_gp_records: list[dict] = []
    failures: list[dict] = []
    scrape_stats = {
        "state": "Odisha",
        "state_id": STATE_ID,
        "financial_year": "2023-2024",
        "scrape_started_at": datetime.now(UTC).isoformat(),
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
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "    ✗ Failed to scrape block {} / {} — skipping.",
                    district_name, block_name,
                )
                failures.append({
                    "district_id": district_id,
                    "district_name": district_name,
                    "block_id": block_id,
                    "block_name": block_name,
                    "error": str(exc),
                })

    scrape_stats["total_gram_panchayats"] = len(all_gp_records)
    scrape_stats["failed_blocks"] = len(failures)
    scrape_stats["scrape_finished_at"] = datetime.now(UTC).isoformat()

    output = {
        "metadata": scrape_stats,
        "data": all_gp_records,
        "failures": failures,
    }

    # Route through RunPublisher to create an immutable, hash-verified run
    actual_raw_root = Path(raw_root) if raw_root is not None else OUTPUT_DIR.parent
    actual_run_id = run_id or f"odisha_pai_{datetime.now(UTC).strftime('%Y%m%d_%H%M%SZ')}"
    terminal_state = "failed" if failures else "complete"

    with RunPublisher(
        raw_root=actual_raw_root,
        source="PAI",
        run_id=actual_run_id,
        requested_scope={"state_id": STATE_ID, "financial_year": "2023-2024"},
    ) as publisher:
        publisher.write_payload("odisha_pai_scores.json", json.dumps(output, ensure_ascii=False, indent=2))
        run_destination = publisher.publish(
            terminal_state=terminal_state,
            observed_scope={
                "state_id": STATE_ID,
                "total_districts": len(districts),
                "total_blocks": scrape_stats["total_blocks"],
                "failed_blocks": len(failures),
            },
            counts={
                "gram_panchayats": len(all_gp_records),
                "failed_blocks": len(failures),
            },
            failures=failures,
        )

    # Only update the standalone canonical output if the run completed with ZERO failures.
    # An incomplete, timed-out, or failed run never replaces an existing complete run.
    standalone_file = OUTPUT_DIR / "odisha_pai_scores.json"
    if terminal_state == "complete" and all_gp_records:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        temp_file = OUTPUT_DIR / f".odisha_pai_scores.json.tmp.{uuid4().hex}"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        temp_file.replace(standalone_file)
        logger.info("Updated canonical output: {}", standalone_file)
    elif failures:
        logger.warning(
            "Run had {} failed block(s); preserved any existing canonical output at {}",
            len(failures), standalone_file,
        )

    logger.info("=" * 60)
    logger.info("Published run: {} (terminal_state={})", run_destination, terminal_state)
    logger.info("Stats: {}", json.dumps(scrape_stats, indent=2))
    logger.info("=" * 60)

    return run_destination
