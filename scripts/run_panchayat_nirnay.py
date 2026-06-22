import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

from config import directories
from src.ingest.Panchayat_Nirnay.config import TARGET_GPS, HEADERS, MEETINGS_LIST_API, AGENDA_LIST_API, MEETING_DETAILS_API, DOWNLOAD_BASE
from src.ingest.Panchayat_Nirnay.extractor import scrape_catalog

if __name__ == "__main__":
    
    OUTPUT_DATASET = directories.RAW_DATA / "Panchayat_Nirnay_Data"
    
    scrape_catalog(
        target_gps=TARGET_GPS,
        headers=HEADERS,
        list_api=MEETINGS_LIST_API,
        agenda_api=AGENDA_LIST_API, 
        details_api=MEETING_DETAILS_API,
        download_base=DOWNLOAD_BASE,
        output_dir=OUTPUT_DATASET
    )