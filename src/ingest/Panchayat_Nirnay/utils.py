import re

def safe_name(name: str) -> str:
    """Removes illegal Windows folder characters and strips trailing spaces"""
    return re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()