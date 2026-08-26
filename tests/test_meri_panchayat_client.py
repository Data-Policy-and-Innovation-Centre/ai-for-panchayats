"""The shared HTTP client.

A failed request must never look like an empty result. All HTTP is mocked; no
test reaches the network.
"""

from __future__ import annotations

import pytest
import requests

from ingest.meri_panchayat import base_scraper, config
from ingest.meri_panchayat.base_scraper import (FetchError, fetch_json,
                                                fetch_json_post, get_blocks,
                                                get_gps, get_zps, save_outputs)


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch):
    monkeypatch.setenv("MERI_PANCHAYAT_ACCESS_KEY", "test-access-key")
    for env in config._SECRET_ENV.values():
        monkeypatch.setenv(env, f"test-{env.lower()}")


class Response:
    def __init__(self, status=200, payload=None, body="{}"):
        self.status_code = status
        self._payload = payload
        self.text = body

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value")
        return self._payload


# ---------------------------------------------------------------- failures


@pytest.mark.parametrize("response,reason", [
    (Response(status=401), "HTTP 401"),
    (Response(status=429), "HTTP 429"),
    (Response(status=500), "HTTP 500"),
    (Response(status=200, payload=None), "unparseable JSON"),
])
def test_failed_request_raises_rather_than_returning_empty(monkeypatch, response,
                                                           reason):
    monkeypatch.setattr(requests, "get", lambda *a, **k: response)

    with pytest.raises(FetchError) as excinfo:
        fetch_json("https://example.invalid/x", {})

    assert reason in str(excinfo.value)


def test_fetch_error_carries_the_status_for_the_caller(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(status=401))

    with pytest.raises(FetchError) as excinfo:
        fetch_json("https://example.invalid/x", {})

    assert excinfo.value.status == 401
    assert excinfo.value.url == "https://example.invalid/x"


@pytest.mark.parametrize("exception", [
    requests.Timeout("timed out"),
    requests.ConnectionError("refused"),
])
def test_transport_errors_raise(monkeypatch, exception):
    def boom(*args, **kwargs):
        raise exception
    monkeypatch.setattr(requests, "get", boom)

    with pytest.raises(FetchError, match="request failed"):
        fetch_json("https://example.invalid/x", {})


def test_post_failures_raise_too(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(status=500))

    with pytest.raises(FetchError):
        fetch_json_post("https://example.invalid/x", {}, {})


# ------------------------------------------------- empty versus failed


def test_hierarchy_failure_does_not_become_zero_districts(monkeypatch):
    """A 401 used to return [], after which every adapter wrote an empty file."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(status=401))

    with pytest.raises(FetchError):
        get_zps()


def test_a_genuinely_empty_response_is_a_valid_empty_list(monkeypatch):
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: Response(payload={"response": []}))

    assert get_zps() == []


def test_a_malformed_response_body_is_not_treated_as_empty(monkeypatch):
    """A dict where a list was expected means schema drift, not no records.

    This test previously asserted `get_zps() == []`, which is what its name
    says must not happen. The name and docstring were right and the assertion
    was wrong: coercing schema drift to an empty list is the same
    successful-empty-output failure the 401 case above guards against.
    """
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: Response(payload={"response": {"x": 1}}))

    with pytest.raises(FetchError):
        get_zps()


# ---------------------------------------------------------------- hierarchy


def test_blocks_union_every_configured_year(monkeypatch):
    """A block created after the pilot's first year must still be discovered."""
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        year = url.split("fYear=")[1]
        if year == config.FIN_YEARS[-1]:
            return Response(payload={"response": [{"bpId": 1}, {"bpId": 2}]})
        return Response(payload={"response": [{"bpId": 1}]})

    monkeypatch.setattr(requests, "get", fake_get)
    blocks = get_blocks(321)

    assert sorted(b["bpId"] for b in blocks) == [1, 2]
    assert len(seen) == len(config.FIN_YEARS)


def test_gps_dedupe_across_years(monkeypatch):
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: Response(payload={"response": [{"gpId": 119598}]}))

    gps = get_gps(321, 3823)

    assert len(gps) == 1


