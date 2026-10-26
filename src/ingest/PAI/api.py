import time

import requests
from loguru import logger

from .config import (
    STATE_ID, FY_ID, DISTRICTS_URL, BLOCKS_URL,
    MAX_RETRIES, RETRY_DELAY, REQUEST_TIMEOUT, HEADERS,
)
from .utils import clean_name


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
                "Attempt {}/{} failed for {}: {} — retrying in {}s",
                attempt, retries, url, exc, wait,
            )
            if attempt == retries:
                raise
            time.sleep(wait)


def get_districts(session: requests.Session,
                  state_id: str = STATE_ID,
                  fy_id: str = FY_ID):
    """Return list of {id, name} for all districts of the state."""
    url = DISTRICTS_URL.format(state_id=state_id, fy_id=fy_id)
    logger.info("Fetching districts for state_id={} …", state_id)
    resp = fetch_with_retry(session, "GET", url)
    data = resp.json()
    districts = [{"id": str(row[0]).strip(), "name": clean_name(str(row[1]))}
                 for row in data.get("rows", [])]
    logger.info("Found {} districts.", len(districts))
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
