"""The endpoint adapters and the orchestrator.

All HTTP is mocked and every output goes to tmp_path.
"""

from __future__ import annotations

import importlib

import pandas as pd
import pytest
import requests

from ingest.meri_panchayat import config
from ingest.meri_panchayat.base_scraper import FetchError, IncompleteRun

ADAPTERS = [
    "village_population", "panchayat_funds", "panchayat_payment_register",
    "action_plans", "activity_summary", "beneficiaries", "work_activities",
]


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch):
    monkeypatch.setenv("MERI_PANCHAYAT_ACCESS_KEY", "test-access-key")
    for env in config._SECRET_ENV.values():
        monkeypatch.setenv(env, f"test-{env.lower()}")


# ---------------------------------------------------------------- imports


@pytest.mark.parametrize("name", ADAPTERS)
def test_every_adapter_imports_as_a_package_module(name):
    """`from config import ...` used to resolve to the repo-root config.py."""
    module = importlib.import_module(f"ingest.meri_panchayat.{name}")

    assert hasattr(module, "main")


@pytest.mark.parametrize("name", ADAPTERS)
def test_no_adapter_mutates_sys_path(name):
    source = (config._CONFIG_PATH.parent / f"{name}.py").read_text(encoding="utf-8")

    assert "sys.path" not in source


@pytest.mark.parametrize("name", ADAPTERS)
def test_every_adapter_applies_the_pilot_scope(name):
    """Only one adapter checked scope, and its check was dead code."""
    source = (config._CONFIG_PATH.parent / f"{name}.py").read_text(encoding="utf-8")

    assert "in_scope(" in source, f"{name} would scrape every GP statewide"


# ---------------------------------------------- payment register pagination


@pytest.fixture
def register(monkeypatch, tmp_path):
    module = importlib.import_module(
        "ingest.meri_panchayat.panchayat_payment_register")
    monkeypatch.setattr(module, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(module, "OUTPUT_FILE_JSON", tmp_path / "register.json")
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    return module


def test_pagination_walks_until_the_reported_count(register, monkeypatch):
    pages = [
        {"response": {"epos": [{"id": i} for i in range(50)], "count": 60}},
        {"response": {"epos": [{"id": i} for i in range(50, 60)], "count": 60}},
    ]
    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"]["skip"])
        page = pages[len(calls) - 1]
        return type("R", (), {"status_code": 200, "text": "",
                              "json": lambda self, p=page: p})()

    monkeypatch.setattr(requests, "post", fake_post)
    orders = register.get_epayment_orders(119598, "2024-2025")

    assert len(orders) == 60
    assert calls == [0, 50]


def test_a_failed_page_never_returns_a_partial_register(register, monkeypatch):
    """Returning page 1 when page 2 fails reports an incomplete run as complete."""
    state = {"n": 0}

    def fake_post(url, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return type("R", (), {
                "status_code": 200, "text": "",
                "json": lambda self: {"response": {"epos": [{"id": 1}] * 50,
                                                  "count": 100}}})()
        return type("R", (), {"status_code": 500, "text": ""})()

    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(FetchError):
        register.get_epayment_orders(119598, "2024-2025")


def test_a_persistent_failure_is_recorded_and_fails_the_run(register,
                                                            monkeypatch, tmp_path):
    """Data collected is kept, the gap is written down, and the run still fails."""
    monkeypatch.setattr(register, "FIN_YEARS", ["2024-2025"])
    monkeypatch.setattr(register, "get_zps",
                        lambda: [{"zpId": 321, "name": "Khordha"}])
    monkeypatch.setattr(register, "get_blocks",
                        lambda *a, **k: [{"bpId": 3823, "name": "Bhubaneswar"}])
    monkeypatch.setattr(register, "get_gps",
                        lambda *a, **k: [{"gpId": 119598, "name": "Andhrua"}])

    attempts = []

    def always_fail(*args, **kwargs):
        attempts.append(1)
        raise FetchError("https://example.invalid/x", "HTTP 500", 500)

    monkeypatch.setattr(register, "get_epayment_orders", always_fail)

    with pytest.raises(IncompleteRun) as excinfo:
        register.main()

    assert len(attempts) == 2                      # tried, retried once
    assert len(excinfo.value.failures) == 1
    assert excinfo.value.failures[0]["financial_year"] == "2024-2025"

    manifest = tmp_path / "panchayat_payment_register_FAILED_gp_years.json"
    assert manifest.exists()
    assert pd.read_json(manifest)["reason"].tolist() == ["HTTP 500"]


def test_a_transient_failure_recovers_on_retry(register, monkeypatch, tmp_path):
    monkeypatch.setattr(register, "FIN_YEARS", ["2024-2025"])
    monkeypatch.setattr(register, "get_zps",
                        lambda: [{"zpId": 321, "name": "Khordha"}])
    monkeypatch.setattr(register, "get_blocks",
                        lambda *a, **k: [{"bpId": 3823, "name": "Bhubaneswar"}])
    monkeypatch.setattr(register, "get_gps",
                        lambda *a, **k: [{"gpId": 119598, "name": "Andhrua"}])

    state = {"n": 0}

    def fail_once(*args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise FetchError("https://example.invalid/x", "HTTP 500", 500)
        return [{"id": 1, "epono": "E1"}]

    monkeypatch.setattr(register, "get_epayment_orders", fail_once)

    register.main()                                 # must not raise

    assert (tmp_path / "panchayat_payment_register_FAILED_gp_years.json"
            ).exists() is False


# ---------------------------------------------------------------- checkpoints


@pytest.mark.parametrize("name", ADAPTERS)
def test_every_adapter_checkpoints_during_a_long_run(name):
    """A run that dies mid-way must not lose everything collected so far."""
    source = (config._CONFIG_PATH.parent / f"{name}.py").read_text(encoding="utf-8")

    assert "SAVE_EVERY_GP" in source, f"{name} has no checkpointing"


# ---------------------------------------------------------------- orchestrator


def test_orchestrator_lists_every_adapter():
    import scripts.main_meri_panchayat as orchestrator

    assert set(orchestrator.STAGES) == set(ADAPTERS)


def test_orchestrator_returns_non_zero_when_a_stage_fails(monkeypatch):
    import scripts.main_meri_panchayat as orchestrator

    def boom(stage):
        raise FetchError("https://example.invalid/x", "HTTP 401", 401)
    monkeypatch.setattr(orchestrator, "run_stage", boom)

    assert orchestrator.main(["--stages", "beneficiaries"]) == 1


def test_orchestrator_reports_a_missing_credential_distinctly(monkeypatch):
    import scripts.main_meri_panchayat as orchestrator

    def boom(stage):
        raise config.MissingCredential("MERI_PANCHAYAT_ACCESS_KEY is not set")
    monkeypatch.setattr(orchestrator, "run_stage", boom)

    assert orchestrator.main(["--stages", "beneficiaries"]) == 2


def test_orchestrator_succeeds_when_every_stage_does(monkeypatch):
    import scripts.main_meri_panchayat as orchestrator

    ran = []
    monkeypatch.setattr(orchestrator, "run_stage", ran.append)

    assert orchestrator.main([]) == 0
    assert ran == orchestrator.STAGES
