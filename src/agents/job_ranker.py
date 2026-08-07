"""Orders the collected job pool newest-first, and filters out banned roles.

There is deliberately NO relevance scoring here. Relevance is already decided
by *what we search for* — every keyword in ``SEARCH_QUERIES`` is a role the
candidate wants, so re-scoring the results against the same skills/titles only
reshuffles jobs that are all acceptable anyway. The one signal that genuinely
changes outcomes is RECENCY: recruiters work the applicant pile top-down, so a
30-minute-old posting is worth far more than a 20-hour-old one.

So the rule is simply: sort the whole pool (every search term combined) newest
to oldest, then apply down that list until the daily limit is hit.

Ordering prefers the exact ``createdDate`` timestamp from the listing payload.
The free-text ``posted_date`` ("3 Days Ago", "Just now") is only a fallback,
because with ``JOB_AGE_DAYS`` filtering to the last day or two almost every job
collapses into the same text bucket and the sort would degenerate into noise.
"""

import re
import time

from src.config import agent_config as config

# "30+ days ago", "3 Days Ago", "5 hours ago", "20 minutes ago", "2 weeks ago"
_AGE_RE = re.compile(
    r"(\d+)\s*\+?\s*(minute|min|hour|hr|day|week|month)", re.IGNORECASE
)
_UNIT_HOURS = {
    "minute": 1 / 60, "min": 1 / 60,
    "hour": 1.0, "hr": 1.0,
    "day": 24.0,
    "week": 168.0,
    "month": 720.0,
}
# Phrases that mean "essentially just posted" with no number attached.
_JUST_POSTED = ("just now", "just posted", "few hours", "few minutes",
                "moments ago", "today", "a day ago")

# Buckets used purely to summarise the pool in the console. They do not
# influence apply order — the timestamp sort does.
_AGE_LABELS = (
    ("≤3h",   3.0),
    ("≤12h",  12.0),
    ("today", 24.0),
    ("≤2d",   48.0),
    ("≤3d",   72.0),
    ("≤7d",   168.0),
    ("older", float("inf")),
)


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9+#. ]+", " ", (text or "").lower())


# ---------------------------------------------------------------------------
# Age
# ---------------------------------------------------------------------------

def _age_from_text(posted: str) -> float:
    """Age in hours parsed from Naukri's free-text posted label."""
    match = _AGE_RE.search(posted)
    if match:
        return int(match.group(1)) * _UNIT_HOURS.get(match.group(2).lower(), 24.0)

    if any(marker in posted for marker in _JUST_POSTED):
        # "today"/"a day ago" are vaguer than "2 hours ago" — treat them as
        # same-day but not top-tier, so a precise "1 hour ago" still wins.
        if "hour" in posted or "minute" in posted or "just" in posted:
            return 1.0
        return 20.0

    return config.RANK_UNKNOWN_FRESHNESS_HOURS


def posting_age_hours(job) -> float:
    """Age of the posting in hours — smaller is fresher.

    Uses the exact ``createdDate`` timestamp when the listing provided one,
    which is the only way to order jobs that all render as "1 Day Ago".
    Falls back to parsing the text label, and finally to a configured
    assumption so odd formats are neither promoted to the top nor buried.
    """
    created_ms = getattr(job, "created_ms", None)
    if created_ms:
        age_hours = (time.time() * 1000.0 - float(created_ms)) / 3_600_000.0
        # Guard against clock skew / bad payloads producing a "future" job
        # that would otherwise sort ahead of everything real.
        if age_hours >= 0:
            return age_hours

    posted = (job.posted_date or "").strip().lower()
    if not posted:
        return config.RANK_UNKNOWN_FRESHNESS_HOURS

    return _age_from_text(posted)


def age_label(job) -> str:
    """Coarse human label for the job's age (console summaries only)."""
    age = posting_age_hours(job)
    for label, max_hours in _AGE_LABELS:
        if age <= max_hours:
            return label
    return _AGE_LABELS[-1][0]


# ---------------------------------------------------------------------------
# Exclusions + ordering
# ---------------------------------------------------------------------------

def is_excluded_title(job) -> bool:
    """True for roles we never want, no matter how fresh they are."""
    title = _normalise(job.title)
    return any(term in title for term in config.EXCLUDED_TITLE_TERMS)


def rank_leads(leads: list) -> list:
    """Return the pooled leads sorted newest-first, across all search terms.

    Ties (same timestamp, or both falling back to the same text label) keep
    their original collection order because ``sorted`` is stable. The small
    human-like reordering applied before applying lives in
    ``EasyApplyAgent`` (``JOB_ORDER_DRIFT``), so no jitter is needed here.
    """
    if not config.RANK_JOB_POOL:
        return list(leads)

    return sorted(leads, key=lambda lead: posting_age_hours(lead.job))
