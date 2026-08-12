"""Editable settings for the Naukri apply agent.

Change this file for search targets, candidate profile, filtering rules, and
apply-time defaults. Keep API endpoint constants in constants.py.

The candidate profile (CTC, experience, links, location, education, skills)
lives in ``candidate_profile.json`` at the repo root — edit that file to
change answers; no code change needed.
"""

import json
import os
from datetime import date, datetime

_PROFILE_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "candidate_profile.json",
)

# ---------------------------------------------------------------------------
# Search and run settings
# ---------------------------------------------------------------------------

APPLIED_JOBS_CSV = "applied_jobs.csv"
EXTERNAL_JOBS_CSV = "external_jobs.csv"

# ---------------------------------------------------------------------------
# Diagnostic logging (see src/utils/run_logging.py)
# ---------------------------------------------------------------------------
# Separate from the emoji run report printed to the console. This file is the
# machine-readable trail (raw apply payloads, retries, auth recovery) used to
# work out WHY a run behaved the way it did. It rotates, so it can never grow
# without bound the way logs/noperi_hidden.log did.
LOG_TO_FILE = True
LOG_FILE = "logs/noperi_debug.log"
LOG_FILE_LEVEL = "DEBUG"      # what lands in the file
LOG_CONSOLE_LEVEL = "ERROR"   # keep stdout as the readable emoji report
LOG_MAX_BYTES = 2_000_000     # rotate at ~2 MB
LOG_BACKUP_COUNT = 3          # keep 3 rotations (~8 MB worst case)

# Record the raw apply-workflow response body at DEBUG level. This is what
# proves whether an apply actually landed when the parser disagrees; it costs
# nothing on a healthy run and is the first thing to enable on a bad one.
LOG_RAW_APPLY_RESPONSE = True

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
# Fetch deep enough that external/already-applied/non-AI results do not leave
# fewer candidates than the 100-110 confirmed-application run target.
SEARCH_PAGES = 10
JOB_AGE_DAYS = 1
# Per-run target for CONFIRMED successful applications. Already-applied,
# external, excluded, non-AI, browsed, and failed jobs never consume it.
# With DAILY_LIMIT_JITTER=5 below, each run targets 100..110 successes.
DAILY_APPLY_LIMIT = 105

# Reconcile recently applied Naukri job IDs before a run so applications made
# outside this checkout are not submitted again. The history endpoint is
# paginated; keep the lookback and request ceiling deliberately small.
APPLICATION_HISTORY_SYNC_ENABLED = True
APPLICATION_HISTORY_DAYS = 3
APPLICATION_HISTORY_PAGE_SIZE = 100
APPLICATION_HISTORY_MAX_PAGES = 5

# Server-side result ordering for the search API. "" sends no sort param at
# all, so Naukri returns its default RELEVANCE order — the same thing a
# browser gets on a plain SRP load, and one less non-default parameter for
# the bot heuristics to notice. "f" would ask for Date (freshest first).
SEARCH_SORT_BY = ""


# ---------------------------------------------------------------------------
# Collect → sort → apply pipeline
# ---------------------------------------------------------------------------
# Every search term is collected into ONE pool before a single apply happens.
# The pool is then sorted NEWEST FIRST and applied to top-down until the daily
# limit runs out. Pooling is what stops the budget being burnt by whichever
# keyword happened to be shuffled first.
#
# Search keywords create a candidate pool; the apply-time AI role filter reads
# each full listing and decides target relevance. Local ranking only controls
# which fresh candidates are considered first.

RANK_JOB_POOL = True          # False = keep raw collection order

# Assumed age when a posting has no usable timestamp and its text label can't
# be parsed. Kept mid-range so odd formats are neither promoted nor buried.
RANK_UNKNOWN_FRESHNESS_HOURS = 24.0

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
# without applying (a strong human signal - bots apply to 100% of views).
# Skipped jobs stay eligible for future runs and never consume the limit.
#
# Only applies when AI_ROLE_FILTER has NO opinion on the job. A role the
# filter confirmed as AI/ML is always applied to, never browsed away — the
# roles it rejects are already opened-and-abandoned, which is the same signal
# for free. See EasyApplyAgent._apply_core.
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
DAILY_LIMIT_JITTER = 5                 # per-run success target +/- this amount

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

# Never apply to companies whose name contains any of these strings.
# Matched as a substring against the LOWERCASED company name, so every
# entry here must be lowercase (see EasyApplyAgent._is_blocked_company).
BLOCKED_COMPANIES = [
    "accion labs",
    "biz tech consultants",
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
    "full_name": "",
    "current_ctc": "",
    "expected_ctc": "",
    "gender": "Male",
    "exp_total": "0",
    "exp_ai": "0",
    "team_mentored": "",
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
    "tcs_registration_email": "",
    "tcs_ep_number": "",
    "location_preference": [],
    "skills": [],
}


_PROFILE_DATE_FORMATS = (
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
)


def _remaining_notice_days(last_working_day, today: date | None = None) -> int | None:
    """Return days until LWD, or None when the profile date cannot be parsed."""
    if isinstance(last_working_day, datetime):
        lwd = last_working_day.date()
    elif isinstance(last_working_day, date):
        lwd = last_working_day
    else:
        value = " ".join(str(last_working_day or "").replace(",", " ").split())
        lwd = None
        for date_format in _PROFILE_DATE_FORMATS:
            try:
                lwd = datetime.strptime(value, date_format).date()
                break
            except ValueError:
                continue
        if lwd is None:
            return None

    current_day = today or datetime.now().astimezone().date()
    return max((lwd - current_day).days, 0)


