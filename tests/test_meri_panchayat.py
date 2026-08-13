"""Regression tests for the Meri Panchayat ingestion foundation.

All HTTP is mocked; no test reads data/, writes outside tmp_path, or needs a
credential beyond the fake ones set here.
"""

from __future__ import annotations

import importlib

import pandas as pd
import pytest
import requests

from ingest.meri_panchayat import base_scraper, config
from ingest.meri_panchayat.base_scraper import (FetchError, fetch_json,
                                                get_blocks, get_zps,
                                                save_outputs)


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


# ------------------------------------------------------------- credentials


def test_no_credential_is_tracked_in_config():
    text = (config._CONFIG_PATH).read_text(encoding="utf-8")
    assert "secret_keys" not in text
    for line in text.splitlines():
        assert not line.strip().startswith("access_key:")


def test_missing_credential_raises_instead_of_sending_a_blank_header(monkeypatch):
    monkeypatch.delenv("MERI_PANCHAYAT_ACCESS_KEY", raising=False)
    with pytest.raises(config.MissingCredential, match="MERI_PANCHAYAT_ACCESS_KEY"):
        config.build_headers("master")


def test_headers_carry_the_endpoint_specific_secret():
    headers = config.build_headers("funds")
    assert headers["accesskey"] == "test-access-key"
    assert headers["secretkey"] == "test-meri_panchayat_secret_funds"


# ------------------------------------------------------------- failures


@pytest.mark.parametrize("response", [
    Response(status=401),
    Response(status=500),
    Response(status=200, payload=None),          # unparseable body
])
def test_failed_request_raises_rather_than_returning_empty(monkeypatch, response):
    monkeypatch.setattr(requests, "get", lambda *a, **k: response)
    with pytest.raises(FetchError):
        fetch_json("https://example.invalid/x", {})


def test_transport_error_raises(monkeypatch):
    def boom(*a, **k):
        raise requests.Timeout("timed out")
    monkeypatch.setattr(requests, "get", boom)

    with pytest.raises(FetchError, match="request failed"):
        fetch_json("https://example.invalid/x", {})


def test_hierarchy_failure_does_not_become_zero_districts(monkeypatch):
    """The bug: a 401 here returned [], so every scraper wrote an empty file."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(status=401))
    with pytest.raises(FetchError):
        get_zps()


def test_empty_result_is_distinct_from_failure(monkeypatch):
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: Response(payload={"response": []}))
    assert get_zps() == []


# ------------------------------------------------------------- hierarchy year


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


def test_pinned_hierarchy_year_is_honoured(monkeypatch):
    monkeypatch.setattr(base_scraper, "HIERARCHY_FIN_YEAR", "2020-2021")
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return Response(payload={"response": [{"bpId": 1}]})

    monkeypatch.setattr(requests, "get", fake_get)
    get_blocks(321)

    assert len(seen) == 1 and "fYear=2020-2021" in seen[0]


# ------------------------------------------------------------- scope


def test_pilot_scope_is_twenty_gps():
    assert len(config.TARGET_GP_IDS) == 20
    assert config.in_scope(119598)
    assert not config.in_scope(999999)


def test_all_gps_flag_opens_the_scope(monkeypatch):
    monkeypatch.setattr(config, "ALL_GPS", True)
    assert config.in_scope(999999)


# ------------------------------------------------------------- outputs


def test_output_root_expands_and_stays_under_the_repo():
    assert "~" not in str(config.OUTPUT_DIR)
    assert config.OUTPUT_DIR.is_absolute()
    assert config.OUTPUT_DIR.name == "meri_panchayat"


def test_save_outputs_accepts_the_activity_scraper_call(tmp_path):
    """The bug: work_activities passed three positional args and raised TypeError."""
    frame = pd.DataFrame([{"gp_id": 119598}])
    json_path = tmp_path / "nested" / "activities.json"
    csv_path = tmp_path / "nested" / "activities.csv"

    save_outputs(frame, json_path=json_path, csv_path=csv_path)

    assert json_path.exists() and csv_path.exists()
    assert pd.read_csv(csv_path)["gp_id"].tolist() == [119598]


# ------------------------------------------------------------- imports


@pytest.mark.parametrize("module", [
    "village_population", "panchayat_funds", "panchayat_payment_register",
    "action_plans", "activity_summary", "beneficiaries", "work_activities",
])
def test_every_scraper_imports_as_a_package_module(module):
    """The bug: `from config import ...` resolved to the repo-root config.py."""
    imported = importlib.import_module(f"ingest.meri_panchayat.{module}")
    assert hasattr(imported, "main")


@pytest.mark.parametrize("module", [
    "village_population", "panchayat_funds", "panchayat_payment_register",
    "action_plans", "activity_summary", "beneficiaries", "work_activities",
])
def test_no_scraper_mutates_sys_path(module):
    source = (config._CONFIG_PATH.parent / f"{module}.py").read_text(encoding="utf-8")
    assert "sys.path" not in source
