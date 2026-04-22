# Quick Start — New User Setup (~15 minutes)

---

## Step 1 — Copy the Google Sheets template

1. Open the template: **[Google Sheets Template →](https://docs.google.com/spreadsheets/d/e/2PACX-1vS4MfF5gNZ0lBo2PbGi2pvntAXCfv2mc11JpB5OsYUK7kjWiVSfH3JSU2BoYxx4Q-gYz7TwG23QoxGf/pubhtml)**
2. Click **File → Make a copy** — this saves a copy to your Google Drive
3. From the URL of your copy, copy the spreadsheet **ID**:
   ```
   https://docs.google.com/spreadsheets/d/  <<<THIS_PART>>>  /edit
   ```
   Save it — you'll use it as the `SPREADSHEET_ID` secret in Step 5.

Leave all tabs empty except `Paramètres`. The pipeline fills everything else on first run.

---

## Step 2 — Fill in the Paramètres tab

The `Paramètres` tab is a **3-column table**. Each row is one config entry:

| Column A | Column B | Column C |
|----------|----------|----------|
| Key name | First value | Second value (profiles only) |

### Your LinkedIn profile (one row per profile, max 3)

| A | B | C |
|---|---|---|
| `profil` | `John Doe` ← your display name | `https://linkedin.com/in/john-doe/` ← full URL |

> The URL must be a **public** LinkedIn profile — test by opening it in a private browser window without being logged in.

### Target countries (one row per country)

| A | B | C |
|---|---|---|
| `pays` | `France` | *(leave empty)* |
| `pays` | `Belgium` | *(leave empty)* |

Add as many `pays` rows as you want.

### Search keywords — freelance missions (one row per query)

| A | B | C |
|---|---|---|
| `keyword` | `"mission" AND "freelance" AND "PMO"` | *(leave empty)* |
| `keyword` | `"besoin freelance" AND "chef de projet"` | *(leave empty)* |

Use LinkedIn boolean syntax: `AND`, `OR`, quoted phrases for exact matches.

### Search keywords — remote jobs (one row per query)

| A | B | C |
|---|---|---|
| `remote_keyword` | `"full remote" AND "freelance" AND "PMO"` | *(leave empty)* |

### Thresholds

| A | B | C |
|---|---|---|
| `score_minimum` | `50` | *(leave empty)* |
| `posts_max_par_pays` | `50` | *(leave empty)* |

> **Tips**:
> - Start with `score_minimum = 40` to see volume, raise once you calibrate
> - You can edit this tab anytime — no code change needed, takes effect on next run
> - Comment rows (starting with `#`) are ignored by the pipeline

---

## Step 3 — Get your API tokens

### Apify API token

Apify scrapes LinkedIn posts using the `supreme_coder/linkedin-post` actor. No LinkedIn account or cookies required.

1. Go to **[console.apify.com](https://console.apify.com/)** → create an account
2. Go to **Settings → Integrations → API tokens**
3. Click **Create new token** → name it (e.g. `linkedin-tracker`) → **Create**
4. Copy your token — it starts with `apify_api_...`
5. Save as `APIFY_API_TOKEN`

Cost: ~$1 per 1,000 posts. A free trial credit is included on signup.

### Anthropic API key

Claude Haiku scores posts against your profile. Cost: ~$1–5/month.

1. Go to **[console.anthropic.com](https://console.anthropic.com)** → create an account
2. **Billing** → add payment method (or redeem a credit)
3. **API Keys → Create Key** → name it (e.g. `linkedin-tracker`) → **Create**
4. Copy the key immediately — shown only once. Starts with `sk-ant-...`
5. Save as `ANTHROPIC_API_KEY`

---

## Step 4 — Google Service Account (Sheets access)

No credit card needed — Google Sheets API is always free.

### 4.1 — Create a Google Cloud project

1. Go to **[console.cloud.google.com](https://console.cloud.google.com)**
2. Click the project dropdown at top → **New Project** → name it (e.g. `linkedin-tracker`) → **Create**

### 4.2 — Enable the Google Sheets API

1. Left menu: **APIs & Services → Library**
2. Search `Google Sheets API` → click it → **Enable**

### 4.3 — Create a Service Account

1. Left menu: **IAM & Admin → Service Accounts**
2. **+ Create Service Account** → name: `sheets-writer` → **Create and Continue**
3. Skip optional steps → **Done**

### 4.4 — Generate a JSON key

1. Click the service account → **Keys** tab → **Add Key → Create new key → JSON → Create**
2. A `.json` file downloads — keep it safe

### 4.5 — Convert the JSON to a single line

GitHub secrets cannot contain newlines. Run:

```bash
python -c "import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps(d))" service_account.json
```

Copy the output (one line, starts with `{"type":"service_account",...}`).
This is your `GOOGLE_SERVICE_ACCOUNT_JSON` value.

### 4.6 — Share your Google Sheet with the service account

1. Open your JSON file → find the `client_email` field:
   ```
   sheets-writer@your-project-123456.iam.gserviceaccount.com
   ```
2. Open your Google Sheet → **Share** → paste the email → role: **Editor** → **Send**

---

## Step 5 — Fork the repo and add GitHub secrets

### 5.1 — Fork the repository

Click **Fork** at the top of the repo page → select your GitHub account.

### 5.2 — Create a "production" environment

The workflows use `environment: production`. You must create it first:

1. In your fork: **Settings → Environments → New environment**
2. Name it exactly `production` → **Configure environment**
3. Leave protection rules empty → **Save protection rules**

### 5.3 — Add secrets to the environment

In **Settings → Environments → production → Environment secrets → Add secret**:

| Secret name | Value |
|-------------|-------|
| `APIFY_API_TOKEN` | From Step 3 |
| `ANTHROPIC_API_KEY` | From Step 3 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Single-line JSON from Step 4.5 |
| `SPREADSHEET_ID` | Spreadsheet ID from Step 1 |

> **Important**: Add secrets under **Environments → production**, not under the top-level "Actions secrets". The workflows read from the environment secret store.

---

## Step 6 — Trigger your first run

There are **two workflows** to trigger manually on first run:

**Workflow 1 — Freelance missions**

1. In your fork, click the **Actions** tab (top navigation bar of your repo)
2. In the left sidebar, click **Daily LinkedIn Freelance Mission Extract**
3. On the right side, click **Run workflow** → **Run workflow** (green button)
4. Wait **15–20 minutes** → open your sheet → check the **Missions_YYYY-MM** tab

> Apify runs all keyword URLs in a single actor call and handles rate-limiting internally. Typical runtime is **5–10 minutes**.

**Workflow 2 — Remote jobs**

1. Still in **Actions**, click **Daily LinkedIn Remote Job Extract** in the left sidebar
2. Click **Run workflow** → **Run workflow** (green button)
3. Wait **15–20 minutes** → check the **Remote_YYYY-MM** tab in your sheet

> After the first run, both workflows run automatically every day (10:30 and 11:00 UTC). You only need to trigger them manually once.

> If a run fails: click the failed run → scroll down → download the **run-logs** artifact → open the `.log` file for the exact error.

---

## What happens on first run

```
Paramètres tab read         → your config loaded (profile, countries, keywords)
Profils_Cache checked       → empty → cache miss
BeReach called              → your LinkedIn profile fetched once and cached
Apify actor triggered       → all keyword URLs batched in one run (past-24h filter)
Posts scored (Claude)       → each post matched against your profile (0–100)
Results written             → Missions tab populated
Dedup_Index updated         → prevents duplicates on all future runs
docs/usage.json updated     → dashboard stats committed to repo
```

## Subsequent runs (daily, 10:30 UTC = 12:30 CEST)

```
Profils_Cache hit           → no profile API call → fast startup
Dedup_Index checked         → only new posts processed
New results appended        → Missions tab grows daily
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Placeholder values detected` | `profil` row not filled | Complete Step 2 — especially the `profil` row |
| `KeyError: APIFY_API_TOKEN` | Secret in wrong location | Secrets must be under **Environments → production**, not top-level Actions secrets |
| `APIError: 403` on Sheets | Sheet not shared | Share with the `client_email` from JSON key (Step 4.6) |
| `json.JSONDecodeError` | Newlines in JSON secret | Re-run the one-liner from Step 4.5 — output must be one line |
| 0 posts returned | Keywords too narrow or Apify run failed | Check log artifact; try broader keywords; verify `APIFY_API_TOKEN` is valid |
| Score always 0 | Profile URL not public | Open the profile URL in a private browser — must be accessible without login |

For more detail, see [README.md](README.md).
