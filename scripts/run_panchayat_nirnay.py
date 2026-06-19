import sys
from pathlib import Path

# Add the project root to the path so it can find the src folder
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

# Import the team's top-level config directories
from config import directories

from src.ingest.Panchayat_Nirnay.config import TARGET_GPS, HEADERS, MEETINGS_LIST_API, MEETING_DETAILS_API, DOWNLOAD_BASE
from src.ingest.Panchayat_Nirnay.extractor import scrape_catalog

if __name__ == "__main__":
    
    # 1. Route the final data to the official raw data directory
    OUTPUT_DATASET = directories.RAW_DATA / "Panchayat_Nirnay"
    
    
    PROGRESS_FILE = directories.RAW_DATA/"Panchayat_Nirnay"/ "panchayat_nirnay_scrape_progress.json"

    scrape_catalog(
        target_gps=TARGET_GPS,
        headers=HEADERS,
        list_api=MEETINGS_LIST_API,
        details_api=MEETING_DETAILS_API,
        download_base=DOWNLOAD_BASE,
        output_dir=OUTPUT_DATASET,
        progress_file=PROGRESS_FILE
    )