import base64
import json
import time
from datetime import datetime
from pathlib import Path

import duckdb
import requests
from loguru import logger
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

# Configuration
BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4"
HEADERS = {
    "X-API-Key": "jobboerse-jobsuche",
    "User-Agent": "DE-Data-Jobs-Market-Research/1.0 (Contact: via Github)",
    "Accept": "application/json"
}


KEYWORDS = [
    "Data Scientist", "Data Analyst", "Data Engineer", "Machine Learning", 
    "ML Engineer", "AI Engineer", "MLOps", "Business Intelligence",
    "Junior Data", "Werkstudent Data", "Praktikum Data", "Trainee Data"
]

BUNDESLAENDER = [
    "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen",
    "Hamburg", "Hessen", "Mecklenburg-Vorpommern", "Niedersachsen", 
    "Nordrhein-Westfalen", "Rheinland-Pfalz", "Saarland", "Sachsen", 
    "Sachsen-Anhalt", "Schleswig-Holstein", "Thüringen"
]

# Set up paths
TODAY = datetime.now().strftime("%Y-%m-%d")
RAW_DIR = Path("data/raw") / TODAY
DETAILS_DIR = RAW_DIR / "details"
DETAILS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = "data/db/jobs.duckdb"

# Initialize DuckDB tables
def init_db():
    conn = duckdb.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs_metadata (
            refnr VARCHAR,
            collection_date DATE,
            source_keyword VARCHAR,
            source_city VARCHAR,
            PRIMARY KEY (refnr, collection_date, source_keyword)
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs_raw (
            refnr VARCHAR PRIMARY KEY,
            title VARCHAR,
            employer VARCHAR,
            city VARCHAR,
            raw_json JSON,
            collected_at TIMESTAMP
        );
    """)
    return conn

class APIError(Exception):
    pass

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type((requests.exceptions.RequestException, APIError))
)
def search_jobs(keyword: str, region: str, page: int = 1) -> dict:
    """Searches the API with pagination. Triggers tenacity on network/5xx errors."""
    url = f"{BASE_URL}/jobs"
    params = {
        "was": keyword,
        "wo": region,   
        "page": page,
        "size": 100
    }
    
    logger.info(f"Searching: '{keyword}' in {region} (Page {page})")
    response = requests.get(url, headers=HEADERS, params=params, timeout=10)
    
    if response.status_code >= 500:
        raise APIError(f"API returned {response.status_code}")
    response.raise_for_status()
    
    time.sleep(1)  
    return response.json()

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type((requests.exceptions.RequestException, APIError))
)
def fetch_job_details(refnr: str) -> dict:
    """Fetches the full JD text using base64 encoded refnr."""
    encoded_refnr = base64.b64encode(refnr.encode('utf-8')).decode('utf-8')
    url = f"{BASE_URL}/jobdetails/{encoded_refnr}"
    
    response = requests.get(url, headers=HEADERS, timeout=10)
    
    if response.status_code >= 500:
        raise APIError(f"API returned {response.status_code}")
    if response.status_code == 404:
        logger.warning(f"Job {refnr} no longer found (404).")
        return {}
        
    response.raise_for_status()
    time.sleep(1) # Polite rate limiting
    return response.json()

def main():
    logger.add(f"logs/collection_{TODAY}.log", rotation="10 MB")
    conn = init_db()
    
    existing_refnrs = set(row[0] for row in conn.execute("SELECT refnr FROM jobs_raw").fetchall())
    new_jobs_count = 0

    for region in BUNDESLAENDER:
        for keyword in KEYWORDS:
            page = 1
            max_pages = 1
            
            while page <= max_pages:
                try:
                    search_results = search_jobs(keyword, region, page)
                except Exception as e:
                    logger.error(f"Failed to fetch {keyword} in {region} page {page}: {e}")
                    break
                
                if page == 1:
                    total_results = search_results.get('maxErgebnisse', 0)
                    max_pages = search_results.get('maxPage', 1)
                    logger.info(f"Found {total_results} total results for '{keyword}' in {region} ({max_pages} pages)")
                
                jobs = search_results.get('stellenangebote', [])
                if not jobs:
                    break

                for job in jobs:
                    refnr = job.get('refnr')
                    if not refnr:
                        continue
                    
                    # Store the 'region' in the source_city column so we don't have to rebuild the database
                    conn.execute("""
                        INSERT OR IGNORE INTO jobs_metadata (refnr, collection_date, source_keyword, source_city)
                        VALUES (?, ?, ?, ?)
                    """, (refnr, TODAY, keyword, region))
                    
                    if refnr not in existing_refnrs:
                        try:
                            details = fetch_job_details(refnr)
                            if not details:
                                continue
                            
                            file_path = DETAILS_DIR / f"{refnr}.json"
                            file_path.write_text(json.dumps(details, ensure_ascii=False, indent=2))
                            
                            # Insert into DuckDB
                            conn.execute("""
                                INSERT INTO jobs_raw (refnr, title, employer, city, raw_json, collected_at)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (
                                refnr, 
                                details.get('titel', ''), 
                                details.get('arbeitgeber', ''), 
                                details.get('arbeitsorte', [{}])[0].get('ort', ''),
                                json.dumps(details),
                                datetime.now()
                            ))
                            
                            existing_refnrs.add(refnr)
                            new_jobs_count += 1
                            logger.info(f"Successfully collected details for {refnr}")
                            
                        except Exception as e:
                            logger.error(f"Failed to fetch details for {refnr}: {e}")
                            
                page += 1

    logger.success(f"Collection complete for {TODAY}. Added {new_jobs_count} new job descriptions.")
    conn.close()

if __name__ == "__main__":
    main()
