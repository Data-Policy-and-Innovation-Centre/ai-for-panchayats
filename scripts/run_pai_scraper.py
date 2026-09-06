import sys
import os

# Add src directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from pai_scraper.scrapper import scrape_all

if __name__ == "__main__":
    scrape_all()
