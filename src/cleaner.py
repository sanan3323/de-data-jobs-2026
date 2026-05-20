import json
import re
import duckdb
import pandas as pd
from loguru import logger
from lingua import Language, LanguageDetectorBuilder

languages = [Language.ENGLISH, Language.GERMAN]
detector = LanguageDetectorBuilder.from_languages(*languages).build()

DB_PATH = "data/db/jobs.duckdb"

def clean_title(raw_json_str: str) -> str:
    """Extracts the title directly from JSON and cleans it."""
    if not raw_json_str or pd.isna(raw_json_str):
        return "Unknown"
    
    try:
        data = json.loads(raw_json_str)
        title = data.get('stellenangebotsTitel', '')
        if not title:
            title = data.get('titel', 'Unknown')
            
        # Clean out (m/w/d) and remote/location suffixes
        title = re.sub(r'\([m|w|d|f|x|/|,|\s]+\)', '', str(title), flags=re.IGNORECASE)
        title = re.sub(r'[-/|]+\s*(remote|homeoffice|hybrid|münchen|berlin).*$', '', title, flags=re.IGNORECASE)
        
        return title.strip()
    except Exception:
        return "Unknown"

def extract_description(raw_json_str: str) -> str:
    """Safely extracts the job description text from the Bundesagentur JSON."""
    if not raw_json_str or pd.isna(raw_json_str):
        return ""
    
    try:
        data = json.loads(raw_json_str)
        desc = data.get('stellenangebotsBeschreibung', '')
        if not desc:
            desc = data.get('aktuelleVeroeffentlichung', {}).get('beschreibung', '')  
        clean_text = re.sub(r'<[^>]+>', ' ', desc)
        return " ".join(clean_text.split())
    except Exception:
        return ""

def detect_language(text: str) -> str:
    """Returns 'en', 'de', or 'unknown' based on the job description text."""
    if not text or len(text) < 20:
        return "unknown"
    
    lang = detector.detect_language_of(text)
    if lang == Language.ENGLISH:
        return "en"
    elif lang == Language.GERMAN:
        return "de"
    return "unknown"

def main():
    logger.info("Connecting to DuckDB...")
    conn = duckdb.connect(DB_PATH)
    
    df = conn.execute("SELECT refnr, employer, city, raw_json FROM jobs_raw").df()
    logger.info(f"Loaded {len(df)} jobs for cleaning.")
    
    logger.info("Extracting and cleaning titles...")
    df['clean_title'] = df['raw_json'].apply(clean_title)  
    
    logger.info("Extracting descriptions from JSON...")
    df['description_text'] = df['raw_json'].apply(extract_description)
    
    logger.info("Detecting languages...")
    df['language'] = df['description_text'].apply(detect_language)
    
    df_clean = df.drop(columns=['raw_json'])
    
    logger.info("Saving to jobs_clean table in DuckDB...")
    conn.execute("DROP TABLE IF EXISTS jobs_clean")
    conn.execute("CREATE TABLE jobs_clean AS SELECT * FROM df_clean")
    
    logger.success("Cleaning complete!")
    conn.close()

if __name__ == "__main__":
    main()
