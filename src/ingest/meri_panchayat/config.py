# config.py
import os
import yaml

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "config.yaml"
)

with open(_CONFIG_PATH, "r", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)

STATE_ID   = _cfg["state"]["id"]
STATE_NAME = _cfg["state"]["name"]

FIN_YEARS          = _cfg["financial_years"]
HIERARCHY_FIN_YEAR = _cfg["hierarchy_fin_year"]

_out        = _cfg["output"]
OUTPUT_ROOT = _out["root"]
OUTPUT_DIR  = os.path.join(OUTPUT_ROOT, _out["folder"])

def get_output_path(script_filename: str):
    stem = script_filename.replace(".py", "")
    return os.path.join(OUTPUT_DIR, f"{stem}.json")

SAVE_EVERY_GP = _cfg["save_every_gp"]

REQUEST_DELAY   = _cfg["request_delay"]
REQUEST_TIMEOUT = _cfg["request_timeout"]

BASE_URL   = _cfg["base_url"]
ACCESS_KEY = _cfg["api"]["access_key"]

_keys = _cfg["api"]["secret_keys"]

MASTER_SECRET_KEY            = _keys["master"]
ACTIVITIES_SECRET_KEY        = _keys["activities"]
ACTIVITY_SUMMARY_SECRET_KEY  = _keys["activity_summary"]
ACTION_PLANS_SECRET_KEY      = _keys["action_plans"]
FUNDS_SECRET_KEY             = _keys["funds"]
BENEFICIARIES_SECRET_KEY     = _keys["beneficiaries"]
GP_PROFILE_SECRET_KEY        = _keys["gp_profile"]

def build_headers(secret_key: str, lang: str = "null-IN", extra: dict = None) -> dict:
    h = {
        "accept":       "application/json, text/plain, */*",
        "accesskey":    ACCESS_KEY,
        "lang":         lang,
        "appversion":   "1.0.0",
        "timestamp":    "aj",
        "secretkey":    secret_key,
        "content-type": "application/json",
        "origin":       "https://meripanchayat.gov.in",
        "user-agent":   "Mozilla/5.0",
    }
    if extra:
        h.update(extra)
    return h