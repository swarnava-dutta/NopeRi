"""Editable settings for the Naukri apply agent.

Change this file for search targets, candidate profile, filtering rules, and
apply-time defaults. Keep API endpoint constants in constants.py.

The candidate profile (CTC, experience, links, location, education, skills)
lives in ``candidate_profile.json`` at the repo root — edit that file to
change answers; no code change needed.
"""

import json
import os

_PROFILE_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "candidate_profile.json",
)

# ---------------------------------------------------------------------------
# Search and run settings
# ---------------------------------------------------------------------------

APPLIED_JOBS_CSV = "applied_jobs.csv"
EXTERNAL_JOBS_CSV = "external_jobs.csv"

# Set RUN_RECOMMENDED_PHASE=False to skip recommended jobs and go straight to
# configured search terms.
RUN_RECOMMENDED_PHASE = False
RUN_SEARCH_PHASE = True
DOCUMENT_EXTERNAL_LINKS = True

SEARCH_QUERIES = [
    {"keyword": "Forward Deployed Engineer", "location": ""},
    {"keyword": "AI Engineer", "location": ""},
    {"keyword": "Artificial Intelligence Engineer", "location": ""},
    {"keyword": "Gen AI Engineer", "location": ""},
    {"keyword": "LLM Engineer", "location": ""},
    {"keyword": "RAG Engineer", "location": ""},
    {"keyword": "Applied AI Engineer", "location": ""},
]

EXPERIENCE_LEVELS = [5]
SEARCH_PAGES = 3
JOB_AGE_DAYS = 1
DAILY_APPLY_LIMIT = 50


# ---------------------------------------------------------------------------
# Collect → rank → apply pipeline
# ---------------------------------------------------------------------------
# Every search term is collected into ONE pool before a single apply happens,
# then the pool is ranked so the daily budget goes to the best-matching jobs
# instead of whichever keyword happened to be shuffled first.

RANK_JOB_POOL = True          # False = keep raw collection order

# FRESHNESS IS THE TOP-PRIORITY SIGNAL.
# Jobs are bucketed into freshness tiers (≤3h, ≤12h, today, 1-2d, ...) and
# each tier is worth more than every other signal combined. A fresher job
# therefore ALWAYS outranks a staler one; relevance only decides the order
# *within* a tier. Being an early applicant beats being a slightly better
# keyword match.
RANK_FRESHNESS_WEIGHT = 30.0     # points per freshness tier (not a flat bonus)
RANK_FRESHNESS_DOMINANT = True   # floor the tier step above every other signal,
                                 # so retuning the weights below can't silently
                                 # break the "freshest first" guarantee
RANK_UNKNOWN_FRESHNESS_HOURS = 24.0  # assumed age when the posted date is
                                     # unparseable (don't bury odd formats)

# Relevance weights — these break ties *inside* a freshness tier.
RANK_SKILL_WEIGHT = 3.0       # per candidate skill found in the job's tags
RANK_SKILL_MAX = 18.0         # cap so a tag-stuffed listing can't dominate
RANK_TITLE_WEIGHT = 6.0       # job title contains a preferred title term
RANK_LOCATION_WEIGHT = 2.5    # job location matches location_preference
RANK_TIE_JITTER = 0.75        # random tie-break so equal scores don't
                              # produce an identical order every run

# Title terms that indicate a strongly relevant role.
PREFERRED_TITLE_TERMS = [
    "ai engineer",
    "gen ai",
    "genai",
    "generative ai",
    "llm",
    "rag",
    "machine learning",
    "applied ai",
    "forward deployed",
    "agentic",
]

# Title terms that disqualify a job outright (never applied to).
EXCLUDED_TITLE_TERMS = [
    "intern",
    "internship",
    "fresher",
    "trainee",
    "sales",
    "bpo",
    "telecaller",
]



# ---------------------------------------------------------------------------
# Anti-ban / humanization settings (see src/utils/humanizer.py)
# ---------------------------------------------------------------------------
# Randomizes timing/behaviour so requests look human. NOTE: no client-side
# randomization protects a datacenter IP — run from a residential IP
# (see src/client/naukri_client.py).

HUMANIZE = True                        # master switch

# Randomized minimum gap enforced between ANY two API calls.
MIN_REQUEST_GAP_SECONDS = 1.5
MAX_REQUEST_GAP_SECONDS = 4.5

