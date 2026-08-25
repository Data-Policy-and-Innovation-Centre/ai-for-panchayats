"""Panchayat Nirnay request headers.

The volatile values must come from the environment, and nothing identifying
the operator's machine may live in tracked source.
"""

from __future__ import annotations

import re

import pytest

from src.ingest.Panchayat_Nirnay import config

CONFIG_SOURCE = config.__file__


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch):
    monkeypatch.setenv(config.ACCESS_KEY_ENV, "test-access-key")
    monkeypatch.setenv(config.SECRET_KEY_ENV, "test-secret-key")
    monkeypatch.setenv(config.TIMESTAMP_ENV, "01012026000000")


def source() -> str:
    with open(CONFIG_SOURCE, encoding="utf-8") as handle:
        return handle.read()


def code() -> str:
    """Source with comments stripped.

    The scan targets values the module would actually send, so prose that
    merely names a removed header must not trip it.
    """
    return "\n".join(line.split("#", 1)[0] for line in source().splitlines())


# ---------------------------------------------------------------- runtime


def test_headers_come_from_the_environment():
    headers = config.build_headers()

    assert headers["accesskey"] == "test-access-key"
    assert headers["secretkey"] == "test-secret-key"
    assert headers["timestamp"] == "01012026000000"


@pytest.mark.parametrize("env_attr", ["ACCESS_KEY_ENV", "SECRET_KEY_ENV",
                                      "TIMESTAMP_ENV"])
def test_a_missing_value_fails_loudly_at_the_point_of_use(monkeypatch, env_attr):
    """The pair expires; a stale or absent value must not fail silently."""
    name = getattr(config, env_attr)
    monkeypatch.delenv(name, raising=False)

    with pytest.raises(config.MissingCredential, match=name):
        config.build_headers()


def test_headers_resolve_at_call_time_not_import_time(monkeypatch):
    """Refreshing the pair must not require reimporting the module."""
    monkeypatch.setenv(config.SECRET_KEY_ENV, "refreshed")

    assert config.build_headers()["secretkey"] == "refreshed"


def test_extra_headers_merge():
    assert config.build_headers({"X-Test": "1"})["X-Test"] == "1"


# ------------------------------------------------- nothing identifying


def test_no_operator_identifiers_are_sent():
    """device-ip and uuid pinned one operator's address and session in Git."""
    headers = config.build_headers()

    assert "device-ip" not in headers
    assert "uuid" not in headers


def test_no_ip_address_literal_in_tracked_source():
    """A quoted bare IP. The Chrome version in the User-Agent is not one."""
    found = re.findall(r'"(?:\d{1,3}\.){3}\d{1,3}"', code())

    assert found == [], f"IP-shaped literal(s) in config: {found}"


def test_no_long_hex_or_uuid_literal_in_tracked_source():
    text = code()

    assert not re.search(r"\b[0-9a-fA-F]{32,}\b", text)
    assert not re.search(
        r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b", text)


def test_env_example_lists_every_required_variable():
    from pathlib import Path

    repo_root = Path(CONFIG_SOURCE).resolve().parents[3]
    example = (repo_root / ".env.example").read_text(encoding="utf-8")

    for name in (config.ACCESS_KEY_ENV, config.SECRET_KEY_ENV,
                 config.TIMESTAMP_ENV):
        assert name in example, f"{name} missing from .env.example"


# ---------------------------------------------------------------- scope


def test_pilot_scope_is_intact():
    assert len(config.TARGET_GPS) == 20
    assert {gp["gp_code"] for gp in config.TARGET_GPS} >= {119598, 119599}
