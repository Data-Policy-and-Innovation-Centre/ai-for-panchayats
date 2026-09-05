import os
from pathlib import Path

# Base URLs
URL = "https://egramswaraj.gov.in/knowYourPanchayat.do"

# Output Directory
# Determine the project root dynamically. 
# Assuming config.py is at <project_root>/src/eGramSwaraj_panchayat_profile/config.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = PROJECT_ROOT / "data" / "raw" / "eGramSwaraj_Panchayat_profile"

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

# Cookies (these may need to be updated if your session expires)
COOKIES = {
    "_ga": "GA1.1.585595965.1782929135",
    "security": "a9419864-27a3-438a-9bdc-d099bcedc5de",
    "JSESSIONID": "0C5FFFBE4D44E2172BB9C9987EB11282", 
    "key": "value"
}
