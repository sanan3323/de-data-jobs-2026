# What the German Data Job Market Actually Looks Like (May 2026)

I've been job hunting in Germany for a while. After a few hundred applications, I got tired of guessing what the market actually wants and decided to look at the data instead. This repository is the result: a pipeline that collects 2,331 data and AI job postings from the official Bundesagentur für Arbeit API, extracts structured fields from them with an LLM, and analyses the result.

A few findings surprised me. The "Junior roles secretly demand 5 years of experience" claims isn't true here — Junior postings in this dataset average 1.6 years of required experience. English postings are more common in Senior and Lead roles than in Junior ones, which is the opposite of what I expected. Career-changer-friendly postings make up only 1.4% of the market. Three of the four findings in this README contradict something I believed before I started.

The full analysis is in three Jupyter notebooks. The charts below are the headlines.

## Headline findings

### 1. Language is an inverse seniority gate

Across all 2,331 postings, 17.6% are written in English. That share is not evenly distributed across seniority levels:

- **Lead roles: 47.3% in English**
- **Senior: 28.7%**
- **Internship: 31.2%**
- **Junior: 13.3%**
- **Werkstudent: 0%**

The intuition I went in with was that English postings are international startups hiring juniors. The data points the other way. Companies write in English when they're competing globally for scarce senior talent. Junior roles are filled domestically and stay in German. If you don't speak German, your addressable market grows as you become more senior, not shrinks.

### 2. The Junior bottleneck is volume, not gatekeeping

Of the eight Junior-titled postings that explicitly state a minimum experience requirement, the average is 1.6 years. Very few demand three or more. The "Junior really means Senior" meme doesn't show up in this dataset.

What does show up: only a small fraction of postings are explicitly Junior-titled, and 82% of postings (1,910 of 2,331) don't state an experience requirement at all. The market isn't gatekeeping juniors with hidden experience demands. It just isn't advertising many Junior data roles.

### 3. Career changers face a 1.4% door

Only 32 of 2,331 postings (1.4%) are flagged Quereinstieg-friendly by the employer. The door isn't closed, but it's narrow. Anyone planning a pivot through traditional employment routes should expect to need at least one alternative path (portfolio work, internal pivot at a current employer, a Werkstudent or Trainee program) running in parallel.

### 4. Geographic concentration is real but not extreme

The top three cities (Munich, Berlin, Hamburg) account for around 23% of all postings. The HHI for the city-level market lands in the moderately-concentrated range — meaningful but not pathological. What does vary sharply is the *flavor* of each city's market: Munich and Berlin are the most English-friendly, NRW cities (Cologne, Düsseldorf) the least. Nürnberg is an unexpected outlier on remote-friendliness.

## What's in this repository

```
de-data-jobs-2026/
├── src/
│   ├── collector.py       # Bundesagentur API client, resumable
│   ├── cleaner.py         # JSON parsing, location/employer extraction, normalization
│   └── extractor.py       # LLM extraction with token-efficient prompting
├── notebooks/
│   ├── 01_skills_landscape.ipynb   # what the market wants
│   ├── 02_market_geography.ipynb   # where the jobs are
│   └── 03_market_access.ipynb      # who actually gets hired
├── data/
│   └── db/jobs.duckdb     # local DuckDB, all stages of the pipeline
└── figures/               # charts saved from the notebooks
```

The three notebooks are self-contained and answer different questions. Notebook 1 is about skill demand and how it varies by role. Notebook 2 is about geography. Notebook 3 is about structural access barriers — the most personally actionable of the three.

## How the data was built

The pipeline runs in three stages.

**Data Collection**. The scraper searches the Bundesagentur für Arbeit database for specific data roles (like Data Scientist, Data Analyst, Data Engineer, etc.) across major cities. It automatically clicks through all the pages of search results, grabs the full job description for each one, and saves the data locally. To keep the dataset clean, it uses the job's unique ID number to remove duplicates. It is also designed to be efficient—if the script is paused and restarted, it remembers what it has already downloaded and skips them. In total, this process collected 2,334 unique job postings on May 16, 2026.

**Cleaning.** The cleaner parses the raw JSON, extracts the employer name, city, Bundesland, coordinates, working-time fields, contract duration, salary signal, posting date, and the `quereinstieg_friendly` and `homeoffice_allowed` flags that Bundesagentur classifies internally. Job descriptions are HTML-stripped and language-detected with `lingua-py`. Output is a clean `jobs_clean` table in DuckDB.

**LLM extraction.** For each posting, the extractor pulls structured fields the cleaner can't reach from free text — required skills, nice-to-have skills, minimum experience, education level, remote policy, entry-level signals. The extractor uses `google/gemini-2.0-flash-001` via OpenRouter. A few engineering details that matter:

- Description text is truncated to 4,000 characters and boilerplate-stripped (German EEO sections, benefits lists) before being sent. This cuts input tokens by ~50% with no extraction quality loss.
- The Pydantic schema is described inline in the prompt rather than dumped as JSON Schema. ~600 tokens saved per call.
- Fields that can be extracted deterministically — seniority from title, explicit German/English fluency requirements via regex — are handled in Python, not by the LLM. Lower cost, more consistent output.
- Tenacity retry handles 429 rate limits with exponential backoff.

The pipeline processed 2,331 of 2,334 eligible postings successfully — a 99.9% success rate at a total cost of roughly €1 in API fees.

## What I had to fix along the way

A few things broke on the first run that are worth flagging because the diagnostic process is part of what makes this project mine.

**The skill extraction was returning generic 3-item lists in 27% of postings.** First-pass diagnostic showed `["AWS", "Python", "SQL"]` appearing as the exact skill list in nearly 10 separate jobs. Root cause: the JD truncation at 1,500 characters was cutting off the skills section in most German postings (the requirements section appears mid-to-late after the company intro). Fix: bumped truncation to 4,000 characters and rewrote the prompt to explicitly forbid generic lists. Re-validated on the same 20 job sample. Empty arrays dropped from 15% to 0%, average skill list length tripled.

**The cleaner was leaving most of the JSON value on the floor.** First pass populated only a few top-level fields. Adding a JSON extraction step against the raw response surfaced 15 more useful columns including coordinates, quereinstieg flag, homeoffice flag, posting date, working-time breakdown. No re-extraction needed; the raw data was already on disk.

**Title-regex role classification failed on 71% of postings.** German employers write bespoke titles ("Mitarbeiter Datenanalyse im Risikomanagement der Versicherungssparte (m/w/d)"), and no reasonable regex catches the long tail. Switched to Bundesagentur's own `hauptberuf` field, which classifies each posting into a canonical occupation. That brought the named-role share up to a usable 39%, with the rest correctly identified as adjacent or off-topic. The 61% "not a core data role" rate is itself a finding about how noisy keyword searches are in this market.

## What this dataset can and can't say

A few caveats so you can read the findings honestly:

- **Source bias.** Bundesagentur data only. Tech startups and international companies that advertise mostly on LinkedIn in English are under-represented. The 17.6% English share in this data is almost certainly a floor, not a ceiling.
- **47% of postings don't state a remote policy.** Remote percentages are computed against the subset that does. Read them as "of postings that take a position, X% allow remote."
- **Bundesagentur classifies quereinstieg-friendly by employer self-report.** Some employers who'd happily hire a career changer don't tick the box. 1.4% is a floor, not a ceiling.
- **Snapshot only.** Data was collected on May 19, 2026. This is a frozen frame of the market on that day, not a trend.
- **82% of postings don't state minimum experience.** The Junior-experience finding rests on a small explicit sample. The directional story is consistent with national patterns but the absolute number should be treated as suggestive rather than authoritative.

## Tech stack

- **Python 3.12**, dependency management via `uv`
- **DuckDB** for local analytical storage and queries
- **OpenRouter - Gemini 2.0 Flash** for LLM extraction
- **Pydantic** for schema validation, **Tenacity** for retry
- **Pandas, NumPy, SciPy** for analysis, **Matplotlib + Seaborn** for charts
- **Jupyter** for the three analysis notebooks

## Reproducing this analysis

The repository is set up for inspection rather than turnkey reproduction. The DuckDB file containing all extracted data isn't committed (too large), and the absolute paths inside the notebooks point at my local setup. If you want to rerun:

```bash
# Install dependencies
uv sync

# Add your OpenRouter API key
echo "OPENROUTER_API_KEY=sk-or-..." > .env

# Update the DB path in extractor.py and the notebooks to your local path

# Run the full pipeline (~3-4 hours of API calls + ~€1 of credit)
uv run src/collector.py
uv run src/cleaner.py
uv run src/extractor.py

# Then open the notebooks
uv run jupyter lab
```

## What I'd build next

A few extensions that would strengthen this if I extend it:

- **Re-collect weekly and track time-series.** Which roles disappear fastest? Which sit posted longest? That would turn a snapshot into a market dynamics story.
- **Add LinkedIn as a second source.** The Bundesagentur source bias is real. Comparing the two would let me quantify how much of the "English-friendly German tech market" actually lives outside the official agency.
- **Topic modelling on JD text.** BERTopic over 2,300 descriptions would surface latent clusters that cut across nominal role titles. I expect a "GenAI/LLM-focused" cluster that spans Data Scientist, ML Engineer, and AI Engineer titles.
- **Salary modelling.** Salary is mentioned in only a fraction of postings. Modelling the conditional distribution against role, seniority, and city would produce useful estimates for the postings that stay silent.

## Contact

If you found this useful, want to discuss anything in the analysis, or are hiring data folks in Germany — I'm Sanan Mammadov, based in Düsseldorf and looking for data scientist or data analyst roles.
