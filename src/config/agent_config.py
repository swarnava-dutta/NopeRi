"""Editable settings for the Naukri apply agent.

Change this file for search targets, candidate profile, filtering rules, and
apply-time defaults. Keep API endpoint constants in constants.py.
"""

# ---------------------------------------------------------------------------
# Search and run settings
# ---------------------------------------------------------------------------

APPLIED_JOBS_CSV = "applied_jobs.csv"
EXTERNAL_JOBS_CSV = "external_jobs.csv"

# Set RUN_RECOMMENDED_PHASE=False to skip recommended jobs and go straight to
# configured search terms.
RUN_RECOMMENDED_PHASE = True
RUN_SEARCH_PHASE = True
DOCUMENT_EXTERNAL_LINKS = True

SEARCH_QUERIES = [
    {"keyword": "AI Engineer", "location": ""},
    {"keyword": "Artificial Intelligence Engineer", "location": ""},
    {"keyword": "Gen AI Engineer", "location": ""},
    {"keyword": "LLM Engineer", "location": ""},
    {"keyword": "RAG Engineer", "location": ""},
    {"keyword": "Applied AI Engineer", "location": ""},
]

EXPERIENCE_LEVELS = [5]
SEARCH_PAGES = 1
JOB_AGE_DAYS = 2
SEARCH_DELAY_SECONDS = 1.2
SEARCH_ERROR_DELAY_SECONDS = 3
APPLY_DELAY_SECONDS = 3
DAILY_APPLY_LIMIT = 50


# ---------------------------------------------------------------------------
# Apply settings
# ---------------------------------------------------------------------------

APPLY_TYPE_ID = "107"
MANDATORY_SKILL_COUNT = 2

APPLY_PAYLOAD_DEFAULTS = {
    "flowtype": "show",
    "crossdomain": True,
    "jquery": 1,
    "rdxMsgId": "",
    "chatBotSDK": True,
    "closebtn": "y",
    "mid": "",
}

QUESTIONNAIRE_PROFILE = {
    "current_ctc": "4050000",
    "expected_ctc": "5500000",
    "exp_total": "5",
    "exp_node": "5",
    "exp_python": "5",
    "notice_days": 30,
    "skills": [
        "RAG",
        "docker",
        "kubernetes",
        "Azure",
        "ci/cd",
        "Agentic AI",
        "Langchain",
        "Langgraph",
    ],
}

DEFAULT_TEXTBOX_ANSWER = "1"
YES_QUESTION_HINTS = ["do you", "have you", "experience"]
