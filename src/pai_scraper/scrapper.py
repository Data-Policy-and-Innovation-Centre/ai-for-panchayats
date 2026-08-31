import json
import re
import sys
import time
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from .config import (
    STATE_ID, FY_ID, DISTRICTS_URL, BLOCKS_URL, PAGE_URL,
    THEME_COLUMNS, MAX_RETRIES, RETRY_DELAY, REQUEST_DELAY,
    REQUEST_TIMEOUT, HEADERS, OUTPUT_DIR
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

def create_session() -> requests.Session:
    """Create a requests session with browser-like headers."""
    session = requests.Session()
    session.headers.update(HEADERS)
    return session

def fetch_with_retry(session: requests.Session, method: str, url: str,
                     retries: int = MAX_RETRIES, **kwargs) -> requests.Response:
    """Fetch a URL with retries and exponential back-off."""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    for attempt in range(1, retries + 1):
        try:
            resp = session.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except (requests.RequestException, ConnectionError) as exc:
            wait = RETRY_DELAY * attempt
            logger.warning(
                "Attempt %d/%d failed for %s: %s — retrying in %ds",
                attempt, retries, url, exc, wait,
            )
            if attempt == retries:
                raise
            time.sleep(wait)

def clean_name(raw_name: str) -> str:
    """Strip trailing LGD code like ' [344]' from names."""
    return re.sub(r"\s*\[\d+\]\s*$", "", raw_name).strip()

def get_districts(session: requests.Session,
                  state_id: str = STATE_ID,
                  fy_id: str = FY_ID):
    """Return list of {id, name} for all districts of the state."""
    url = DISTRICTS_URL.format(state_id=state_id, fy_id=fy_id)
    logger.info("Fetching districts for state_id=%s …", state_id)
    resp = fetch_with_retry(session, "GET", url)
    data = resp.json()
    districts = [{"id": str(row[0]).strip(), "name": clean_name(str(row[1]))}
                 for row in data.get("rows", [])]
    logger.info("Found %d districts.", len(districts))
    return districts

def get_blocks(session: requests.Session,
               district_id: str,
               state_id: str = STATE_ID,
               fy_id: str = FY_ID):
    """Return list of {id, name} for all blocks in a district."""
    url = BLOCKS_URL.format(state_id=state_id, district_id=district_id, fy_id=fy_id)
    resp = fetch_with_retry(session, "GET", url)
    data = resp.json()
    blocks = [{"id": str(row[0]).strip(), "name": clean_name(str(row[1]))}
              for row in data.get("rows", [])]
    return blocks

def extract_asp_hidden_fields(soup: BeautifulSoup) -> dict:
    """Extract __VIEWSTATE and other hidden fields from the page."""
    fields = {}
    for inp in soup.find_all("input", attrs={"type": "hidden"}):
        name = inp.get("name")
        value = inp.get("value", "")
        if name:
            fields[name] = value
    return fields

def build_form_data(hidden_fields: dict,
                    state_id: str,
                    district_id: str,
                    block_id: str,
                    fy_id: str = FY_ID) -> dict:
    """Build the full POST payload to search GP scores for a block."""
    form = dict(hidden_fields)

    # Dropdowns
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_State"] = state_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_District"] = district_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_Block"] = block_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_Survey"] = fy_id

    # Hidden id mirrors
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_FYID"] = fy_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_StateID"] = state_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_DistrictID"] = district_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_BlockID"] = block_id

    # Submit button
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$btnSubmit"] = "Search"

    # Remove event targets for a clean submit
    form["__EVENTTARGET"] = ""
    form["__EVENTARGUMENT"] = ""

    return form

