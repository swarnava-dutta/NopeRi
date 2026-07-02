# Noperi

A lightweight and Selenium-free Python API client for Naukri.com, designed to help you update your profile, upload your resume, search jobs, and apply to jobs (easy apply) programmatically.

---

**Status:** 🟢 Working (Last tested: June 2026)

---

## ✨ Features

| Feature | Status |
|---|---|
| Cookie login and session management | Working |
| Recommended jobs feed | Working |
| `nkparam` token generator | Working |
| Selenium `nkparam` harvester fallback | Working |
| Job search (`/jobapi/v3/search`) | Working |
| Job details (`/jobapi/v1/job/`) | Working |
| One-click job apply | Working |
| Basic questionnaire handling | Partial |
| OTP/MFA helpers | Working |

## Setup

```bash
pip install -r requirements.txt
```

Put logged-in Naukri browser cookies in root `cookies.json`.
The file can be a browser-export cookie list or simple name/value object.
It must include `nauk_at`.

## Quick Start

```python
from src.client.naukri_client import NaukriLoginClient
from src.client.job_client import NaukriJobClient

client = NaukriLoginClient()
client.login()

jc = NaukriJobClient(client)
jobs = jc.search_jobs(keyword="Node.js developer", location="Hyderabad", experience=2)

for job in jobs:
    result = jc.apply_job(job, source="search")
    print(job.title, result)
```

## Agent

```bash
python apply_agent.py
```

Edit agent settings in `src/config/agent_config.py`:

- search keywords, locations, experience, pages, and job age
- daily apply limit, mandatory skill split, delays, and payload defaults
- `RUN_RECOMMENDED_PHASE=False` to skip recommended jobs and go straight to search agents
- `RUN_SEARCH_PHASE=False` to run recommended jobs only
- `DOCUMENT_EXTERNAL_LINKS=True` to save company-site apply links to `external_jobs.csv`
- questionnaire answers such as CTC, experience, notice period, and skills

Plain Python agent flow:

1. Login using `cookies.json`
2. `NaukriApplyOrchestrator` starts the phase order
3. `RecommendedJobAgent` fetches recommended jobs, if enabled
4. `SearchTermApplyAgent` runs once per configured search term, if enabled
5. `EasyApplyAgent` applies Naukri easy-apply jobs
6. `ExternalLinkAgent` documents company-site apply links in `external_jobs.csv`
7. Applied job IDs are saved to `applied_jobs.csv`

When a job has questionnaire questions, the applied row stores a
`questionnaire_answers` JSON array with `question_id`, `question`, `answer`,
and `raw_answer`.

Runtime logs use readable status symbols:

- `✅ Applied`
- `❌ External link`
- `⏭️ Already applied`
- `⚠️ Failed`

## API

### `NaukriLoginClient`

| Method | Description |
|---|---|
| `login()` | Loads `cookies.json`, verifies session, refreshes cookies back into same file |
| `get_application_history()` | Fetches application history |

### `NaukriJobClient`

| Method | Description |
|---|---|
| `get_recommended_jobs()` | Returns a list of `Job` objects personalised to your profile |
| `search_jobs(keyword, location, page, experience, ...)` | Returns job results using the search endpoint |
| `apply_job(job)` | Applies to a job programmatically |

### `Job` model

```python
@dataclass
class Job:
    job_id:      str
    title:       str
    company:     str
    location:    str
    experience:  str
    salary:      str
    posted_date: str
    apply_link:  str
    description: str
    tags:        list[str]
```

---

## 🔑 The `nkparam` Problem (and Current Solution)

Naukri's job-search endpoint (`/jobapi/v3/search`) requires a request header called `nkparam`.  
This is not just a random token — it is essentially an **encrypted/signature key** generated using:
- current timestamp (time-based salt)
- session-related data
- page/context-specific parameters

The logic exists inside Naukri’s obfuscated JavaScript bundle, which makes it hard to reverse directly.

If `nkparam` is missing or invalid, the API returns `403 Forbidden`.

---

### ✅ Current Solution

We now generate `nkparam` directly via API logic (no browser required).


---

### 🧰 Fallback (Optional)

A Selenium-based harvester is still available as a backup:

**`nk_param_getter.py`**
- Opens Chrome
- Captures network requests
- Extracts valid `nkparam`
- Stores in `nkPool.txt`




---

## 🤖 Automated Job Application Agent

For a fully automated, AI-powered job application workflow, use:

```bash
python apply_agent.py
```

The agent is built on top of `NaukriLoginClient` and `NaukriJobClient`, so all authentication, session management, cookies, and token handling are already taken care of automatically.

---

### ✨ What the Agent Does

The workflow runs end-to-end automatically:

1. Logs into Naukri using your `.env` credentials
2. Searches jobs using curated backend-focused keywords
3. Scores each job using an AI model (`OpenAI`)
4. Automatically applies to jobs that pass the score threshold
5. Handles basic job questionnaires using predefined answers
6. Skips external company-site applications
7. Prevents duplicate applications using a local CSV log
8. Displays a colored terminal dashboard with live progress

---

### ⚙️ Prerequisites

Add your OpenAI API key to the `.env` file:

```env
OPEN_API_KEY=your-openai-api-key
```

Your `.env` should now look like:

```env
USERNAME=your_naukri_email@example.com
PASSWORD=your_naukri_password
OPEN_API_KEY=your-openai-api-key
```

---

### 🧠 Default Search Strategy

The agent fetches jobs using curated backend-development queries such as:

- Node.js Developer
- Python Backend Developer
- Backend Engineer
- API Developer
- Full Stack Developer

It also filters using:
- Experience level
- Job freshness
- Multiple result pages

---

### 🔧 Configuration

You can customize the behaviour directly inside `apply_agent.py`:

| Variable | Purpose |
|---|---|
| `BQUERIES` | List of job search keywords |
| `EXPERIENCE_LEVELS` | Experience filters |
| `PAGES` | Number of search-result pages |
| `JOB_AGE` | Max age of jobs to consider |
| `SCORE_THRESHOLD` | Minimum AI score required before applying |

---

### 📂 Application Tracking

The agent keeps track of already-applied jobs using:

```bash
applied_jobs.csv
```

This ensures:
- No duplicate applications
- Safe repeated runs
- Persistent local history

---

### 🖥️ Terminal Dashboard

The agent displays a live terminal dashboard showing:

- Login status
- Jobs fetched
- AI evaluation score
- Apply success/failure
- Questionnaire detection
- Final application summary

Example:

```text
[FETCH] Backend Engineer — Hyderabad
[AI SCORE] 8.7/10
[APPLY] Success
```

---

 External company-site applications | ❌ Skipped intentionally |

---

### 💡 Example Workflow

```bash
python apply_agent.py
```

Typical flow:

```text
Login Successful
Fetching Jobs...
Scoring with AI...
Applying...
Saved to applied_jobs.csv
Run Complete
```

---

## ⚠️ Disclaimer

This project is intended for personal automation of your **own** Naukri account. Use responsibly and in accordance with [Naukri's Terms of Service](https://www.naukri.com/termsAndConditions). The authors are not affiliated with Naukri / InfoEdge India Ltd.

---

## 🛣️ Roadmap

- [x] Complete job-search endpoint integration
- [x] Complete one-click job-apply flow

- [ ] Add async support (`httpx` / `aiohttp`)
- [ ] CLI interface

---

## 🤝 Contributing

Pull requests are welcome!
OTP/MFA login is now fully supported. The main area that could use help is **refactoring and cleanup** — improving code structure and formatting without breaking existing functionality.

---
