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


def test_an_empty_page_short_of_the_reported_count_raises(register, monkeypatch):
    """An empty page before the count is reached is pagination failing.

    Breaking out here returned the pages collected so far as a complete
    register: no FetchError, so the retry and failure-manifest paths never
    run, and the stage exits green with payments missing. The sibling test
    above covers a page that errors; this covers one that succeeds and lies.
    """
    pages = [
        {"response": {"epos": [{"id": i} for i in range(50)], "count": 60}},
        {"response": {"epos": [], "count": 60}},
    ]
    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"]["skip"])
        page = pages[len(calls) - 1]
        return type("R", (), {"status_code": 200, "text": "",
                              "json": lambda self, p=page: p})()

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(FetchError, match="50 of 60"):
        register.get_epayment_orders(119598, "2024-2025")


def test_an_empty_first_page_with_no_reported_count_is_a_valid_empty_register(
    register, monkeypatch
):
    """A GP-year with no payments must still be allowed to return nothing."""
    def fake_post(url, **kwargs):
        page = {"response": {"epos": [], "count": 0}}
        return type("R", (), {"status_code": 200, "text": "",
                              "json": lambda self, p=page: p})()

    monkeypatch.setattr(requests, "post", fake_post)
    assert register.get_epayment_orders(119598, "2024-2025") == []


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


def test_a_clean_rerun_removes_a_previous_run_s_failure_manifest(register, monkeypatch, tmp_path):
    """A stale manifest beside complete output misreports the latest extraction."""
    stale = tmp_path / "panchayat_payment_register_FAILED_gp_years.json"
    stale.write_text('[{"gp_id": 119598, "fin_year": "2024-2025"}]')

    monkeypatch.setattr(register, "get_zps", lambda: [])
    register.main()

    assert not stale.exists(), "the previous run's manifest survived a clean rerun"


def test_a_failed_rerun_still_writes_the_manifest(register, monkeypatch, tmp_path):
    """Removing the stale file must not disable the manifest itself."""
    manifest = tmp_path / "panchayat_payment_register_FAILED_gp_years.json"

    monkeypatch.setattr(register, "get_zps",
                        lambda: [{"zpId": 321, "name": "Khordha"}])
    monkeypatch.setattr(register, "get_blocks",
                        lambda *a, **k: [{"bpId": 3823, "name": "Bhubaneswar"}])
    monkeypatch.setattr(register, "get_gps",
                        lambda *a, **k: [{"gpId": 119598, "name": "Andhrua"}])

    def boom(*a, **k):
        raise FetchError("https://example.invalid/x", "HTTP 500", 500)
    monkeypatch.setattr(register, "get_epayment_orders", boom)

    with pytest.raises(IncompleteRun):
        register.main()

    assert manifest.exists(), "a failing run must still record its gaps"


# --------------------------------------------------------------------------
# Envelope validation: a 200 that says nothing is not an empty result
# --------------------------------------------------------------------------


def test_a_payment_error_envelope_is_not_an_empty_register(register, monkeypatch):
    """epos=[]/count=0 from a missing `response` looked like a clean empty run.

    That bypassed the retry and failure-manifest paths, overwrote an existing
    register with nothing, and then let the stale-manifest cleanup report the
    rerun as successful -- three silent failures from one default.
    """
    def fake_post(url, **kwargs):
        page = {"status": "error", "message": "invalid key"}
        return type("R", (), {"status_code": 200, "text": "",
                              "json": lambda self, p=page: p})()

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(FetchError, match="malformed envelope"):
        register.get_epayment_orders(119598, "2024-2025")


def test_a_payment_envelope_missing_only_epos_also_raises(register, monkeypatch):
    """`response` present but without its list is drift too, not an empty run."""
    def fake_post(url, **kwargs):
        page = {"response": {"count": 10}}
        return type("R", (), {"status_code": 200, "text": "",
                              "json": lambda self, p=page: p})()

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(FetchError, match="no `response.epos`"):
        register.get_epayment_orders(119598, "2024-2025")


def test_a_payment_page_without_a_count_raises(register, monkeypatch):
    """Defaulting a missing count to 0 capped the register at one page."""
    def fake_post(url, **kwargs):
        page = {"response": {"epos": [{"id": i} for i in range(50)]}}
        return type("R", (), {"status_code": 200, "text": "",
                              "json": lambda self, p=page: p})()

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(FetchError, match="`response.count`"):
        register.get_epayment_orders(119598, "2024-2025")


