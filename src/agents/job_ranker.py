"""Ranks the collected job pool so the daily budget goes to the best jobs.

FRESHNESS IS THE DOMINANT SIGNAL. Applying early matters more than applying
to a marginally better keyword match — recruiters work the pile top-down and
a 6-hour-old posting has far fewer applicants than a 3-day-old one.

Ranking is therefore lexicographic, implemented with a tiered score:

    score = (freshness_tier * TIER_STEP) + relevance

``TIER_STEP`` is floored above the maximum achievable relevance score, so a
fresher job ALWAYS outranks a staler one no matter how well the staler one
matches. Relevance (skills, title, location) only decides the order *within*
a freshness tier. That keeps the guarantee intact even if the relevance
weights are retuned later.

Relevance signals, all read off the search listing (no extra API calls):
  skill overlap  — candidate skills that appear in the job's tags
  title match    — job title contains a preferred role term
  location       — job sits in a preferred city
"""

import random
import re

from src.config import agent_config as config

# Freshness tiers, best first: (label, max_age_hours, tier_value).
# Tier value is the multiplier applied to the tier step — bigger is fresher.
_FRESHNESS_TIERS = (
    ("≤3h",    3.0,    6),
    ("≤12h",   12.0,   5),
    ("today",  24.0,   4),
    ("≤2d",    48.0,   3),
    ("≤3d",    72.0,   2),
    ("≤7d",    168.0,  1),
    ("older",  float("inf"), 0),
)

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


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9+#. ]+", " ", (text or "").lower())


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def posting_age_hours(job) -> float:
    """Best-effort age of the posting in hours.

    Naukri's ``footerPlaceholderLabel`` is free text ("3 Days Ago", "Just
    now", "30+ Days Ago"). Unparseable values fall back to a configured
    assumption rather than being treated as brand new (which would let odd
    formats jump the queue) or ancient (which would bury them).
    """
    posted = (job.posted_date or "").strip().lower()

    if not posted:
        return config.RANK_UNKNOWN_FRESHNESS_HOURS

    match = _AGE_RE.search(posted)
    if match:
        value = int(match.group(1))
        unit = match.group(2).lower()
        return value * _UNIT_HOURS.get(unit, 24.0)

    if any(marker in posted for marker in _JUST_POSTED):
        # "today"/"a day ago" are vaguer than "2 hours ago" — treat them as
        # same-day but not top-tier, so a precise "1 hour ago" still wins.
        return 1.0 if "hour" in posted or "minute" in posted or "just" in posted else 20.0

    return config.RANK_UNKNOWN_FRESHNESS_HOURS


def freshness_tier(job) -> tuple[str, int]:
    """(label, tier_value) for the job's posting age. Higher value = fresher."""
    age = posting_age_hours(job)
    for label, max_hours, value in _FRESHNESS_TIERS:
        if age <= max_hours:
            return label, value
    return _FRESHNESS_TIERS[-1][0], _FRESHNESS_TIERS[-1][2]


def _max_relevance() -> float:
    """Ceiling on the relevance half of the score."""
    return (
        config.RANK_SKILL_MAX
        + config.RANK_TITLE_WEIGHT
        + config.RANK_LOCATION_WEIGHT
        + config.RANK_TIE_JITTER
    )


def _tier_step() -> float:
    """Points per freshness tier.

    Floored just above the maximum relevance score when
    RANK_FRESHNESS_DOMINANT is on, which is what guarantees that no amount
    of relevance can promote a staler job over a fresher one.
    """
    step = config.RANK_FRESHNESS_WEIGHT
    if config.RANK_FRESHNESS_DOMINANT:
        step = max(step, _max_relevance() + 1.0)
    return step


# ---------------------------------------------------------------------------
# Relevance (tie-breakers within a freshness tier)
# ---------------------------------------------------------------------------

def _skill_score(job) -> float:
    """Weighted overlap between candidate skills and the job's tags."""
    skills = config.QUESTIONNAIRE_PROFILE.get("skills") or []
    if not skills or not job.tags:
        return 0.0

    haystack = _normalise(" ".join(job.tags))
    hits = 0
    for skill in skills:
        # "RAG (Retrieval-Augmented Generation)" -> "rag"
        token = _normalise(skill.split("(")[0]).strip()
        if len(token) < 2:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", haystack):
            hits += 1

    return min(hits * config.RANK_SKILL_WEIGHT, config.RANK_SKILL_MAX)


def _title_score(job) -> float:
    title = _normalise(job.title)
    return (
        config.RANK_TITLE_WEIGHT
        if any(term in title for term in config.PREFERRED_TITLE_TERMS)
        else 0.0
    )


def _location_score(job) -> float:
    preferences = config.QUESTIONNAIRE_PROFILE.get("location_preference") or []
    if not preferences:
        return 0.0

    location = _normalise(job.location)
    return (
        config.RANK_LOCATION_WEIGHT
        if any(_normalise(pref).strip() in location for pref in preferences)
        else 0.0
    )


def relevance_score(job) -> float:
    """How well the job matches the candidate, ignoring recency."""
    return _skill_score(job) + _title_score(job) + _location_score(job)


def score_job(job) -> float:
    """Total ranking score: freshness tier dominates, relevance breaks ties."""
    _, tier_value = freshness_tier(job)
    return tier_value * _tier_step() + relevance_score(job)


# ---------------------------------------------------------------------------
# Exclusions + ordering
# ---------------------------------------------------------------------------

def is_excluded_title(job) -> bool:
    """True for roles we never want regardless of how well they score."""
    title = _normalise(job.title)
    return any(term in title for term in config.EXCLUDED_TITLE_TERMS)


def rank_leads(leads: list) -> list:
    """Return the pool sorted freshest-first, then most relevant.

    The jitter only perturbs the relevance half — it can never push a job
    across a freshness tier boundary, since the tier step is strictly
    greater than max relevance + max jitter.
    """
    if not config.RANK_JOB_POOL:
        return list(leads)

    for lead in leads:
        lead.score = score_job(lead.job)

    jitter = config.RANK_TIE_JITTER
    return sorted(
        leads,
        key=lambda lead: lead.score + random.uniform(0.0, jitter),
        reverse=True,
    )