# Randomized delay ranges (min, max seconds).
APPLY_DELAY_MIN_SECONDS = 4.0          # between applies
APPLY_DELAY_MAX_SECONDS = 12.0
SEARCH_DELAY_MIN_SECONDS = 2.0         # between search pages
SEARCH_DELAY_MAX_SECONDS = 7.0
SEARCH_ERROR_DELAY_MIN_SECONDS = 5.0   # after a search error
SEARCH_ERROR_DELAY_MAX_SECONDS = 15.0
QUERY_DELAY_MIN_SECONDS = 5.0          # between search terms
QUERY_DELAY_MAX_SECONDS = 20.0
READING_PAUSE_MIN_SECONDS = 1.5        # "reading the JD" before applying
READING_PAUSE_MAX_SECONDS = 6.0
SESSION_WARMUP_MIN_SECONDS = 3         # random start delay (breaks cron timing)
SESSION_WARMUP_MAX_SECONDS = 25

# Occasional long "walked away" breaks between applies.
LONG_BREAK_PROBABILITY = 0.08
LONG_BREAK_MIN_SECONDS = 20
LONG_BREAK_MAX_SECONDS = 90

# Session fatigue: humans slow down the longer they browse. Every processed
# job stretches all delays a little, up to the cap.
FATIGUE_RAMP_PER_JOB = 0.02            # +2% delay per processed job
FATIGUE_MAX_MULTIPLIER = 1.6           # never slower than 1.6x base delays

# Window shopping: occasionally open a job, read it, and just move on
# without applying (a strong human signal — bots apply to 100% of views).
# Skipped jobs stay eligible for future runs and never consume the limit.
WINDOW_SHOPPING_PROBABILITY = 0.04

# Reading pause scales with JD length — longer descriptions take longer.
READING_SECONDS_PER_1000_CHARS = 2.0   # extra seconds per 1000 JD chars
READING_PAUSE_EXTRA_MAX_SECONDS = 8.0  # cap on length-based extra time

# Humans don't apply strictly top-to-bottom: each job may drift up to this
# many positions in the apply order (0 = keep exact search order).
JOB_ORDER_DRIFT = 3

# 403/429 defense: randomized cooldown (escalates per block), lasting
# slowdown cap, and hard abort after too many blocks.
BLOCK_COOLDOWN_MIN_SECONDS = 60
BLOCK_COOLDOWN_MAX_SECONDS = 180
MAX_BLOCK_PENALTY_SECONDS = 30.0
MAX_BLOCKS_BEFORE_ABORT = 3

SHUFFLE_SEARCH_QUERIES = True          # shuffle query order each run
DAILY_LIMIT_JITTER = 3                 # daily limit +/- this amount per run (anti-pattern: never exactly N daily)

# Transient server error (5xx) retry: Naukri's job APIs randomly return
# HTTP 500 "System Error" HTML pages (flaky backend, not a client problem).
# Retry with exponential backoff + jitter before failing the job.
TRANSIENT_RETRY_ATTEMPTS = 3           # total attempts per request
TRANSIENT_RETRY_BASE_SECONDS = 3.0     # first retry wait (doubles each retry)
TRANSIENT_RETRY_MAX_SECONDS = 20.0     # cap on any single retry wait


# ---------------------------------------------------------------------------
# Apply settings
# ---------------------------------------------------------------------------

APPLY_TYPE_ID = "107"
MANDATORY_SKILL_COUNT = 2

# Never apply to companies whose name contains any of these strings
# (case-insensitive substring match against the job's company name).
BLOCKED_COMPANIES = [
    "accion labs",
]

APPLY_PAYLOAD_DEFAULTS = {
    "flowtype": "show",
    "crossdomain": True,
    "jquery": 1,
    "rdxMsgId": "",
    "chatBotSDK": True,
    "closebtn": "y",
    "mid": "",
}

# Structural fallback used ONLY if candidate_profile.json is missing/broken
# or a key is absent from it. candidate_profile.json is the single source of
# truth for all personal details — do NOT duplicate real values here. These
# neutral defaults just guarantee every key exists so the agent never crashes.
_PROFILE_FALLBACK = {
    "current_ctc": "",
    "expected_ctc": "",
    "gender": "Male",
    "exp_total": "0",
    "exp_ai": "0",
    "notice_days": 30,
    "notice_status": "",
    "last_working_day": "",
    "available_to_join_from": "",
    "current_company": "",
    "current_location": "",
    "date_of_birth": "",
    "pan_number": "",
    "phone": "",
    "email": "",
    "linkedin_url": "",
    "github_url": "",
    "highest_qualification": "Bachelor's degree",
    "graduation_year": "",
    "has_masters": False,
    "has_postgraduation": False,
    "tcs_registration_email": "",
    "tcs_ep_number": "",
    "location_preference": [],
    "skills": [],
}


