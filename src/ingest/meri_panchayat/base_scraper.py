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


class IncompleteRun(RuntimeError):
    """The run finished but could not retrieve everything it was asked for.

    Raised after outputs and the failure manifest are written, so the data
    collected is kept while the run is still reported as incomplete. A partial
    extraction must never exit successfully.
    """

    def __init__(self, stage: str, failures: list) -> None:
        super().__init__(
            f"{stage}: {len(failures)} unit(s) could not be retrieved; "
            f"see the failure manifest beside the output")
        self.stage = stage
        self.failures = failures


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


def response_field(data, field: str, url: str, context: str = "") -> list:
    """One named list out of a `response` envelope, or FetchError.

    `data.get("response", {}).get(field, [])` reads a 200 error envelope as an
    empty result, so an adapter publishes an empty dataset and the
    orchestrator reports success -- the successful-empty-output failure this
    client exists to prevent, reintroduced once per adapter.

    Every adapter shares this instead of writing the defaulting chain again.
    An explicit empty list still means the API said there are none.
    """
    where = f" for {context}" if context else ""
    if not isinstance(data, dict):
        raise FetchError(
            url, f"malformed envelope{where}: body is {type(data).__name__}, not an object")
    response = data.get("response")
    if not isinstance(response, dict):
        raise FetchError(url, f"malformed envelope{where}: no `response` object")
    if field not in response:
        raise FetchError(url, f"malformed envelope{where}: no `response.{field}`")
    value = response[field]
    if not isinstance(value, list):
        raise FetchError(
            url,
            f"malformed envelope{where}: `response.{field}` is "
            f"{type(value).__name__}, not a list")
    return value


def _checked_entries(items: list, key: str, url: str) -> list:
    """Every hierarchy node is a mapping carrying its own identifier.

    A scalar entry (`{"response": [null]}`) raises a raw AttributeError the
    moment a caller does `item.get(...)`, and an object missing its id travels
    downstream as a node with no identity, failing later with an untyped
    KeyError far from the request that produced it. Both are schema drift, so
    both take the same typed failure as a malformed envelope.

    Shared by every hierarchy level. An earlier version validated inside
    `_union_over_years` only, which left `get_zps` -- the one level that does
    not go through it -- returning malformed districts as success.
    """
    for item in items:
        if not isinstance(item, dict):
            raise FetchError(
                url, f"malformed entry: {type(item).__name__}, not an object")
        identifier = item.get(key)
        if identifier is None:
            raise FetchError(url, f"malformed entry: no `{key}`")
        if not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
            # `{"bpId": []}` clears the None check and then fails as an
            # unhashable `seen` key with an untyped TypeError; an equally
            # malformed `zpId` would travel downstream unchecked, since
            # get_zps does not dedupe. An identifier has to be a scalar.
            raise FetchError(
                url,
                f"malformed entry: `{key}` is {type(identifier).__name__}, not a scalar")
        if isinstance(identifier, str) and not identifier.strip():
            # An empty id is a scalar and passes every check above, then
            # builds a URL with a hollow parent segment when it reaches
            # get_gps -- or, for a GP, quietly reads as out of scope.
            raise FetchError(url, f"malformed entry: `{key}` is empty")
    return items


def get_zps() -> list:
    """Districts. A hierarchy failure raises rather than yielding no districts."""
    url = f"{BASE_URL}/api/prd/master/v1/getZPList/{STATE_ID}"
    return _checked_entries(
        _response_list(fetch_json(url, build_headers("master")), url), "zpId", url)


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
        entries = _response_list(fetch_json(url, build_headers("master")), url)
        for item in _checked_entries(entries, key, url):
            seen.setdefault(item[key], item)
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
