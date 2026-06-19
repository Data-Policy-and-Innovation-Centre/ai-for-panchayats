import requests
from pathlib import Path

def download_file(url: str, save_path: Path, headers: dict):
    """Streams a file from the server directly to the disk."""
    print(f"        -> Downloading: {url.split('file_name=')[-1][:30]}...")
    try:
        response = requests.get(url, headers=headers, stream=True, timeout=30)
        response.raise_for_status()
        
        with open(save_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)
        print("        ✓ Saved.")
    except Exception as e:
        print(f"        [!] DOWNLOAD ERROR: {e}")