# scripts/base_scraper.py
# Centralized core engine for Meri Panchayat Data Extraction
# Author: Ravishankar Singh

import requests
from config import (
    BASE_URL, STATE_ID, HIERARCHY_FIN_YEAR, 
    MASTER_SECRET_KEY, REQUEST_TIMEOUT, build_headers
)

MASTER_HEADERS = build_headers(MASTER_SECRET_KEY, lang="null-IN")

def fetch_json(url, headers):
    """Common HTTP GET request handler with safe error tracking."""
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"HTTP {response.status_code}: {url}")
            print(response.text[:500])
            return None
        return response.json()
    except Exception as e:
        print(f"ERROR: {e}")
        return None

def fetch_json_post(url, headers, payload):
    """Common HTTP POST request handler for complex data filters."""
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"HTTP {response.status_code}: {url}")
            return None
        return response.json()
    except Exception as e:
        print(f"ERROR: {e}")
        return None

def get_zps():
    """Fetches the list of all Zilla Parishads (Districts)."""
    url = f"{BASE_URL}/api/prd/master/v1/getZPList/{STATE_ID}"
    data = fetch_json(url, MASTER_HEADERS)
    return data.get("response", []) if data else []

def get_blocks(zp_id):
    """Fetches the list of Blocks within a specific Zilla Parishad."""
    url = (f"{BASE_URL}/api/prd/master/v1/getBlockPanchayatList/"
           f"{STATE_ID}/{zp_id}/P/2?fYear={HIERARCHY_FIN_YEAR}")
    data = fetch_json(url, MASTER_HEADERS)
    return data.get("response", []) if data else []

def get_gps(zp_id, bp_id):
    """Fetches the list of Gram Panchayats within a Block."""
    url = (f"{BASE_URL}/api/prd/master/v1/getGramPanchayatList/"
           f"{STATE_ID}/{zp_id}/{bp_id}/P/3?fYear={HIERARCHY_FIN_YEAR}")
    data = fetch_json(url, MASTER_HEADERS)
    return data.get("response", []) if data else []

def save_outputs(df, json_path=None):
    """Unified file preservation utility."""
    if json_path:
        df.to_json(json_path, orient="records", indent=2, force_ascii=False)