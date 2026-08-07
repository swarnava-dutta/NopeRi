# Noperi

A lightweight and Selenium-free Python API client for Naukri.com, designed to help you search jobs and apply to jobs (easy apply) programmatically.

---

**Status:** 🟢 Working (Last tested: June 2026)

---

## ✨ Features

| Feature | Status |
|---|---|
| Cookie login and session management | Working |
| Recommended jobs feed | Working |
| `nkparam` token generator | Working |
| Job search (`/jobapi/v3/search`) | Working |
| Job details (`/jobapi/v1/job/`) | Working |
| One-click job apply | Working |
| Questionnaire handling (rules + Claude AI fallback) | Working |
| OTP/MFA helpers | Working |
| Anti-ban humanization (pacing, jitter, cooldowns) | Working |

## Setup

```bash
pip install -r requirements.txt
```

Put logged-in Naukri browser cookies in root `cookies.json`.
The file can be a browser-export cookie list or simple name/value object.
It must include `nauk_at`.

Optional: add an Anthropic API key to `.env` for AI questionnaire answers:

```env
ANTHROPIC_API_KEY=your-anthropic-api-key
```

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
python main.py
```

Edit agent settings in `src/config/agent_config.py`:

- search keywords, locations, experience, pages, and job age
- daily apply limit (with jitter), mandatory skill split, delays, and payload defaults
- `RUN_RECOMMENDED_PHASE=False` to skip recommended jobs and go straight to search agents
- `RUN_SEARCH_PHASE=False` to run recommended jobs only
- `DOCUMENT_EXTERNAL_LINKS=True` to save company-site apply links to `external_jobs.csv`
- `BLOCKED_COMPANIES` to never apply to specific companies
- anti-ban / humanization knobs (`HUMANIZE`, delays, cooldowns, abort thresholds)

Candidate profile (CTC, experience, notice period, LWD, joining availability,
links, skills) lives in
`candidate_profile.json` at the repo root — edit that file to change
questionnaire answers; no code change needed.

Plain Python agent flow:

1. Login using `cookies.json`
2. `NaukriApplyOrchestrator` starts the phase order
3. `job_sources.run_recommended` fetches recommended jobs, if enabled
4. `job_sources.run_search_term` runs once per configured search term, if enabled
5. `EasyApplyAgent` applies Naukri easy-apply jobs
6. `ExternalLinkAgent` documents company-site apply links in `external_jobs.csv`
7. Applied job IDs are saved to `applied_jobs.csv`

Questionnaire answering (fixed rules + AI fallback) lives in
`src/utils/questionnaire.py`.

When a job has questionnaire questions, fixed rules answer the common ones
(CTC, notice period, relocation, etc.) and Claude answers the rest using the
candidate profile. The applied row stores a `questionnaire_answers` JSON
array with `question_id`, `question`, `answer`, and `raw_answer`.

Runtime logs use readable status symbols:

- `✅ Applied`
- `❌ External link`
- `⏭️ Already applied`
- `🚫 Blocked company`
- `⚠️ Failed`

## API

### `NaukriLoginClient`

| Method | Description |
|---|---|
| `login()` | Loads `cookies.json`, verifies session, refreshes cookies back into same file |
| `send_otp()` / `verify_otp()` | OTP/MFA login helpers |
| `get_application_history()` | Fetches application history |

### `NaukriJobClient`

| Method | Description |
|---|---|
| `get_recommended_jobs()` | Returns a list of `Job` objects personalised to your profile |
| `search_jobs(keyword, location, page, experience, ...)` | Returns job results using the search endpoint |
| `apply_job(job)` | Applies to a job programmatically |
| `handle_static_questionnaire_and_apply(...)` | Answers questionnaire and applies |

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

## 🔑 The `nkparam` Header

Naukri's job-search endpoint (`/jobapi/v3/search`) requires a request header
called `nkparam` — an encrypted, timestamped signature generated inside
Naukri's obfuscated JavaScript bundle. If it is missing or invalid, the API
returns `403 Forbidden`.

This project generates `nkparam` directly via RSA encryption
(`src/utils/nkparam_generator.py`) — no browser required.

---

## ⚠️ IP / Hosting Advice

Naukri fingerprints the IP of every login and API request. Datacenter IPs
(Azure, GitHub Actions, most GCP) get MFA-challenged or banned on sight.

- **Works:** home broadband, mobile hotspot, residential proxy, AWS EC2 with a clean Elastic IP
- **Avoid:** Azure, GitHub Actions/CI runners, known cloud CIDR ranges

The bearer token and cookies are tied to the login IP — keep the same IP for
the full session lifetime.

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
