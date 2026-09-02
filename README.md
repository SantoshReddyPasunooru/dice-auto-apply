# Dice.com Job Application Automation

Automatically applies to **Easy Apply** jobs on Dice.com matching **"gen ai"** posted today.

## Prerequisites

- Python 3.10+
- A Dice.com account with your resume already uploaded

---

## Setup

### 1. Install Python dependencies

```bash
cd dice_auto_apply
pip install -r requirements.txt
```

### 2. Install Playwright's Chromium browser

```bash
playwright install chromium
```

### 3. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and fill in your Dice.com credentials:

```
DICE_EMAIL=your@email.com
DICE_PASSWORD=yourpassword
```

> `.env` is gitignored — never commit it.

---

## Running

```bash
python main.py
```

The browser will open visibly so you can monitor progress. The script will:

1. Log in to Dice.com
2. Search for "gen ai" jobs with Easy Apply + Posted Today filters
3. Apply to each job sequentially across all pages
4. Log every result to `applied_jobs.csv`

### Console output

```
Applying to: Senior Gen AI Engineer @ Acme Corp...
  [1/15] ✅ Applied: Senior Gen AI Engineer
  [2/15] ⏭️  Skipped: AI Prompt Engineer — already applied
  [3/15] ⏭️  Skipped: ML Engineer — external - skipped
  [4/15] ❌ Error: LLM Researcher — error: submit failed

========================================
Summary:
  ✅ Applied : 12
  ⏭️  Skipped : 2
  ❌ Errors  : 1
========================================
```

---

## CSV Log (`applied_jobs.csv`)

| Column | Description |
|--------|-------------|
| `timestamp` | ISO 8601 datetime of the application attempt |
| `job_title` | Job title |
| `company` | Company name |
| `location` | Job location |
| `job_url` | Direct link to the job posting |
| `status` | `applied`, `skipped - already applied`, `external - skipped`, `error: <reason>` |

---

## Optional: Ollama for custom application questions

Some jobs include extra text fields (beyond resume/work-auth/location). If Ollama is installed, the script will use the **llama3** model to auto-answer them.

### Install Ollama

```bash
# macOS
brew install ollama

# Or download from https://ollama.com
```

### Pull the model

```bash
ollama pull llama3
```

### Enable in requirements.txt

Uncomment the `ollama` line in `requirements.txt`, then reinstall:

```bash
pip install ollama
```

The script detects `ollama` automatically — no other changes needed.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Login fails | Verify `.env` credentials; check for CAPTCHA in the browser window |
| No jobs found | Dice may have changed their HTML structure; open the browser and inspect selectors |
| Resume not pre-selected | Upload your resume to Dice.com profile first; the script expects it to already be there |
| External redirect | Normal — Dice shows some jobs that redirect to employer ATS; these are logged as `external - skipped` |
| Script hangs on a page | Press Ctrl+C — results so far are already saved to CSV |
