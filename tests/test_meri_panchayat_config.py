"""Configuration and credential handling for the Meri Panchayat adapters.

No test reads data/, touches a network, or uses a real credential.
"""

from __future__ import annotations

import pytest

from ingest.meri_panchayat import config


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch):
    monkeypatch.setenv("MERI_PANCHAYAT_ACCESS_KEY", "test-access-key")
    for env in config._SECRET_ENV.values():
        monkeypatch.setenv(env, f"test-{env.lower()}")


# ---------------------------------------------------------------- credentials


def test_no_credential_is_tracked_in_config():
    """The tracked YAML carries variable names, never values."""
    text = config._CONFIG_PATH.read_text(encoding="utf-8")

    assert "secret_keys" not in text
    for line in text.splitlines():
        assert not line.strip().startswith("access_key:")


def test_missing_credential_raises_instead_of_sending_a_blank_header(monkeypatch):
    monkeypatch.delenv("MERI_PANCHAYAT_ACCESS_KEY", raising=False)

    with pytest.raises(config.MissingCredential, match="MERI_PANCHAYAT_ACCESS_KEY"):
        config.build_headers("master")


def test_missing_endpoint_secret_raises(monkeypatch):
    monkeypatch.delenv("MERI_PANCHAYAT_SECRET_FUNDS", raising=False)

    with pytest.raises(config.MissingCredential, match="MERI_PANCHAYAT_SECRET_FUNDS"):
        config.build_headers("funds")


def test_headers_carry_the_endpoint_specific_secret():
    headers = config.build_headers("funds")

    assert headers["accesskey"] == "test-access-key"
    assert headers["secretkey"] == "test-meri_panchayat_secret_funds"


def test_unknown_endpoint_is_rejected():
    with pytest.raises(KeyError, match="nonexistent"):
        config.secret_key("nonexistent")


def test_env_example_lists_every_configured_variable():
    """A contributor copying .env.example must get a complete set."""
    example = (config._REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert config.ACCESS_KEY_ENV in example
    for env in config._SECRET_ENV.values():
        assert env in example, f"{env} missing from .env.example"


# ---------------------------------------------------------------- scope


def test_pilot_scope_is_twenty_gps():
    assert len(config.TARGET_GP_IDS) == 20
    assert config.in_scope(119598)
    assert not config.in_scope(999999)


def test_all_gps_flag_opens_the_scope(monkeypatch):
    """Widening beyond the pilot must be a config decision, not an accident."""
    monkeypatch.setattr(config, "ALL_GPS", True)

    assert config.in_scope(999999)


# ---------------------------------------------------------------- hierarchy


@pytest.mark.parametrize("bad", ["false", "true", "no", 0, 1, None, []])
def test_a_non_boolean_all_gps_is_rejected_rather_than_coerced(bad):
    """`bool("false")` is True, so coercion fails open.

    A quoted `all_gps: "false"` is easy to write by hand or emit from a
    template. Coerced, it would admit every GP and turn the 20-GP pilot into a
    statewide scrape of a government portal, silently. The scope flag is the
    one value that must never be guessed at.
    """
    with pytest.raises(config.ConfigError) as excinfo:
        config._strict_bool(bad, "all_gps")
    assert "all_gps" in str(excinfo.value)


@pytest.mark.parametrize("value", [True, False])
def test_a_real_boolean_all_gps_passes_through(value):
    assert config._strict_bool(value, "all_gps") is value


def test_the_shipped_config_uses_a_real_boolean():
    """Guards the file itself, not just the helper."""
    assert isinstance(config.ALL_GPS, bool)
    assert config.ALL_GPS is False, "the pilot scope must ship closed"


def test_hierarchy_year_defaults_to_the_year_being_scraped():
    """Pinning one year hides panchayats created later."""
    assert config.HIERARCHY_FIN_YEAR is None
    assert config.hierarchy_year("2025-2026") == "2025-2026"


def test_pinned_hierarchy_year_overrides(monkeypatch):
    monkeypatch.setattr(config, "HIERARCHY_FIN_YEAR", "2020-2021")

    assert config.hierarchy_year("2025-2026") == "2020-2021"


# ---------------------------------------------------------------- outputs


def test_output_root_expands_and_is_absolute():
    """os.path.join and to_json do not expand ~, so it must be gone by now."""
    assert "~" not in str(config.OUTPUT_DIR)
    assert config.OUTPUT_DIR.is_absolute()
    assert config.OUTPUT_DIR.name == "meri_panchayat"


def test_relative_output_root_resolves_against_the_repo_not_the_cwd(tmp_path,
                                                                   monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert config._resolve("data/raw").is_relative_to(config._REPO_ROOT)


def test_output_paths_pair_csv_and_json():
    csv_path, json_path = config.output_paths("activities")

    assert csv_path.name == "activities.csv"
    assert json_path.name == "activities.json"
    assert csv_path.parent == json_path.parent == config.OUTPUT_DIR


def test_get_output_path_uses_the_module_stem():
    assert config.get_output_path("beneficiaries.py").name == "beneficiaries.json"


@pytest.mark.parametrize("gp_id", [116350, "116350", " 116350 "])
def test_scope_matches_whatever_type_the_portal_sends(gp_id):
    """The targets are YAML ints; the hierarchy may serialise gpId either way.

    A raw `in` against a frozenset of ints matched nothing the moment the API
    sent strings, and every adapter would then publish an empty output while
    the pipeline reported success -- indistinguishable from a pilot with no
    GPs in that block.
    """
    assert config.in_scope(gp_id) is True


@pytest.mark.parametrize("gp_id", [999999, "999999", None])
def test_out_of_scope_stays_out_whatever_the_type(gp_id):
    assert config.in_scope(gp_id) is False

