# Noperi

Selenium-free Python API client for Naukri job search and easy-apply automation.

## Features

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
| `get_recommended_jobs()` | Returns recommended jobs |
| `search_jobs(...)` | Returns job search results |
| `get_job_details(job_id)` | Fetches job details |
| `apply_job(job)` | Applies to a job |
| `handle_static_questionnaire_and_apply(...)` | Answers supported questionnaires and applies |

## Notes

- `cookies.json` is intentionally not in `.gitignore`.
- Sessions are IP-bound. Changing IP can invalidate cookies.
- `nkparam` is generated in code; Selenium harvester exists only as fallback.

## Disclaimer

Use only for personal automation of your own Naukri account and follow Naukri terms.
