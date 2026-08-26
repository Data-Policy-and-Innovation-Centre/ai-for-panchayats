"""Shared HTTP and persistence helpers for the Meri Panchayat scrapers.

A failed request raises FetchError. It is never reported as an empty result,
because "the API did not answer" and "this panchayat has no records" must not
collapse into the same zero, and an empty output must never overwrite a good
one after an outage.
"""

from __future__ import annotations

import logging
import os

import requests

from .config import (BASE_URL, FIN_YEARS, HIERARCHY_FIN_YEAR, REQUEST_TIMEOUT,
                     gp_key,
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
    if 300 <= response.status_code < 400:
        # requests follows redirects by default and carries custom headers
        # across hosts, so a portal redirect -- or a hijacked one -- would
        # forward `accesskey` and `secretkey` to wherever it points. These are
        # JSON endpoints; a redirect is not a normal answer, so refuse it
        # rather than deciding at runtime which hosts are safe.
        raise FetchError(
            url,
            f"refusing redirect (HTTP {response.status_code} -> "
            f"{response.headers.get('Location', 'unknown')}): "
            "portal credentials must not follow it",
            response.status_code)
    if response.status_code != 200:
        raise FetchError(url, f"HTTP {response.status_code}", response.status_code)
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(url, f"unparseable JSON body ({exc})") from exc


def fetch_json(url: str, headers: dict) -> dict:
    """GET one endpoint. Raises FetchError; never returns None."""
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT,
                                allow_redirects=False)
    except requests.RequestException as exc:
        raise FetchError(url, f"request failed ({exc})") from exc
    return _parse(response, url)


def fetch_json_post(url: str, headers: dict, payload: dict) -> dict:
    """POST one endpoint. Raises FetchError; never returns None."""
    try:
        response = requests.post(url, headers=headers, json=payload,
                                 timeout=REQUEST_TIMEOUT, allow_redirects=False)
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
        if isinstance(identifier, str) and identifier != identifier.strip():
            # Normalising only the dedupe key left the padded original in the
            # node itself, and callers build URLs from that: `" 3823 "` becomes
            # a path segment of encoded spaces rather than the block id. Fix
            # the value, not just the key derived from it.
            item[key] = identifier.strip()
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
            # `_checked_entries` deliberately accepts an id as int or str,
            # because the portal is not consistent between years. Keying the
            # union on the raw value therefore lets 3823 and "3823" survive as
            # two entries and returns the same panchayat twice. Same
            # normalisation as config.in_scope, for the same reason.
            seen.setdefault(gp_key(item[key]), item)
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


def _checkpoint_path(path):
    return path.with_suffix(path.suffix + ".partial")


def save_outputs(df, json_path=None, csv_path=None, checkpoint: bool = False) -> None:
    """Write a frame to JSON and/or CSV, creating parent directories.

    Accepts either order used by the scrapers: save_outputs(df, json_path) and
    save_outputs(df, csv_path=..., json_path=...) both work.

    `checkpoint=True` writes beside the target as `<name>.partial` instead of
    to the target itself. Mid-run checkpoints wrote straight to the canonical
    filename, so a stage that failed after its first checkpoint left a
    previously complete artifact replaced by a partial one under the name
    everything downstream reads -- an incomplete dataset wearing the name of a
    complete one, which is the failure this client exists to prevent. The
    final, unflagged save promotes the run and clears any stale partial.
    """
    promotions = []

    for path, writer in ((json_path, "json"), (csv_path, "csv")):
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)

        if checkpoint:
            target = _checkpoint_path(path)
        else:
            # Write somewhere else and rename in. A direct write to the
            # canonical path truncates it the instant it opens, so an
            # interrupt, a full disk, or a writer raising part-way leaves the
            # previously valid output destroyed -- the data loss the
            # checkpoint sidecar prevents mid-run, reintroduced at the final
            # save. os.replace is atomic within a filesystem, so readers see
            # either the old file or the new one.
            target = path.with_suffix(path.suffix + ".tmp")

        if writer == "json":
            df.to_json(target, orient="records", indent=2, force_ascii=False)
        else:
            df.to_csv(target, index=False)

        if not checkpoint:
            # Collected, not promoted yet. Promoting each format as it is
            # written meant a CSV failure after a successful JSON left the two
            # canonical files representing different runs -- a partial replace
            # of a previously consistent output set. Every format is written
            # first; only then does anything move into place.
            promotions.append((target, path))

    # Residual window, deliberately left open: POSIX can rename one path
    # atomically, not two. If the process is killed between these calls, JSON
    # and CSV can still come from different runs. Staging every format first
    # shrinks that window from the whole serialisation -- seconds, and
    # anything a writer can raise -- to the gap between two renames, which is
    # as far as this can go without promoting a whole directory instead of
    # individual files. Consumers that need the two formats to agree should
    # read one of them, not both.
    # Finish every canonical promotion before touching checkpoint sidecars.
    # Cleanup can fail for reasons unrelated to the staged outputs (for
    # example an undeletable or malformed partial path); interleaving it with
    # promotion would leave JSON from the new run beside CSV from the old one.
    for target, path in promotions:
        os.replace(target, path)

    for _, path in promotions:
        stale = _checkpoint_path(path)
        try:
            if stale.exists():
                stale.unlink()
        except OSError as exc:
            logger.warning("Could not remove stale checkpoint %s: %s", stale, exc)
