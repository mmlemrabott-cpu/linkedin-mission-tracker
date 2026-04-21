\# CLAUDE.md — LinkedIn Freelance Mission Tracker



\## Project Purpose

Daily automation that discovers freelance mission posts on LinkedIn for specified countries,

scores them against a consultant profile using Claude AI, and writes structured data to Google Sheets.



\## Architecture

\- Entry point: `run.py` (invoked daily via GitHub Actions)

\- Modules: `scraper/`, `matcher/`, `sheets/`, `config/`

\- Config: `.env` for secrets, `config/settings.json` for placeholder defaults only

\- Runtime config: Google Sheets `Paramètres` tab overrides `settings.json` at startup

\- CI/CD: `.github/workflows/daily_extract.yml` — runs every day at 10:30 UTC (= 12:30 CEST / 11:30 CET)

\- Second pipeline: `.github/workflows/daily_remote.yml` — remote jobs, 11:00 UTC (= 13:00 CEST / 12:00 CET)



\## Config Flow

1\. `config/settings.json` — placeholder template committed to git (no personal data)

2\. `sync_config_tab()` — reads `Paramètres` tab from Google Sheet and overrides all settings

3\. Placeholder guard in `run.py` — raises `EnvironmentError` if `LINKEDIN_PROFILES` still contains `YOUR_` values after step 2



\## Coding Rules

\- Never hardcode credentials. Always use `os.getenv()`.

\- Handle BeReach rate limits: BeReach enforces ~**2 requests per 5-minute sliding window**. Run keyword queries in **batches of 2** with a **310s pause between batches** and a **3–6s intra-batch delay** between the two queries. Never run queries in parallel — constants `_RATE_LIMIT_BATCH_SIZE`, `_RATE_LIMIT_BATCH_PAUSE`, and `_RATE_LIMIT_INTRA_BATCH_DELAY` in `bereach_scraper.py` control this behaviour.

\- All functions must have docstrings and return typed values.

\- Log all steps to `logs/run\_{date}.log`.

\- On error, write a failed row to the sheet with status="ERROR" rather than crashing.

\- Deduplicate posts using post URL as unique key before writing to sheet.

\- Only process posts published within the last 24 hours. Discard older posts at scrape time.



\## Key Files

\- `config/settings.json`: placeholder template — fields: `LINKEDIN_PROFILES`, `TARGET_COUNTRIES`, `SEARCH_KEYWORDS`, `REMOTE_KEYWORDS`, `MIN_MATCH_SCORE`, `MAX_POSTS_PER_COUNTRY`

\- `~.claude/skills/linkedin-scraper/SKILL.md`: scraping logic instructions (BeReach API)

\- `~.claude/skills/profile-matcher/SKILL.md`: scoring logic instructions (BeReach profile fetch + Claude Haiku)

\- `~.claude/skills/sheets-writer/SKILL.md`: Google Sheets write instructions

\- `QUICKSTART.md`: new user setup guide (Google Sheets template → fill Paramètres → set secrets → run)

\- `CONTRIBUTING.md`: fork and customization guide



\## GitHub Actions Setup

\- Workflow file: `.github/workflows/daily_extract.yml` (freelance missions, 10:30 UTC = 12:30 CEST)

\- Workflow file: `.github/workflows/daily_remote.yml` (remote jobs, 11:00 UTC = 13:00 CEST)

\- Schedule: `cron: '30 10 * * *'` (10:30 UTC = 12:30 CEST in summer, 11:30 CET in winter)

\- Trigger: also supports `workflow_dispatch` for manual runs

\- All secrets must be set in the GitHub repo Settings → Secrets and variables → Actions

\- Workflow steps: checkout → install Python deps → run `python run.py` → upload logs as artifact



\## Environment Variables Required

ANTHROPIC\_API\_KEY, BEREACH\_API\_TOKEN,

GOOGLE\_SERVICE\_ACCOUNT\_JSON, SPREADSHEET\_ID



\## APIs Used

\- **BeReach** (`https://api.berea.ch`): post scraping (`/search/linkedin/posts`) + profile fetching (`/visit/linkedin/profile`)

\- **Anthropic Claude Haiku**: post scoring and mission extraction

\- **Google Sheets API**: config sync, results storage, dedup index, profile cache


