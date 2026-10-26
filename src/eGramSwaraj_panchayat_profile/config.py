import os

from config import directories

# Base URLs
URL = "https://egramswaraj.gov.in/knowYourPanchayat.do"

# Output Directory
OUTPUT_DIR = directories.RAW_DATA / "eGramSwaraj_Panchayat_profile"

# Headers for the requests
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://egramswaraj.gov.in",
    "Referer": "https://egramswaraj.gov.in/knowYourPanchayat.do",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
}

# Cookies — resolved at runtime from environment variables.
# See .env.example for the required keys.
# IMPORTANT: Rotate the eGramSwaraj session on the portal — the old
# hardcoded values are burned in git history regardless of this fix.
# These must be set before running the scraper. The scrapper module
# validates they are present at call time.
COOKIES = {
    "JSESSIONID": os.environ.get("EGRAM_JSESSIONID", ""),
    "security": os.environ.get("EGRAM_SECURITY", ""),
}