def build_next_page_form(hidden_fields: dict,
                         state_id: str,
                         district_id: str,
                         block_id: str,
                         fy_id: str = FY_ID) -> dict:
    """Build the POST payload for clicking the 'Next 100' button."""
    form = dict(hidden_fields)

    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_State"] = state_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_District"] = district_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_Block"] = block_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_Survey"] = fy_id

    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_FYID"] = fy_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_StateID"] = state_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_DistrictID"] = district_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_BlockID"] = block_id

    # Click "Next 100 >>"
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$btnNext"] = "Next 100 >>"
    form["__EVENTTARGET"] = ""
    form["__EVENTARGUMENT"] = ""

    # Remove the Search button if present
    form.pop("ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$btnSubmit", None)

    return form

def parse_gp_table(soup: BeautifulSoup,
                   district_name: str,
                   block_name: str):
    """Parse the GP data table and return a list of GP records."""
    table = soup.find("table", {"id": "GVdataT"})
    if not table:
        return []

    tbody = table.find("tbody")
    if not tbody:
        return []

    rows = tbody.find_all("tr")
    records = []

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 11:
            continue

        first_cell = cells[0]
        gp_name = ""
        gp_code = ""

        link = first_cell.find("a")
        if link:
            text = link.get_text(strip=True)
            match = re.match(r"(.+?)-\[(\d+)\]", text)
            if match:
                gp_name = match.group(1).strip()
                gp_code = match.group(2).strip()
            else:
                gp_name = text

        scores = {}
        for i, col_name in enumerate(THEME_COLUMNS):
            cell_text = cells[i + 1].get_text(strip=True)
            try:
                scores[col_name] = float(cell_text)
            except (ValueError, IndexError):
                scores[col_name] = None

        record = {
            "state": "Odisha",
            "district": district_name,
            "block": block_name,
            "gram_panchayat": gp_name,
            "gp_code": gp_code,
            **scores,
        }
        records.append(record)

    return records

def has_next_page(soup: BeautifulSoup) -> bool:
    """Check if the 'Next 100 >>' button is enabled."""
    btn = soup.find("input", {"id": "btnNext"})
    if btn and not btn.get("disabled"):
        return True
    return False

def scrape_block(session: requests.Session,
                 state_id: str,
                 district_id: str,
                 district_name: str,
                 block_id: str,
                 block_name: str,
                 fy_id: str = FY_ID):
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
        logger.info("    ↳ Fetching page %d …", page)
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
    logger.info("=" * 60)
    logger.info("PAI Scraper — Odisha (FY 2023-2024)")
    logger.info("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Output directory: %s", OUTPUT_DIR)

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
            "[District %d/%d] %s (id=%s)",
            d_idx, len(districts), district_name, district_id,
        )

        time.sleep(REQUEST_DELAY)
        blocks = get_blocks(session, district_id)
        logger.info("  Found %d blocks.", len(blocks))
        scrape_stats["total_blocks"] += len(blocks)

        for b_idx, block in enumerate(blocks, 1):
            block_id = block["id"]
            block_name = block["name"]
            logger.info(
                "  [Block %d/%d] %s (id=%s)",
                b_idx, len(blocks), block_name, block_id,
            )

            time.sleep(REQUEST_DELAY)
            try:
                records = scrape_block(
                    session, STATE_ID, district_id, district_name,
                    block_id, block_name, FY_ID,
                )
                logger.info("    -> %d GPs scraped.", len(records))
                all_gp_records.extend(records)
            except Exception:
                logger.exception(
                    "    ✗ Failed to scrape block %s / %s — skipping.",
                    district_name, block_name,
                )

    scrape_stats["total_gram_panchayats"] = len(all_gp_records)
    scrape_stats["scrape_finished_at"] = datetime.now().isoformat()

    output = {
        "metadata": scrape_stats,
        "data": all_gp_records,
    }

    output_file = OUTPUT_DIR / "odisha_pai_scores.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info("DONE — %d GP records saved to %s", len(all_gp_records), output_file)
    logger.info("Stats: %s", json.dumps(scrape_stats, indent=2))
    logger.info("=" * 60)

