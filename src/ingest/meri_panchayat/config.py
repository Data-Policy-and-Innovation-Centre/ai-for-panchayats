"""Meri Panchayat ingestion settings.

Non-secret settings come from config.yaml next to this module. Credentials come
from the environment and are never tracked; a missing one raises at the moment
it is needed, so a run cannot silently proceed unauthenticated.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
_REPO_ROOT = Path(__file__).resolve().parents[3]

with _CONFIG_PATH.open("r", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)

STATE_ID = _cfg["state"]["id"]
STATE_NAME = _cfg["state"]["name"]

FIN_YEARS = _cfg["financial_years"]

# None means "enumerate the hierarchy for whichever year is being scraped", so
# panchayats created after the pilot's first year are not missed.
HIERARCHY_FIN_YEAR = _cfg.get("hierarchy_fin_year")


def hierarchy_year(fin_year: str) -> str:
    """Year to enumerate blocks/GPs for when scraping fin_year."""
    return HIERARCHY_FIN_YEAR or fin_year


# ---------------------------------------------------------------- output


def _resolve(root: str) -> Path:
    """Expand ~ and resolve a relative root against the repository, not the cwd."""
    path = Path(root).expanduser()
    return path if path.is_absolute() else (_REPO_ROOT / path)


_out = _cfg["output"]
OUTPUT_ROOT = _resolve(_out["root"])
OUTPUT_DIR = OUTPUT_ROOT / _out["folder"]


def get_output_path(script_filename: str) -> Path:
    """data/raw/meri_panchayat/<stem>.json"""
    return OUTPUT_DIR / f"{Path(script_filename).stem}.json"


def output_paths(stem: str) -> tuple[Path, Path]:
    """(csv, json) output paths for one dataset."""
    return OUTPUT_DIR / f"{stem}.csv", OUTPUT_DIR / f"{stem}.json"


# ---------------------------------------------------------------- scope

ALL_GPS = bool(_cfg.get("all_gps", False))
TARGET_GPS = _cfg.get("target_gps") or []
TARGET_GP_IDS = frozenset(gp["gp_code"] for gp in TARGET_GPS)


def in_scope(gp_id) -> bool:
    """True when this GP should be scraped under the configured pilot scope."""
    if ALL_GPS:
        return True
    return gp_id in TARGET_GP_IDS


SAVE_EVERY_GP = _cfg["save_every_gp"]
REQUEST_DELAY = _cfg["request_delay"]
REQUEST_TIMEOUT = _cfg["request_timeout"]
BASE_URL = _cfg["base_url"]

# ---------------------------------------------------------------- credentials

ACCESS_KEY_ENV = "MERI_PANCHAYAT_ACCESS_KEY"
_SECRET_ENV = _cfg["secret_key_env"]


class MissingCredential(RuntimeError):
    """A required API credential is not set in the environment."""


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise MissingCredential(
            f"{name} is not set. Copy .env.example, fill in the values issued "
            f"for this portal, and export them before running the scrapers."
        )
    return value


def secret_key(endpoint: str) -> str:
    """Secret for one endpoint, read from its configured environment variable."""
    try:
        env_var = _SECRET_ENV[endpoint]
    except KeyError:
        raise KeyError(
            f"Unknown endpoint {endpoint!r}; known: {sorted(_SECRET_ENV)}"
        ) from None
    return _require_env(env_var)


def build_headers(endpoint: str, lang: str = "null-IN",
                  extra: dict | None = None) -> dict:
    """Request headers for one endpoint. Credentials resolve at call time."""
    headers = {
        "accept": "application/json, text/plain, */*",
        "accesskey": _require_env(ACCESS_KEY_ENV),
        "lang": lang,
        "appversion": "1.0.0",
        "timestamp": "aj",
        "secretkey": secret_key(endpoint),
        "content-type": "application/json",
        "origin": "https://meripanchayat.gov.in",
        "user-agent": "Mozilla/5.0",
    }
    if extra:
        headers.update(extra)
    return headers
