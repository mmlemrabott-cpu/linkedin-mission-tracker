# LinkedIn Mission Tracker

Daily automation that scrapes LinkedIn for freelance mission posts, scores them against your consultant profile using Claude AI, and writes results to Google Sheets.

Runs automatically at **07:00 UTC** (09:00 CEST, lun–sam) and **11:30 UTC** (13:30 CEST, lun–ven) via GitHub Actions.

---

## How it works

```
1. Read config from Paramètres tab (your profile, countries, keywords)
2. Scrape LinkedIn posts via Apify (supreme_coder/linkedin-post actor) — all keywords batched in one run
3. Score each post with Claude Haiku against your profile vector (0–100)
4. Write scored results to Missions tab — deduplicated across runs
```

Posts below your score threshold are discarded. Everything else lands in your sheet with match score, extracted skills, TJM, location, and top 3 match reasons.

---

## Setup (~15 minutes)

### Step 1 — Copy the Google Sheets template

**[Open the template →](https://docs.google.com/spreadsheets/d/1MXV1UpnUYTLWr7nzHMt1yYZhXD0-h1bQD3fxI3MHWx0/edit?usp=sharing)**

1. Click **File → Make a copy** — this creates your own editable version in your Google Drive
2. From the URL of your copy, copy the spreadsheet **ID**:
   ```
   https://docs.google.com/spreadsheets/d/  <<<THIS_PART>>>  /edit
   ```
   Save it — you'll paste it as the `SPREADSHEET_ID` secret in Step 5.

The template has all required tabs. Leave everything except `Paramètres` empty — the pipeline fills them on first run.

---

### Step 2 — Fill in the Paramètres tab

The `Paramètres` tab is a **3-column table** (columns A, B, C). Each row is a config entry:

| Column A — `parametre` | Column B — `valeur_1` | Column C — `valeur_2` |
|------------------------|----------------------|----------------------|
| Key name | First value | Second value (profiles only) |

**Profile rows** — one row per LinkedIn profile (max 3):

| A | B | C |
|---|---|---|
| `profil` | `John Doe` ← your display name | `https://linkedin.com/in/john-doe/` ← full public URL |

> The URL must be a **public** LinkedIn profile. Test by opening it in a private browser window without being logged in.

**Country rows** — one row per target country:

| A | B | C |
|---|---|---|
| `pays` | `France` | *(leave empty)* |
| `pays` | `Belgium` | *(leave empty)* |

**Keyword rows** — one row per search query (use LinkedIn boolean syntax):

| A | B | C |
|---|---|---|
| `keyword` | `"mission" AND "freelance" AND "PMO"` | *(leave empty)* |
| `keyword` | `"besoin freelance" AND "chef de projet"` | *(leave empty)* |
| `remote_keyword` | `"full remote" AND "freelance" AND "PMO"` | *(leave empty)* |

**Threshold rows** — one row each:

| A | B | C |
|---|---|---|
| `score_minimum` | `50` | *(leave empty)* |
| `posts_max_par_pays` | `50` | *(leave empty)* |

> **Keyword tips**:
> - Use `AND` to require all terms, `OR` for alternatives, quotes for exact phrases
> - Start with `score_minimum = 40` to see volume, raise to 60+ once calibrated
> - You can edit this tab at any time — changes apply on the next run, no code change needed

---

### Step 3 — Get your API tokens

You need tokens from **2 services** (Apify and Anthropic):

#### Apify API token

Apify scrapes LinkedIn posts using the `supreme_coder/linkedin-post` actor. No cookies or LinkedIn account required.

1. Go to **[console.apify.com](https://console.apify.com/)** and create an account
2. Go to **Settings → Integrations → API tokens**
3. Click **Create new token** → name it (e.g. `linkedin-tracker`) → **Create**
4. Copy the token — it starts with `apify_api_...`
5. Save it as `APIFY_API_TOKEN`

Cost: ~$1 per 1,000 posts scraped. A free trial credit is included on signup.

#### Anthropic API key

Claude Haiku scores each post against your profile. Extremely cheap (~$1–5/month).

1. Go to **[console.anthropic.com](https://console.anthropic.com)** and create an account
2. Go to **Billing** and add a payment method (or redeem a credit)
3. Go to **API Keys → Create Key**
4. Give it a name (e.g. `linkedin-tracker`), click **Create**
5. Copy the key immediately — it's only shown once. It starts with `sk-ant-...`
6. Save it as `ANTHROPIC_API_KEY`

---

### Step 4 — Google Service Account (Sheets access)

The pipeline reads and writes your Google Sheet using a service account — a bot identity you create in Google Cloud. No credit card required; Google Sheets API is always free.

**4.1 — Create a Google Cloud project**

1. Go to **[console.cloud.google.com](https://console.cloud.google.com)**
2. Click the project selector at the top → **New Project**
3. Name it anything (e.g. `linkedin-tracker`) → **Create**

**4.2 — Enable the Google Sheets API**

1. In the left menu: **APIs & Services → Library**
2. Search for `Google Sheets API`
3. Click it → **Enable**

**4.3 — Create a Service Account**

1. In the left menu: **IAM & Admin → Service Accounts**
2. Click **+ Create Service Account**
3. Name: `sheets-writer` (or anything) → **Create and Continue**
4. Skip the optional role/access steps → **Done**

**4.4 — Generate a JSON key**

1. Click the service account you just created
2. Go to the **Keys** tab → **Add Key → Create new key**
3. Select **JSON** → **Create**
4. A `.json` file downloads automatically — keep it safe

**4.5 — Convert the JSON key to a single line**

GitHub secrets cannot contain newlines. Run this command (replace `service_account.json` with your file path):

```bash
python -c "import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps(d))" service_account.json
```

Copy the full output — it's one line starting with `{"type":"service_account","project_id":...}`.
This is your `GOOGLE_SERVICE_ACCOUNT_JSON` secret value.

**4.6 — Share your Google Sheet with the service account**

1. Open your JSON key file and find the `client_email` field — it looks like:
   ```
   sheets-writer@your-project-123456.iam.gserviceaccount.com
   ```
2. Open your Google Sheet copy from Step 1
3. Click **Share** (top right) → paste the `client_email` → set role to **Editor** → **Send**

---

### Step 5 — Fork the repo and add GitHub secrets

**5.1 — Fork the repository**

1. Click **Fork** at the top of this page
2. Select your GitHub account as the destination

**5.2 — Create a "production" environment**

The workflows reference a GitHub environment called `production`. You need to create it:

1. In your fork: **Settings → Environments → New environment**
2. Name it exactly `production` → **Configure environment**
3. Leave protection rules empty (no required reviewers) → **Save protection rules**

**5.3 — Add secrets to the environment**

Still in **Settings → Environments → production**:

Scroll to **Environment secrets** → **Add secret** — add all 4:

| Secret name | Where to get it | Example format |
|-------------|----------------|---------------|
| `APIFY_API_TOKEN` | Step 3 — Apify console | `apify_api_...` |
| `ANTHROPIC_API_KEY` | Step 3 — Anthropic console | `sk-ant-api03-...` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Step 4.5 — one-line JSON output | `{"type":"service_account",...}` |
| `SPREADSHEET_ID` | Step 1 — from your sheet URL | `1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms` |

> **Important**: Secrets go under **Environments → production → Environment secrets**, NOT under the top-level "Actions secrets". The workflows use `environment: production`, so they read from the environment's secret store.

---

### Step 6 — Trigger your first run

There are **two workflows** — trigger them both manually for the first run:

**Workflow 1 — Freelance missions → Missions tab**

1. In your fork, click the **Actions** tab (top navigation bar)
2. In the left sidebar, click **Daily LinkedIn Freelance Mission Extract**
3. On the right side of the page, click **Run workflow** → **Run workflow** (green button)
4. Wait **15–20 minutes** → open your Google Sheet → check the **Missions_YYYY-MM** tab

> Apify runs all keyword URLs in a single actor call and handles LinkedIn rate-limiting internally. Typical pipeline time is **5–10 minutes** for a standard config.

**Workflow 2 — Remote jobs → Remote tab**

1. Still in **Actions**, click **Daily LinkedIn Remote Job Extract** in the left sidebar
2. Click **Run workflow** → **Run workflow** (green button)
3. Wait **15–20 minutes** → check the **Remote_YYYY-MM** tab in your sheet

> After the first run, all four workflows run automatically (07:00, 07:30, 11:30, 12:00 UTC). You only need to trigger them manually once.

> If a run fails: click the failed run → scroll down → download the **run-logs** artifact → open the `.log` file for the exact error message.

---

## What happens on first run

```
Paramètres tab read         → your config loaded (profile, countries, keywords)
Profils_Cache checked       → empty → cache miss
BeReach API called          → your LinkedIn profile fetched and stored in cache
Apify actor triggered       → all keyword URLs batched in one run (past-24h filter)
Posts scored (Claude)       → each post matched against your profile (0–100)
Results written             → Missions tab populated with scored posts
Dedup_Index updated         → prevents duplicates on all future runs
docs/usage.json updated     → dashboard stats committed to repo
```

## Subsequent runs (matin 07:00 UTC = 09:00 CEST — après-midi 11:30 UTC = 13:30 CEST, lun–ven)

```
Profils_Cache hit           → no profile API call → fast startup
Dedup_Index checked         → only new posts processed
New results appended        → Missions tab grows daily
```

---

## Output columns (Missions tab)

| Column | Content |
|--------|---------|
| A — date | Post date (YYYY-MM-DD) |
| B — heure | Post time (HH:MM UTC) |
| C — author | Post author name |
| D — mission_title | Extracted mission title |
| E — required_skills | Comma-separated skills |
| F — match_score | 0–100 (green ≥80, yellow 50–79, red <50) |
| G — tjm | Daily rate if mentioned |
| H — post_url | Link to the original LinkedIn post |
| I — pays | Country |
| J — ville | City |
| K — profil | Which of your profiles matched best |
| L — match_reasons | Top 3 reasons from Claude |
| M — feedback | Fill this manually to calibrate future scoring |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Placeholder values detected` | Paramètres tab not filled | Complete Step 2 — especially the `profil` row |
| `KeyError: BEREACH_API_TOKEN` | Secret missing or in wrong location | Check secrets are under **Environments → production**, not top-level Actions secrets |
| `APIError: 403` on Sheets | Sheet not shared with service account | Share the sheet with the `client_email` from your JSON key (Step 4.6) |
| `json.JSONDecodeError` on startup | Newlines in `GOOGLE_SERVICE_ACCOUNT_JSON` | Re-run the Python one-liner from Step 4.5 — result must be one line |
| 0 posts returned | Keywords too narrow | Check `logs/run_YYYY-MM-DD.log` artifact; try broader keywords |
| Score always 0 | Profile URL not reachable | Verify `profil` URL in Paramètres opens without login in a private browser |
| Workflow not visible in Actions | GitHub indexing delay | Make a small change to any workflow file and push |

---

## Cost estimate

| Service | Free tier | Typical paid |
|---------|-----------|------|
| Apify | Free trial credit included | ~$1/1,000 posts scraped |
| Anthropic Claude Haiku | $5 free credit | ~$1–5/month |
| Google Sheets API | Always free | Free |
| GitHub Actions | 2,000 min/month free | Free for this use case |

---

## Architecture

```
run.py (orchestrator)
├── config/config.py           — load & validate AppConfig from env vars
├── sheets/sheets_writer.py    — read Paramètres tab → override config
│                              — load Profils_Cache + Dedup_Index
├── scraper/apify_scraper.py   — Apify actor: all keywords batched → RawPost list + stats
├── matcher/profile_matcher.py — BeReach API: fetch profile → Claude Haiku scoring
├── sheets/sheets_writer.py    — write enriched posts → update Dedup_Index
└── sheets/usage_stats.py      — append run metrics to docs/usage.json
```

Config priority: **Paramètres tab** (runtime, editable) → `config/settings.json` (bootstrap defaults)

Two pipelines:
- `daily_extract.yml` — 07:00 UTC (09:00 CEST, lun–sam) — scrapes freelance missions → `Missions_YYYY-MM` tab
- `daily_remote.yml` — 07:30 UTC (09:30 CEST, lun–sam) — scrapes remote jobs → `Remote_YYYY-MM` tab
- `afternoon_extract.yml` — 11:30 UTC (13:30 CEST, lun–ven) — 2nd daily freelance pass → `Missions_YYYY-MM` tab
- `afternoon_remote.yml` — 12:00 UTC (14:00 CEST, lun–ven) — 2nd daily remote pass → `Remote_YYYY-MM` tab

---

## Dashboard (GitHub Pages)

Each pipeline run writes metrics to `docs/usage.json` and the workflow commits it back to the repo. GitHub Pages serves a live dashboard from that file.

**Activate once** (after forking): **Settings → Pages → Source: Deploy from a branch → Branch: master, folder: /docs → Save**

Dashboard URL: `https://{your-github-username}.github.io/linkedin-freelance-mission-tracker/`

Metrics tracked: posts scraped, missions retained, Apify cost (USD), run duration — filterable by mode (Freelance / Job).

---

## Customizing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to adapt keywords, scoring logic, and add new countries.
