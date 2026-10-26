import os
import json
import time
import requests
from .config import HEADERS, DEFAULT_TIMEOUT, MAX_RETRIES, INITIAL_BACKOFF

def download_api_data(target_path: str, state_code: str, year: int, lgd_code: str, api_endpoint: str) -> bool:
    """
    Handles API request with exponential backoff and JSON integrity validation.
    Returns True if successful or file exists, False if permanently failed.
    """
    if os.path.exists(target_path):
        return True

    url = f"https://egramswaraj.gov.in/webservice/{api_endpoint}/{state_code}/{year}/{lgd_code}"
    backoff_time = INITIAL_BACKOFF

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    with open(target_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=4)
                    print(f"  [SUCCESS] {target_path}")
                    return True
                except json.JSONDecodeError:
                    print(f"  [FORMAT ERROR] Non-JSON payload for LGD {lgd_code}. Retrying...")
            elif response.status_code == 404:
                print(f"  [NOT FOUND] 404 for LGD {lgd_code}. Skipping file.")
                return False
            else:
                print(f"  [SERVER ERROR] {response.status_code} for LGD {lgd_code}. Retrying...")
                
        except requests.exceptions.RequestException as e:
            print(f"  [NETWORK ERROR] Attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
        
        time.sleep(backoff_time)
        backoff_time *= 2
        
    print(f"  [FAILED] Permanently failed to download {target_path} after {MAX_RETRIES} attempts.")
    return False