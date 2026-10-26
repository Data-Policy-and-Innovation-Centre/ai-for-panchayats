import os

# --- PATH CONFIGURATION ---
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
LGD_DATA_FILE = os.path.join(PACKAGE_DIR, "lgd_codes.json")

# Target output path under data/raw/eGramSwaraj_Data/
BASE_DIR = os.path.join("data", "raw", "eGramSwaraj_Data")
PROGRESS_FILE = os.path.join(BASE_DIR, "scraping_progress.json")

# --- SCRAPER PARAMETERS ---
YEARS = [2020, 2021, 2022, 2023, 2024, 2025]

API_TYPES = {
    "PL": "getLbApprovedActivityData",
    "RE": "getLbAllocatedAmountData",
    "TA": "getLbTechnicalApprovalData",
    "AA": "getLbAdmApprovalData",
    "PP": "getLbPhysicalProgressData"
}

# Network settings
DEFAULT_TIMEOUT = 20
MAX_RETRIES = 4
INITIAL_BACKOFF = 2

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json"
}