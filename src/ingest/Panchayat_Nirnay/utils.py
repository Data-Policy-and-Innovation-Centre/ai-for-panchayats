import json
from pathlib import Path

def get_start_index(progress_file: Path) -> int:
    """Reads the interim state file to determine where the scraper left off."""
    if progress_file.exists():
        with open(progress_file, 'r') as f:
            data = json.load(f)
            return data.get("last_completed_index", -1) + 1
    return 0

def save_progress(progress_file: Path, index: int):
    """Saves the current index to the interim state file."""
    # Ensure the parent directory exists before saving
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    with open(progress_file, 'w') as f:
        json.dump({"last_completed_index": index}, f)