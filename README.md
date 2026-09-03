# Dice Auto Apply + LinkedIn Recruiter Outreach

Two automation tools that work independently or together:

- **Dice Apply** — automatically applies to Easy Apply jobs on Dice.com
- **LinkedIn Outreach** — searches LinkedIn for hiring posts, extracts recruiter emails, and sends cold outreach emails with your resume attached

---

## Prerequisites

- Python 3.10+
- Google Chrome installed (used by both tools)
- A Dice.com account with resume uploaded (for Dice Apply)
- A Gmail account with OAuth credentials (for email sending)
- [Ollama](https://ollama.com) running locally with `gemma2:2b` pulled

---

## Installation

```bash
pip install -r requirements.txt
playwright install chromium
```

Pull the Ollama model:

```bash
ollama pull gemma2:2b
```

---

## Configuration Files

| File | Purpose | Gitignored |
|------|---------|-----------|
| `.env` | Dice credentials, search settings | ✓ |
| `profiles.json` | Sender profiles (name, email, skills, work auth) | ✓ |
| `resumes.json` | Maps email → resume folder + default resume | ✓ |
| `gmail_credentials.json` | Gmail OAuth app credentials from Google Cloud | ✓ |
| `linkedin_config.json` | LinkedIn search keywords per profile | ✓ |

---

## Part 1 — Dice Auto Apply

### Setup

**1. Create `.env`:**

```bash
cp .env.example .env
```

Edit `.env`:

```env
DICE_EMAIL=your@email.com
DICE_PASSWORD=yourpassword
SEARCH_QUERY=gen ai
POSTED_DATE=ONE
EASY_APPLY=true
SENDER_NAME=Your Full Name
```

`POSTED_DATE` options: `ONE` (today) · `THREE` · `SEVEN` · `THIRTY` · `` (any time)

**2. Set up sender profiles** (`profiles.json`):

```json
{
  "your@gmail.com": {
    "name": "Your Name",
    "email": "your@gmail.com",
    "phone": "512-000-0000",
    "location": "Austin, TX",
    "work_auth": "OPT",
    "years_experience": 4,
    "skills": "Python, Gen AI, LangChain, AWS"
  }
}
```

**3. Set up resumes** (`resumes.json`):

```json
{
  "your@gmail.com": {
    "resume_folder": "/path/to/your/resumes",
    "default_resume": "/path/to/your/resumes/YourResume.pdf"
  }
}
```

**4. Gmail OAuth** — place `gmail_credentials.json` (downloaded from Google Cloud Console) in the project folder. Auth runs automatically on first email send.

### Run

```bash
python main.py
```

The browser opens visibly. The script logs in, searches Dice, and applies to each job. Results are saved to `applied_jobs.csv`.

### Console Output

```
Applying to: Senior Gen AI Engineer @ Acme Corp...
  [1/15] ✅ Applied: Senior Gen AI Engineer
  [2/15] ⏭️  Skipped: AI Prompt Engineer — already applied
  [3/15] ⏭️  Skipped: ML Engineer — external - skipped
```

### Multi-profile Dice Apply

To run Dice apply for a different profile, set `DICE_EMAIL` / `DICE_PASSWORD` in `.env` for that profile and run `python main.py` again.

---

## Part 2 — LinkedIn Recruiter Outreach

Searches LinkedIn posts for recruiter job postings matching your keywords (OPT, W2, C2C, etc.), extracts email addresses from posts, and sends personalised cold outreach emails with your resume.

### One-time Setup (per profile)

**Step 1 — Configure search keywords:**

```bash
# Configure all profiles at once (recommended)
python linkedin_outreach.py --setup-all

# Or configure one profile
python linkedin_outreach.py --setup --profile your@gmail.com
```

You'll be asked for:
- Search keywords (e.g. `python gen ai llm aws`)
- Job types to filter (e.g. `OPT W2 C2C`)
- Date range (`past-week` / `past-month` / any time)
- Max posts to scan and emails to send per run

**Step 2 — Log in to LinkedIn:**

```bash
python linkedin_outreach.py --login --profile your@gmail.com
```

A browser window opens — log in to LinkedIn, then press Enter in the terminal. Your session is saved so you never need to log in again (until the session expires).

Repeat Step 2 for each profile.

### Running Outreach

```bash
# Run one profile
python linkedin_outreach.py --profile yagneshreddypasunooru@gmail.com

# Run all profiles one after another
python linkedin_outreach.py --all

# Run all profiles simultaneously (parallel)
python linkedin_outreach.py --parallel
```

### Console Output (parallel example)

```
[yagneshred] ✓ LinkedIn session ready
[santoshpas] ✓ LinkedIn session ready
[yagneshred] [post 1] Gen AI Engineer @ Acme — recruiter@acme.com
[yagneshred] ✓ Email sent → recruiter@acme.com  (resume: YagneshResume.docx)
[santoshpas] [post 2] Sr. Python Developer @ TechCorp — hr@techcorp.com
[santoshpas] ✓ Email sent → hr@techcorp.com  (resume: SantoshResume.pdf)
[yagneshred] Done.  Posts scanned: 60  |  Emails sent: 18
[santoshpas] Done.  Posts scanned: 54  |  Emails sent: 22
```

### Check Status

```bash
python linkedin_outreach.py --list
```

Shows each profile's LinkedIn session status, Gmail token status, and configured keywords.

### All LinkedIn Commands

| Command | What it does |
|---------|-------------|
| `--setup-all` | Configure keywords for all profiles (interactive wizard) |
| `--setup --profile email` | Configure one profile |
| `--login --profile email` | Save LinkedIn session for a profile |
| `--profile email` | Run outreach for one profile |
| `--all` | Run all profiles sequentially |
| `--parallel` | Run all profiles at the same time |
| `--list` | Show status of all configured profiles |

---

## Data Files

| File | What's tracked |
|------|---------------|
| `applied_jobs.csv` | Every Dice application attempt with status |
| `~/.dice-playwright-profile-{email}/sent_emails.csv` | Every LinkedIn email sent per profile |

Both files prevent duplicates — jobs and recruiters already contacted are automatically skipped on subsequent runs.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Dice login fails | Check `.env` credentials; solve any CAPTCHA manually in the browser |
| LinkedIn session expired | Re-run `--login --profile email` |
| Gmail sends from wrong account | Delete `~/.dice-playwright-profile-{email}/gmail_token.json` and re-run; select the correct Google account in the OAuth popup |
| No LinkedIn posts found | The search returned no results — try broader keywords in `--setup` |
| Ollama errors | Make sure `ollama serve` is running and `gemma2:2b` is pulled |
| Script hangs | Press Ctrl+C — all results saved so far are in the CSV |
