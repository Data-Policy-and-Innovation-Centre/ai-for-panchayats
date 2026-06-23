SOURCE_PORTAL = "https://sabhasaar.panchayat.gov.in/api"
STATE_CODE = "21" # Odisha

FILTERS = {
    # fin_year is empty so it scrapes every year. 
    # If you want to scrape a specific year, you can set it to a specific value like "2025-2026"
    "fin_year": "",
    # 0 indicates all meeting types      
    "meeting_type": "0"  
}

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}