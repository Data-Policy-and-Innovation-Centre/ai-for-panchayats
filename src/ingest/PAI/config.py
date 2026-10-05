from config import directories

# Output directory -- sourced from the repo's canonical config (root config.py)
OUTPUT_DIR = directories.RAW_DATA / "PAI"

BASE_URL = "https://pai.gov.in"
PAGE_URL = f"{BASE_URL}/PS/Public/TW-GP.aspx?s=2"

# Odisha state ID on the PAI portal
STATE_ID = "21"
# Financial Year ID: 2 = 2023-2024, 1 = 2022-2023
FY_ID = "2"

# API handler endpoints (discovered from DDLFill_v1.js)
DISTRICTS_URL = f"{BASE_URL}/Handlers/Y_Lgd_Districts.ashx?SID={{state_id}}&YID={{fy_id}}"
BLOCKS_URL = f"{BASE_URL}/Handlers/Y_LGD_Blocks.ashx?SID={{state_id}}&ZID={{district_id}}&YID={{fy_id}}"

# Theme column names (from the HTML table header)
THEME_COLUMNS = [
    "overall_pai_score",
    "t1_poverty_free_enhanced_livelihoods",
    "t2_healthy_panchayat",
    "t3_child_friendly_panchayat",
    "t4_water_sufficient_panchayat",
    "t5_clean_green_panchayat",
    "t6_self_sufficient_infrastructure",
    "t7_socially_just_secured_panchayat",
    "t8_good_governance",
    "t9_women_friendly_panchayat",
]

# Retry / rate-limiting settings
MAX_RETRIES = 3
RETRY_DELAY = 5          # seconds between retries
REQUEST_DELAY = 1.0       # seconds between successive requests
REQUEST_TIMEOUT = 60       # seconds

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;"
        "q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Origin": BASE_URL,
    "Referer": PAGE_URL,
    "Sec-Ch-Ua": '"Chromium";v="154", "Google Chrome";v="154", "Not A(Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
