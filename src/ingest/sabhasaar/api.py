import time
import requests

def fetch_json(url: str, headers: dict, params: dict = None):
    """Executes GET requests with basic rate limiting and error handling."""
    try:
        time.sleep(0.5)
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.status_code == 200:
            return response.json()
        print(f"Error fetching {url}: Status {response.status_code}")
    except Exception as e:
        print(f"Connection exception for {url}: {e}")
    return None