@pytest.fixture
def activities(monkeypatch, tmp_path):
    module = importlib.import_module("ingest.meri_panchayat.work_activities")
    monkeypatch.setattr(module, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    return module


def test_an_activity_error_envelope_raises(activities, monkeypatch):
    def fake_get(url, **kwargs):
        page = {"status": "error"}
        return type("R", (), {"status_code": 200, "text": "",
                              "json": lambda self, p=page: p})()

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(FetchError, match="malformed envelope"):
        activities.get_activities(119598, "2024-2025")


def test_an_activity_page_without_a_count_raises(activities, monkeypatch):
    def fake_get(url, **kwargs):
        page = {"response": {"activities": [{"id": i} for i in range(100)]}}
        return type("R", (), {"status_code": 200, "text": "",
                              "json": lambda self, p=page: p})()

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(FetchError, match="`response.count`"):
        activities.get_activities(119598, "2024-2025")


def test_activity_pagination_short_of_the_count_raises(activities, monkeypatch):
    """The payment register's bug, in the sibling module that shared the shape."""
    pages = [
        {"response": {"activities": [{"id": i} for i in range(100)], "count": 150}},
        {"response": {"activities": [], "count": 150}},
    ]
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        page = pages[len(calls) - 1]
        return type("R", (), {"status_code": 200, "text": "",
                              "json": lambda self, p=page: p})()

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(FetchError, match="100 of 150"):
        activities.get_activities(119598, "2024-2025")


def test_an_activity_page_with_no_records_and_no_count_is_valid(activities, monkeypatch):
    def fake_get(url, **kwargs):
        page = {"response": {"activities": [], "count": 0}}
        return type("R", (), {"status_code": 200, "text": "",
                              "json": lambda self, p=page: p})()

    monkeypatch.setattr(requests, "get", fake_get)
    assert activities.get_activities(119598, "2024-2025") == []


def test_no_adapter_defaults_a_missing_response_to_empty():
    """The defect that survived three review rounds in three different files.

    `data.get("response", {}).get(field, [])` reads a 200 error envelope as an
    empty result, so the adapter publishes an empty dataset and the
    orchestrator reports success. It was fixed one file at a time, and each
    time a sibling still had it. This asserts the pattern is gone everywhere
    and cannot come back in adapter number six.
    """
    import re
    from pathlib import Path

    pkg = Path(__file__).resolve().parents[1] / "src" / "ingest" / "meri_panchayat"
    pattern = re.compile(r'get\(\s*["\']response["\']\s*,\s*\{\}\s*\)')
    offenders = [
        path.name for path in sorted(pkg.glob("*.py"))
        # base_scraper quotes the pattern in `response_field`'s docstring to
        # say what it replaces, which is the one place it should appear.
        if path.name != "base_scraper.py" and pattern.search(path.read_text())
    ]

    assert not offenders, (
        f"{offenders} default a missing `response` to empty; "
        "use base_scraper.response_field so drift raises FetchError"
    )


@pytest.mark.parametrize("module_name", ["panchayat_funds", "beneficiaries"])
def test_a_reorganized_gp_is_scraped_once_where_rows_carry_no_block(
    module_name, monkeypatch, tmp_path
):
    """A GP under two blocks must not double a fund or beneficiary total.

    get_gps unions across years per block, so a GP that changed block appears
    under both. These two adapters record no bp_id, so the duplicate rows are
    byte-identical and there is no way to tell them apart downstream. The
    adapters that do record bp_id have a live question about which parent is
    authoritative, tracked in #42; here there is no question to ask.
    """
    module = importlib.import_module(f"ingest.meri_panchayat.{module_name}")
    monkeypatch.setattr(module, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    monkeypatch.setattr(module, "get_zps", lambda: [{"zpId": 321, "name": "Khordha"}])
    monkeypatch.setattr(module, "get_blocks", lambda *a, **k: [
        {"bpId": 3823, "name": "Old Block"},
        {"bpId": 3824, "name": "New Block"},
    ])
    # The same in-scope GP is returned under both blocks.
    monkeypatch.setattr(module, "get_gps",
                        lambda *a, **k: [{"gpId": 119598, "name": "Andhrua"}])

    fetched = []
    fetcher = "get_funds" if module_name == "panchayat_funds" else "get_beneficiaries"

    def record(gp_id, *args, **kwargs):
        fetched.append((gp_id, args))
        return []

    monkeypatch.setattr(module, fetcher, record)
    module.main()

    gp_ids = [gp for gp, _ in fetched]
    assert gp_ids.count(119598) == len(set(a for _, a in fetched)), (
        f"GP 119598 was fetched {gp_ids.count(119598)} times across "
        f"{len(set(a for _, a in fetched))} distinct year(s) -- the second "
        "block re-scraped it"
    )


def test_an_adapter_checkpoint_writes_beside_the_output_not_over_it(
    monkeypatch, tmp_path
):
    """Drives a real adapter past SAVE_EVERY_GP.

    The first version of this change passed `checkpoint=True` to
    `pd.DataFrame` instead of `save_outputs`, which raises TypeError at the
    first checkpoint -- and the whole suite still passed, because nothing
    exercised an adapter's checkpoint branch. Testing `save_outputs` directly
    is not enough; the call site has to run.
    """
    import pandas as pd

    module = importlib.import_module("ingest.meri_panchayat.village_population")
    final = tmp_path / "village_population.json"
    monkeypatch.setattr(module, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(module, "OUTPUT_FILE_JSON", final)
    monkeypatch.setattr(module, "SAVE_EVERY_GP", 1)
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)

    pd.DataFrame([{"gp_id": "previous complete run"}]).to_json(final, orient="records")

    monkeypatch.setattr(module, "get_zps", lambda: [{"zpId": 321, "name": "Khordha"}])
    monkeypatch.setattr(module, "get_blocks",
                        lambda *a, **k: [{"bpId": 3823, "name": "Bhubaneswar"}])
    # Two in-scope GPs: the first checkpoints, the second fails.
    monkeypatch.setattr(module, "get_gps", lambda *a, **k: [
        {"gpId": 116350, "name": "Hirlipali"},
        {"gpId": 116397, "name": "Bandhpali"},
    ])

    calls = {"n": 0}

    def villages(gp_id, *a, **k):
        calls["n"] += 1
        if calls["n"] > 1:
            raise FetchError("https://example.invalid/x", "HTTP 500", 500)
        return [{"name": "Hirlipali", "population": 1200}]

    monkeypatch.setattr(module, "get_villages", villages)

    with pytest.raises((FetchError, IncompleteRun, SystemExit)):
        module.main()

    surviving = pd.read_json(final)
    assert surviving.iloc[0]["gp_id"] == "previous complete run", (
        "a mid-run checkpoint replaced the last complete output"
    )