def test_a_single_year_can_be_requested_explicitly(monkeypatch):
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return Response(payload={"response": [{"bpId": 1}]})

    monkeypatch.setattr(requests, "get", fake_get)
    get_blocks(321, "2023-2024")

    assert len(seen) == 1 and "fYear=2023-2024" in seen[0]


def test_pinned_hierarchy_year_is_honoured(monkeypatch):
    monkeypatch.setattr(base_scraper, "HIERARCHY_FIN_YEAR", "2020-2021")
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return Response(payload={"response": [{"bpId": 1}]})

    monkeypatch.setattr(requests, "get", fake_get)
    get_blocks(321)

    assert len(seen) == 1 and "fYear=2020-2021" in seen[0]


# ---------------------------------------------------------------- outputs


def test_save_outputs_writes_both_formats_and_creates_directories(tmp_path):
    import pandas as pd

    frame = pd.DataFrame([{"gp_id": 119598}])
    json_path = tmp_path / "nested" / "activities.json"
    csv_path = tmp_path / "nested" / "activities.csv"

    save_outputs(frame, json_path=json_path, csv_path=csv_path)

    assert pd.read_csv(csv_path)["gp_id"].tolist() == [119598]
    assert json_path.exists()


def test_save_outputs_accepts_json_positionally(tmp_path):
    """Several adapters call save_outputs(df, path)."""
    import pandas as pd

    path = tmp_path / "out.json"
    save_outputs(pd.DataFrame([{"a": 1}]), path)

    assert path.exists()


# --------------------------------------------------------------------------
# A malformed envelope is a failure, not an empty hierarchy
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "envelope, why",
    [
        ({"status": "error", "message": "invalid key"}, "200 carrying an error envelope"),
        ({}, "no `response` key at all"),
        ({"response": None}, "`response` present but null"),
        ({"response": "oops"}, "`response` present but not a list"),
        (None, "a body of literal JSON `null`"),
        (5, "a bare JSON number"),
        ("text", "a bare JSON string"),
    ],
)
def test_a_malformed_envelope_raises_instead_of_reporting_no_districts(
    monkeypatch, envelope, why
):
    """Returning [] here is the successful-empty-output bug this client prevents.

    `get_zps` promises in its own docstring that "a hierarchy failure raises
    rather than yielding no districts". Coercing every malformed envelope to
    [] broke that promise silently: a scrape would report success with zero
    districts and overwrite good saved data with nothing.
    """
    monkeypatch.setattr(base_scraper, "fetch_json", lambda url, headers: envelope)
    with pytest.raises(base_scraper.FetchError):
        base_scraper.get_zps()


@pytest.mark.parametrize("entry, why", [
    (None, "a scalar entry would raise AttributeError"),
    ("gp", "a string entry would raise AttributeError"),
    ({"name": "Andhrua"}, "an object with no bpId would be kept under None"),
])
def test_a_malformed_hierarchy_entry_raises(monkeypatch, entry, why):
    """Entry-level drift takes the same typed failure as envelope-level drift."""
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: Response(payload={"response": [entry]}))

    with pytest.raises(FetchError, match="malformed entry"):
        get_blocks(321, "2024-2025")


@pytest.mark.parametrize("entry", [None, "zp", {"name": "Khordha"}])
def test_a_malformed_district_entry_raises_too(monkeypatch, entry):
    """get_zps does not go through _union_over_years, so it needs the same guard.

    Validating inside the union helper alone left this one level returning
    malformed districts as success, where every adapter's `zp.get("zpId")`
    would then fail with an untyped error far from the request that caused it.
    """
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: Response(payload={"response": [entry]}))

    with pytest.raises(FetchError, match="malformed entry"):
        get_zps()


def test_every_hierarchy_level_shares_one_validation_path(monkeypatch):
    """Districts, blocks and GPs must all reject the same malformed entry."""
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: Response(payload={"response": [None]}))

    for call in (get_zps, lambda: get_blocks(321, "2024-2025"),
                 lambda: get_gps(321, 3823, "2024-2025")):
        with pytest.raises(FetchError, match="malformed entry"):
            call()