def _load_profile() -> dict:
    try:
        with open(_PROFILE_JSON, encoding="utf-8") as f:
            data = json.load(f)
        # Merge over the fallback so missing keys never crash the agent.
        merged = {**_PROFILE_FALLBACK, **data}
        # Notice shrinks every day while serving. LWD is source of truth;
        # configured notice_days remains fallback for a missing/invalid LWD.
        remaining_days = _remaining_notice_days(merged.get("last_working_day"))
        if remaining_days is not None:
            merged["notice_days"] = remaining_days
        return merged
    except Exception:
        return dict(_PROFILE_FALLBACK)


QUESTIONNAIRE_PROFILE = _load_profile()

DEFAULT_TEXTBOX_ANSWER = "1"

# Free-text fallbacks used when a question is not covered by a fixed rule AND
# the AI is off / fails / stays evasive after a retry. They must never make
# the candidate look worse than the truth. See _fallback_text_answer.
#
# UNKNOWN_TEXT_ANSWER: last resort for a non-numeric question ("Employee
# code", "Java version"). A recruiter reads "N/A" as a clean non-answer,
# whereas an LLM hedge ("I don't have my PAN handy, I'll share it later")
# reads as a red flag.
UNKNOWN_TEXT_ANSWER = "N/A"

# SCALE_TEXTBOX_ANSWER: for volume questions ("how many tokens per month",
# "how many users did it serve"). DEFAULT_TEXTBOX_ANSWER of "1" is literally
# true of nothing and screens the candidate out.
#
# Deliberately NOT a round number. "1000000" reads as an invented marketing
# figure and invites a follow-up the candidate cannot answer; an uneven,
# mid-sized value reads like someone quoting a dashboard they actually
# looked at. Keep it modest enough to defend in an interview.
SCALE_TEXTBOX_ANSWER = "45000"

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
]

# Face-to-face / in-person interview questions → always No. Also appended to
# NO_QUESTION_HINTS below, since matching either list forces the same "No" —
# two hand-synced copies of these terms drifted apart once already.
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

NO_QUESTION_HINTS += F2F_INTERVIEW_HINTS

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


# ---------------------------------------------------------------------------
# AI role filter (OpenAI) — see src/utils/ai_role_filter.py
# ---------------------------------------------------------------------------
# High-recall filter for hands-on AI/ML/GenAI engineering work. It includes
# LLM/RAG/agents, classical ML, CV, model-building data science, MLOps, and AI
# platform engineering; programming language is not an AI-role gate. Clear
# GenAI builder titles pass locally. Ambiguous jobs require OPENAI_API_KEY and
# whole-listing judgement (cached per identical prompt). Legacy OPEN_API_KEY
# is also accepted for compatibility with older local .env files.
# Fails open: if the API is down or unkeyed, ambiguous jobs are not rejected.
AI_ROLE_FILTER = True

# Synchronous chat-completions model. NOT the Batch API — that's async with a
# 24h window (measured: still "validating" after 30s vs ~1.6s sync), and this
# verdict is needed inline while deciding whether to apply right now.
ROLE_FILTER_MODEL = "gpt-5.6-luna"

# How hard the model thinks before answering. Accepted values for this model
# family: "none", "low", "medium", "high", "xhigh" — "minimal" is REJECTED
# with HTTP 400 ("does not support 'minimal' with this model"), so do not use
# it here even though older gpt-5 snapshots took it.
# "medium" is the deliberate choice: the verdict rests on a whole job
# description where the title often contradicts the actual work, and that
# judgement is exactly what reasoning tokens buy. A wrong NO silently drops a
# real AI job for the whole run, which costs far more than a few tokens.
ROLE_FILTER_REASONING_EFFORT = "medium"

# Only one word ("YES"/"NO") is needed, but this cap covers internal reasoning
# tokens too, and those are spent BEFORE anything visible is written. A cap
# sized for the answer alone (16) was being eaten whole by reasoning on long or
# ambiguous JDs, returning empty content with finish_reason="length" — which
# looked random because it depends on how much the model deliberates.
# Sized for ROLE_FILTER_REASONING_EFFORT above: at "medium" the model thinks
# considerably longer than at the old "minimal", so 512 would now be swallowed
# by reasoning on long JDs and come back empty. Raise this if you raise the
# effort to "high"/"xhigh".
# Billing is on tokens actually generated, so an obvious listing still costs
# ~1 output token; only the genuinely hard ones spend more headroom.
ROLE_FILTER_MAX_TOKENS = 4096

# Separate from AI_TIMEOUT_SECONDS: reasoning time scales with the effort
# above, so a "medium" verdict on a long JD takes far longer than a quick
# questionnaire answer. A timeout counts toward AI_MAX_FAILURES, so a value
# sized for the fast path would trip the breaker and silently switch the
# filter off mid-run.
ROLE_FILTER_TIMEOUT_SECONDS = 90



# How much of the job description to send. 0 = the WHOLE description. Title,
# responsibilities, and required skills are judged together; the text is
# HTML-stripped first (job_utils.plain_text). Set a positive number only to cap
# token cost.
AI_ROLE_FILTER_JD_CHARS = 0


# Optional extra free-text context for the AI (education, city, work auth, etc.)
AI_ANSWER_CONTEXT = ""