def _load_profile() -> dict:
    try:
        with open(_PROFILE_JSON, encoding="utf-8") as f:
            data = json.load(f)
        # Merge over the fallback so missing keys never crash the agent.
        merged = {**_PROFILE_FALLBACK, **data}
        return merged
    except Exception:
        return dict(_PROFILE_FALLBACK)


QUESTIONNAIRE_PROFILE = _load_profile()

DEFAULT_TEXTBOX_ANSWER = "1"

# Questions matching these hints are ALWAYS answered "No" (checked before
# anything else). e.g. "Have you worked here before?", "Have you applied to
# this company earlier?", "Are you an ex-employee?"
NO_QUESTION_HINTS = [
    "worked here",
    "worked with us",
    "worked at this",
    "worked for this",
    "worked in this",
    "previously worked",
    "previously employed",
    "previously associated",
    "ex-employee",
    "ex employee",
    "former employee",
    "applied before",
    "applied earlier",
    "applied to this",
    "applied for this",
    "applied with us",
    "applied here",
    "interviewed before",
    "interviewed earlier",
    "interviewed with us",
    "interviewed here",
    "relatives",
    "criminal",
    # Face-to-face / in-person interview questions → always No
    "f2f",
    "face to face",
    "face-to-face",
    "in person interview",
    "in-person interview",
    "walk-in",
    "walkin",
]

# Questions matching these hints are ALWAYS answered "Yes".
RELOCATION_HINTS = [
    "reloc",  # relocate / relocation / relocating
    "willing to move",
    "willing to shift",
    "move to",
    "shift to",
]

# Domain/industry experience questions are ALWAYS answered "Yes".
# Generic words catch phrasing like "XYZ domain"; named domains catch
# questions like "Have you worked in banking/medtech/fintech?".
DOMAIN_EXPERIENCE_HINTS = [
    "domain",
    "industry",
    "sector",
    "vertical",
    "banking",
    "bfsi",
    "finance",
    "financial",
    "fintech",
    "payments",
    "insurance",
    "healthcare",
    "health care",
    "medtech",
    "medical",
    "pharma",
    "life sciences",
    "retail",
    "ecommerce",
    "e-commerce",
    "telecom",
    "manufacturing",
    "automotive",
    "logistics",
    "supply chain",
    "travel",
    "hospitality",
    "education",
    "edtech",
    "real estate",
    "saas",
    "crm",
    "erp",
    "energy",
    "utilities",
    "oil and gas",
    "government",
    "public sector",
]

# Fallback-only hints: used to pick "Yes" when the AI is disabled or fails.
YES_QUESTION_HINTS = ["do you", "have you", "experience"]

# Experience questions mentioning any of these terms (word-boundary matched)
# are answered with QUESTIONNAIRE_PROFILE["exp_ai"] years.
AI_EXPERIENCE_TERMS = [
    "ai",
    "a.i",
    "artificial intelligence",
    "gen ai",
    "genai",
    "generative",
    "llm",
    "llms",
    "rag",
    "machine learning",
    "ml",
    "deep learning",
    "nlp",
    "agentic",
    "langchain",
    "langgraph",
    "llamaindex",
    "prompt engineering",
    "mcp",
    "data science",
    "chatbot",
    "openai",
]

F2F_INTERVIEW_HINTS = [
    "f2f",
    "face to face",
    "face-to-face",
    "in person",
    "in-person",
    "inperson",
    "walk-in",
    "walkin",
]


# ---------------------------------------------------------------------------
# AI answer fallback (Claude)
# ---------------------------------------------------------------------------
# When a questionnaire question is not covered by the fixed rules above,
# Claude answers it using QUESTIONNAIRE_PROFILE as context.
# Requires ANTHROPIC_API_KEY in the environment / .env file.
# If the key is missing or the API fails, the agent silently falls back to
# DEFAULT_TEXTBOX_ANSWER / first-option behaviour.

USE_AI_ANSWERS = True
AI_MODEL = "claude-haiku-4-5"
AI_MAX_TOKENS = 300
AI_TIMEOUT_SECONDS = 20
# Disable AI for the rest of the run after this many consecutive API failures.
AI_MAX_FAILURES = 3
# Optional extra free-text context for the AI (education, city, work auth, etc.)
AI_ANSWER_CONTEXT = ""
