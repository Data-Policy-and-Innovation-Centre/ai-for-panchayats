"""Shared HTTP and persistence helpers for the Meri Panchayat scrapers.

A failed request raises FetchError. It is never reported as an empty result,
because "the API did not answer" and "this panchayat has no records" must not
collapse into the same zero, and an empty output must never overwrite a good
one after an outage.
"""

from __future__ import annotations

import logging

import requests

from .config import (BASE_URL, FIN_YEARS, HIERARCHY_FIN_YEAR, REQUEST_TIMEOUT,
                     STATE_ID, build_headers, hierarchy_year)

logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """A request failed: transport error, non-200 status, or unparseable body."""

    def __init__(self, url: str, reason: str, status: int | None = None) -> None:
        super().__init__(f"{reason} for {url}")
        self.url = url
        self.reason = reason
        self.status = status


def _parse(response: requests.Response, url: str) -> dict:
    if response.status_code != 200:
        raise FetchError(url, f"HTTP {response.status_code}", response.status_code)
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(url, f"unparseable JSON body ({exc})") from exc


def fetch_json(url: str, headers: dict) -> dict:
    """GET one endpoint. Raises FetchError; never returns None."""
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise FetchError(url, f"request failed ({exc})") from exc
    return _parse(response, url)


def fetch_json_post(url: str, headers: dict, payload: dict) -> dict:
    """POST one endpoint. Raises FetchError; never returns None."""
    try:
        response = requests.post(url, headers=headers, json=payload,
                                 timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise FetchError(url, f"request failed ({exc})") from exc
    return _parse(response, url)


def _response_list(data: dict, url: str) -> list:
    """The `response` list from a hierarchy envelope, or FetchError.

    A 200 carrying an error envelope, no `response` key, or a `response` that
    is not a list is a schema failure, not an empty hierarchy. Returning `[]`
    for both made `get_zps`/`get_blocks`/`get_gps` unable to tell them apart,
    which is exactly the successful-empty-output overwrite this client exists
    to prevent: a scrape would "succeed" with no districts and truncate the
    saved data. `[]` now means only that the API sent an explicit empty list.
    """
    if not isinstance(data, dict):
        # A body of literal `null`, or any JSON scalar, parses fine and then
        # fails the membership test below with a raw TypeError -- escaping the
        # FetchError contract this function exists to uphold, and bypassing
        # every caller's failure handling. Check the container first.
        raise FetchError(
            url, f"malformed envelope: body is {type(data).__name__}, not an object")
    if "response" not in data:
        raise FetchError(url, "malformed envelope: no `response` key")
    response = data["response"]
    if not isinstance(response, list):
        raise FetchError(
            url, f"malformed envelope: `response` is {type(response).__name__}, not a list")
    return response


def get_zps() -> list:
    """Districts. A hierarchy failure raises rather than yielding no districts."""
    url = f"{BASE_URL}/api/prd/master/v1/getZPList/{STATE_ID}"
    return _response_list(fetch_json(url, build_headers("master")), url)


def _union_over_years(build_url, key: str, fin_year: str | None) -> list:
    """Hierarchy for one year, or the union across every configured year.

    Pinning the hierarchy to a single year hides panchayats created or
    reorganised later, so the default walks all scraped years and dedupes.
    """
    years = [fin_year] if fin_year else list(FIN_YEARS)
    if HIERARCHY_FIN_YEAR:
        years = [HIERARCHY_FIN_YEAR]

    seen: dict = {}
    for year in years:
        url = build_url(hierarchy_year(year))
        for item in _response_list(fetch_json(url, build_headers("master")), url):
            seen.setdefault(item.get(key), item)
    return list(seen.values())


def get_blocks(zp_id, fin_year: str | None = None) -> list:
    """Blocks in a district, for one year or across every configured year."""
    return _union_over_years(
        lambda year: (f"{BASE_URL}/api/prd/master/v1/getBlockPanchayatList/"
                      f"{STATE_ID}/{zp_id}/P/2?fYear={year}"),
        "bpId", fin_year)


def get_gps(zp_id, bp_id, fin_year: str | None = None) -> list:
    """Gram panchayats in a block, for one year or across every configured year."""
    return _union_over_years(
        lambda year: (f"{BASE_URL}/api/prd/master/v1/getGramPanchayatList/"
                      f"{STATE_ID}/{zp_id}/{bp_id}/P/3?fYear={year}"),
        "gpId", fin_year)


def save_outputs(df, json_path=None, csv_path=None) -> None:
    """Write a frame to JSON and/or CSV, creating parent directories.

    Accepts either order used by the scrapers: save_outputs(df, json_path) and
    save_outputs(df, csv_path=..., json_path=...) both work.
    """
    for path, writer in ((json_path, "json"), (csv_path, "csv")):
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if writer == "json":
            df.to_json(path, orient="records", indent=2, force_ascii=False)
        else:
            df.to_csv(path, index=False)
