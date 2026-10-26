import os
from .config import YEARS, API_TYPES
from .api import download_api_data
from .utils import save_progress

def process_tier(entities: list, base_dir: str, tier_name_key: str, lgd_code_key: str, state_code: str, progress: dict):
    """Processes extraction for a given administrative tier (ZP/BP/GP)."""
    for entity in entities:
        name = entity[tier_name_key].replace(" ", "_").replace("/", "_")
        lgd_code = str(entity[lgd_code_key])
        
        if progress.get(lgd_code) == "completed":
            continue

        folder = os.path.join(base_dir, f"LGD_{lgd_code}_{name}")
        os.makedirs(folder, exist_ok=True)
        print(f"\nProcessing {name} (LGD: {lgd_code})...")

        all_files_success = True

        for year in YEARS:
            for acronym, endpoint in API_TYPES.items():
                file_path = os.path.join(folder, f"{year}_{acronym}.json")
                
                success = download_api_data(file_path, state_code, year, lgd_code, endpoint)
                if not success:
                    all_files_success = False

        if all_files_success:
            progress[lgd_code] = "completed"
            save_progress(progress)