import os
import json
from .config import PROGRESS_FILE

def load_progress() -> dict:
    """Loads the progress tracker JSON if it exists."""
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_progress(progress_data: dict) -> None:
    """Saves the progress tracker JSON to disk."""
    os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress_data, f, indent=4)