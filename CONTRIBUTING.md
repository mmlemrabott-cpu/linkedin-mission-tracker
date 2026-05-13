# Contributing & Customization Guide

This guide explains how to fork, personalize, and extend the LinkedIn Freelance Mission Tracker.

---

## Forking & Personalizing

### 1. Set your profile

Edit `config/settings.json`:

```json
{
  "LINKEDIN_PROFILES": [
    {"name": "YOUR_NAME", "url": "https://www.linkedin.com/in/YOUR_LINKEDIN_PROFILE/"}
  ]
}
```

The pipeline fetches your LinkedIn profile via the BeReach API to build a skills vector used for scoring (BeReach is used only for profile fetching — post scraping uses Apify). Make sure your profile URL is public.

### 2. Set your target countries

```json
{
  "TARGET_COUNTRIES": ["France", "Germany", "Belgium"]
}
```

Any country name that LinkedIn recognizes as a location filter works here.

### 3. Adjust the scoring threshold

```json
{
  "MIN_MATCH_SCORE": 65
}
```

Range: 0–100. Posts below this score are discarded. Start around 40–50 and raise it once you see the quality of results.

### 4. Limit posts per country

```json
{
  "MAX_POSTS_PER_COUNTRY": 50
}
```

Reduce to lower API costs; increase if you want broader coverage.

---

## Adding or Changing Search Keywords

Keywords use LinkedIn's boolean search syntax. Edit the `SEARCH_KEYWORDS` array in `config/settings.json`.

Examples:

```json
"SEARCH_KEYWORDS": [
  "(\"mission\" OR \"besoin\") AND (\"freelance\" OR \"tjm\") AND (\"PMO\" OR \"chef de projet\")",
  "(\"freelance\") AND (\"data engineer\" OR \"data analyst\")"
]
```

Tips:
- Use `AND` to require all terms, `OR` to accept any
- Quote multi-word phrases with `\"`
- Keep each query focused — broad queries return noise
- The same syntax applies to `REMOTE_KEYWORDS` for the remote jobs pipeline

---

## Adding a New Target Country

Just add the country name to `TARGET_COUNTRIES`. No code change required.

```json
"TARGET_COUNTRIES": ["France", "Germany", "Netherlands"]
```

---

## Extending the Scoring Logic

The scoring engine is in `matcher/profile_matcher.py` (uses BeReach API to fetch profile vectors, then Claude Haiku to score each post). The main entry point is:

```python
def score_post(post: RawPost, profile: LinkedInProfile, feedback: list[dict]) -> EnrichedPost:
```

It sends a prompt to Claude Haiku with the post content and your profile, and returns a score (0–100) plus an explanation.

To customize scoring criteria:
- Edit the Claude prompt in `profile_matcher.py` (search for `SCORING_PROMPT`)
- Add new fields to `EnrichedPost` in the same file if you need more output columns
- Update `sheets/sheets_writer.py` to write any new columns to the sheet

---

## Running Locally for Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in secrets
cp .env.example .env

# Run the full pipeline
python run.py

# Check the log
cat logs/run_$(date +%Y-%m-%d).log
```

To test a specific mode:

```bash
RUN_MODE=freelance python run.py   # freelance missions pipeline
RUN_MODE=job python run.py         # remote jobs pipeline
```

---

## Pull Request Guidelines

If you want to contribute improvements back:

1. **Never commit credentials** — `.env` is gitignored; keep it that way
2. **Update `requirements.txt`** if you add a new dependency (pin the version exactly)
3. **Add docstrings** to any new function you write
4. **Test locally** before opening a PR — run `python run.py` and verify the log shows no errors
5. **Keep PRs focused** — one feature or fix per PR

---

## Project Structure Reference

```
run.py                    — orchestrator (entry point)
config/
  config.py               — loads and validates AppConfig from env + settings.json
  settings.json           — user preferences (profile, countries, keywords, thresholds)
scraper/
  apify_scraper.py        — primary scraper (Apify supreme_coder/linkedin-post actor)
  bereach_scraper.py      — backup scraper (BeReach API, kept for reference)
  linkedin_scraper.py     — shared utilities (RawPost type, dedup helpers, 24h filter)
matcher/
  profile_matcher.py      — Claude Haiku scoring engine + BeReach profile fetcher
sheets/
  sheets_writer.py        — Google Sheets read/write, dedup, cache
  usage_stats.py          — appends run metrics to docs/usage.json for the dashboard
docs/
  index.html              — GitHub Pages dashboard (Chart.js, reads usage.json)
  usage.json              — run history (committed by each GitHub Actions run)
.github/workflows/
  daily_extract.yml       — freelance missions pipeline (07:00 UTC, lun–sam = 09:00 CEST)
  daily_remote.yml        — remote jobs pipeline (07:30 UTC, lun–sam = 09:30 CEST)
  afternoon_extract.yml   — freelance missions pipeline (11:30 UTC, lun–ven = 13:30 CEST)
  afternoon_remote.yml    — remote jobs pipeline (12:00 UTC, lun–ven = 14:00 CEST)
```
