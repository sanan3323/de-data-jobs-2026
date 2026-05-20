from __future__ import annotations
import json
import os
import re
import time
from typing import Literal

import duckdb
from dotenv import load_dotenv
from loguru import logger
from openai import OpenAI
from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

load_dotenv()

DB_PATH = "data/db/jobs.duckdb"
MODEL = "google/gemini-2.0-flash-001"
MAX_DESC_CHARS = 4000

client = OpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1",
    default_headers={
        "HTTP-Referer": "https://github.com/sanan3323/de-data-jobs-2026",
        "X-Title": "DE Data Jobs Analysis",
    },
)



class LLMExtraction(BaseModel):
    required_skills: list[str]
    nice_to_have_skills: list[str]
    years_experience_min: int | None
    education_level: Literal["bachelor", "master", "phd", "any", "unclear"]
    remote_policy: Literal["remote", "hybrid", "onsite", "unclear"]
    entry_level_friendly_signals: list[str]
    entry_level_unfriendly_signals: list[str]


SENIORITY_PATTERNS = [
    (re.compile(r"\b(praktikum|intern(ship)?)\b", re.I), "intern"),
    (re.compile(r"\bwerkstudent", re.I), "werkstudent"),
    (re.compile(r"\btrainee\b", re.I), "trainee"),
    (re.compile(r"\b(junior|berufseinsteiger|absolvent|graduate|entry[- ]level)\b", re.I), "junior"),
    (re.compile(r"\b(senior|sr\.?|erfahren)\b", re.I), "senior"),
    (re.compile(r"\b(lead|head of|principal|staff|director)\b", re.I), "lead"),
]

ENTRY_LEVEL_TITLE_RE = re.compile(
    r"\b(junior|jr\.?|praktikum|intern(ship)?|werkstudent|trainee|"
    r"berufseinsteiger|absolvent|graduate|entry[- ]level)\b",
    re.I,
)

GERMAN_REQUIRED_PATTERNS = [
    r"deutsch(kenntnisse|sprachkenntnisse)?\s*(in|auf|mit|sind|werden)?\s*(verhandlungssicher|fließend|fluent|sehr gut|c1|c2|b2|muttersprach)",
    r"\b(verhandlungssichere?|fließende?)\s+deutsch",
    r"deutsch\s+(als\s+)?muttersprache",
    r"fluent\s+(in\s+)?german",
    r"german\s+(language\s+)?required",
    r"native\s+german",
]
GERMAN_REQUIRED_RE = re.compile("|".join(GERMAN_REQUIRED_PATTERNS), re.I)

ENGLISH_REQUIRED_PATTERNS = [
    r"english\s+(language\s+)?required",
    r"fluent\s+(in\s+)?english",
    r"englisch(kenntnisse)?\s*(in|auf|mit|sind|werden)?\s*(verhandlungssicher|fließend|fluent|sehr gut|c1|c2|b2)",
    r"\b(verhandlungssichere?|fließende?)\s+englisch",
]
ENGLISH_REQUIRED_RE = re.compile("|".join(ENGLISH_REQUIRED_PATTERNS), re.I)


def extract_seniority_from_title(title: str) -> str:
    """Return one of: intern, werkstudent, trainee, junior, mid, senior, lead, unspecified."""
    if not title:
        return "unspecified"
    for pattern, label in SENIORITY_PATTERNS:
        if pattern.search(title):
            return label
    return "unspecified"


def is_entry_level_titled(title: str) -> bool:
    return bool(title and ENTRY_LEVEL_TITLE_RE.search(title))


def german_required(text: str) -> bool:
    return bool(GERMAN_REQUIRED_RE.search(text or ""))


def english_required(text: str) -> bool:
    return bool(ENGLISH_REQUIRED_RE.search(text or ""))


BOILERPLATE_PATTERNS = [
    # German EEO / diversity statements (low information density)
    r"wir sind ein arbeitgeber.{0,200}chancengleichheit.{0,200}",
    r"schwerbehinderte.{0,150}bewerber.{0,150}",
    # Common benefits sections (signal-poor for analysis)
    r"unsere benefits[:\s].{0,500}",
    r"was wir bieten[:\s].{0,500}",
    r"bei fragen wenden sie sich.{0,200}",
]
BOILERPLATE_RE = re.compile("|".join(BOILERPLATE_PATTERNS), re.I | re.S)


def preprocess_description(text: str) -> str:
    """Strip boilerplate and truncate to MAX_DESC_CHARS."""
    if not text:
        return ""
    text = BOILERPLATE_RE.sub(" ", text)
    text = " ".join(text.split())  # collapse whitespace
    return text[:MAX_DESC_CHARS]


EXTRACTION_PROMPT = """You are extracting structured data from a German or English job posting for a data/AI role. Your job is to be EXHAUSTIVE, not concise. List every specific technology, tool, framework, platform, language, methodology, or technical concept that the posting mentions as a requirement or expectation.

CRITICAL RULES:
- Do NOT default to short generic lists like ["Python", "SQL", "AWS"]. Read the posting and list what's actually there.
- Include all programming languages, databases, cloud platforms, ML frameworks, BI tools, orchestration tools, data warehouses, version control, methodologies (Agile, Scrum), and domain-specific tech.
- Normalize names: "Power BI" not "Microsoft Power BI"; "AWS" not "Amazon Web Services"; "scikit-learn" not "sklearn".
- If a skill appears in both required and nice-to-have sections, put it in required.
- An empty list is acceptable ONLY if the posting genuinely lists no specific technologies. Most data job postings list 5-15 specific skills.
- For years_experience_min: look for phrases like "3+ Jahre Berufserfahrung", "mindestens 5 Jahre", "X years of experience". Return the integer minimum.

Return ONLY valid JSON matching this exact schema:
{{
  "required_skills": [list of must-have technical skills/tools/platforms],
  "nice_to_have_skills": [list of preferred-but-optional skills],
  "years_experience_min": integer or null,
  "education_level": "bachelor" | "master" | "phd" | "any" | "unclear",
  "remote_policy": "remote" | "hybrid" | "onsite" | "unclear",
  "entry_level_friendly_signals": [short phrases indicating openness to juniors, e.g. "no experience required", "Quereinsteiger willkommen", "wir bilden Sie aus"],
  "entry_level_unfriendly_signals": [short phrases indicating high barrier, e.g. "5+ years required", "PhD required", "production ML experience"]
}}

Job title: {title}

Job description:
{description}

Return only the JSON object. No preamble, no markdown fences."""


class RateLimited(Exception):
    pass


@retry(
    retry=retry_if_exception_type(RateLimited),
    stop=stop_after_attempt(6),               
    wait=wait_exponential(multiplier=4, min=4, max=120),  
    reraise=True,
)

def _call_llm(title: str, description: str) -> str:
    prompt = EXTRACTION_PROMPT.format(title=title, description=description)
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You are a precise data extractor. Output only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=1000,  
        )
    except Exception as e:
        msg = str(e).lower()
        if "rate" in msg or "429" in msg or "tokens per" in msg:
            logger.warning(f"Rate limited, backing off: {e}")
            raise RateLimited(str(e)) from e
        raise
    return resp.choices[0].message.content


def extract_one(refnr: str, title: str, raw_description: str) -> dict | None:
    """Combine deterministic + LLM extraction for one job."""
    desc = preprocess_description(raw_description)
    if len(desc) < 50:
        logger.warning(f"{refnr}: description too short, skipping")
        return None

    deterministic = {
        "refnr": refnr,
        "seniority_label": extract_seniority_from_title(title),
        "is_entry_level_titled": is_entry_level_titled(title),
        "german_required": german_required(desc),
        "english_required": english_required(desc),
    }

    try:
        raw_json = _call_llm(title, desc)
        llm_data = LLMExtraction.model_validate_json(raw_json).model_dump()
    except ValidationError as e:
        logger.warning(f"{refnr}: LLM returned invalid schema: {e}")
        return None
    except Exception as e:
        logger.error(f"{refnr}: LLM call failed: {e}")
        return None

    return {**deterministic, **llm_data}


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs_extracted (
            refnr VARCHAR PRIMARY KEY,
            seniority_label VARCHAR,
            is_entry_level_titled BOOLEAN,
            german_required BOOLEAN,
            english_required BOOLEAN,
            education_level VARCHAR,
            remote_policy VARCHAR,
            years_experience_min INTEGER,
            required_skills JSON,
            nice_to_have_skills JSON,
            entry_level_friendly_signals JSON,
            entry_level_unfriendly_signals JSON,
            extracted_at TIMESTAMP
        );
    """)


def main():
    conn = duckdb.connect(DB_PATH)
    init_db(conn)

    existing = {row[0] for row in conn.execute("SELECT refnr FROM jobs_extracted").fetchall()}
    rows = conn.execute("""
        SELECT refnr, clean_title, description_text
        FROM jobs_clean
        WHERE clean_title IS NOT NULL
          AND description_text IS NOT NULL
          AND LENGTH(description_text) > 100
    """).fetchall()
    todo = [r for r in rows if r[0] not in existing]
    logger.info(f"Total cleaned: {len(rows)}. Already extracted: {len(existing)}. To do: {len(todo)}")

    if not todo:
        logger.success("Nothing to extract.")
        conn.close()
        return

    success = 0
    for refnr, title, desc in tqdm(todo, desc="Extracting"):
        result = extract_one(refnr, title, desc)
        if result is None:
            continue
        conn.execute("""
            INSERT INTO jobs_extracted VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP
            )
        """, (
            result["refnr"],
            result["seniority_label"],
            result["is_entry_level_titled"],
            result["german_required"],
            result["english_required"],
            result["education_level"],
            result["remote_policy"],
            result["years_experience_min"],
            json.dumps(result["required_skills"]),
            json.dumps(result["nice_to_have_skills"]),
            json.dumps(result["entry_level_friendly_signals"]),
            json.dumps(result["entry_level_unfriendly_signals"]),
        ))
        success += 1

        time.sleep(0.2)

    logger.success(f"Extracted {success}/{len(todo)}")
    conn.close()


if __name__ == "__main__":
    main()
