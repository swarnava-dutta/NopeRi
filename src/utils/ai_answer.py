"""Claude-powered fallback for questionnaire answers.

The fixed rules in ``src/utils/questionnaire.py`` stay authoritative.
When a question is NOT covered by those rules, this module asks Claude
(Haiku 4.5 by default) to answer it using the candidate profile from
``agent_config.QUESTIONNAIRE_PROFILE``.

Requires ``ANTHROPIC_API_KEY`` in the environment / ``.env`` file.
Fails soft: on any error it returns ``None`` so callers can fall back to
the static defaults. After ``AI_MAX_FAILURES`` consecutive API failures the
module disables itself for the rest of the run to avoid slowing applies.
"""

import logging
import os

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is in requirements.txt
    pass

from src.config import agent_config as config

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Answers are cached per run: the same question often repeats across jobs.
_answer_cache: dict[str, str] = {}
_consecutive_failures = 0


def _api_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


def ai_enabled() -> bool:
    """True when the AI fallback is configured, keyed, and still healthy."""
    if not getattr(config, "USE_AI_ANSWERS", False):
        return False
    if _consecutive_failures >= getattr(config, "AI_MAX_FAILURES", 3):
        return False
    return bool(_api_key())


def _profile_context() -> str:
    profile = config.QUESTIONNAIRE_PROFILE
    lines = [
        f"- Current CTC (INR per year): {profile.get('current_ctc', 'N/A')}",
        f"- Expected CTC (INR per year): {profile.get('expected_ctc', 'N/A')}",
        f"- Gender: {profile.get('gender', 'Male')}",
        f"- Total experience (years): {profile.get('exp_total', 'N/A')}",
        f"- AI/ML/GenAI/LLM/RAG-related experience (years): "
        f"{profile.get('exp_ai', 'N/A')}",
        f"- Notice period (days): {profile.get('notice_days', 'N/A')}",
        f"- Current company / employer: {profile.get('current_company', 'N/A')}",
        f"- Current location / city: {profile.get('current_location', 'N/A')}",
        f"- Phone number: {profile.get('phone', 'N/A')}",
        f"- Email (default, for everything except TCS): "
        f"{profile.get('email', 'N/A')}",
        f"- LinkedIn URL: {profile.get('linkedin_url', 'N/A')}",
        f"- GitHub URL: {profile.get('github_url', 'N/A')}",
        f"- Highest qualification: "
        f"{profile.get('highest_qualification', 'N/A')} "
        "(NO Masters, NO postgraduation, NO PhD)",
        f"- Bachelor's graduation / passing year: "
        f"{profile.get('graduation_year', 'N/A')}",
        f"- TCS registration email: "
        f"{profile.get('tcs_registration_email', 'N/A')}",
        f"- TCS registration / EP number: "
        f"{profile.get('tcs_ep_number', 'N/A')}",
        f"- Preferred work locations (in priority order): "
        f"{' > '.join(profile.get('location_preference', []) or ['N/A'])} "
        "> any other",
        f"- Skills: {', '.join(profile.get('skills', []))}",
    ]
    extra = (getattr(config, "AI_ANSWER_CONTEXT", "") or "").strip()
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _system_prompt() -> str:
    return (
        "You are filling out a job application questionnaire on behalf of a "
        "candidate. Your single goal is to get the candidate SELECTED at the "
        "screening stage. Answer as the candidate, favorably but plausibly, "
        "based on this profile:\n"
        f"{_profile_context()}\n\n"
        "Rules:\n"
        "- Keep answers short. For factual questions: a number or a few "
        "words. For descriptive 'how/why/describe' questions: 1-2 complete "
        "sentences maximum, never cut off mid-sentence.\n"
        "- NEVER answer with a range (e.g. '4-6 years'). Always give one "
        "exact number.\n"
        "- For gender questions, answer with the exact gender from the "
        "profile.\n"
        "- For option questions, if an option is '>5 years', '> 5 years', "
        "'5+ years', or equivalent, pick that option.\n"
        "- Always answer 'No' to questions about past association with the "
        "company: worked here/there before, ex-employee, previously applied, "
        "previously interviewed, relatives/friends employed at the company, "
        "or any conflict-of-interest / criminal-record style question.\n"
        "- Always answer 'No' to face-to-face (F2F), in-person interview, "
        "or walk-in availability questions.\n"
        "- Always answer 'Yes' to interview availability questions when the "
        "question does not mention F2F, face-to-face, in-person, or walk-in.\n"
        "- Always answer 'Yes' to relocation or willingness-to-move "
        "questions (any city, any location).\n"
        "- Always answer 'Yes' to availability, immediate joining, work "
        "from office/hybrid, shift, and travel willingness questions.\n"
        "- Always answer 'Yes' to domain, industry, sector, or vertical "
        "experience questions, including banking, medtech, healthcare, "
        "finance, fintech, insurance, retail, telecom, manufacturing, or "
        "any other named business domain.\n"
        "- The candidate has NO Masters degree, NO postgraduation, and NO "
        "PhD — answer 'No' to any question about those. Highest "
        "qualification is a Bachelor's degree.\n"
        "- For LinkedIn/GitHub/portfolio URL, phone number, or current "
        "location questions, reply with the exact value from the profile.\n"
        "- For email / email address / email ID questions, reply with the "
        "default email from the profile — UNLESS the question mentions TCS "
        "or TCS registration, in which case reply with the TCS registration "
        "email from the profile.\n"
        "- For TCS registration email / email used for TCS registration "
        "questions, reply with the exact TCS registration email from the "
        "profile.\n"
        "- For TCS registration number / EP number / EP reference number "
        "questions, reply with the exact TCS registration / EP number from "
        "the profile.\n"
        "- For graduation year / year of passing / batch questions, reply "
        "with the Bachelor's graduation year from the profile.\n"
        "- For preferred location / job location / base location choice "
        "questions with options, pick using the profile's location priority: "
        "first choice if available, else the next preferred, else any other "
        "offered location.\n"
        "- For skill or experience yes/no questions, answer 'Yes' if the "
        "skill is in or adjacent to the profile skills; think about what a "
        "recruiter wants to hear.\n"
        "- For 'years of experience' questions about ANY AI-related topic "
        "(AI, ML, machine learning, deep learning, GenAI, generative AI, "
        "LLM, RAG, NLP, agentic AI, LangChain, LangGraph, data science, "
        "chatbots, prompt engineering), always reply with the AI-related "
        "experience years from the profile.\n"
        "- For 'years of experience' questions about ANYTHING ELSE (any "
        "non-AI skill, tool, language, domain, or total/overall experience), "
        "always reply with the total experience years from the profile.\n"
        "- For every other yes/no question, pick whichever answer maximizes "
        "the chance of passing the screening, unless the profile clearly "
        "contradicts it.\n"
        "- For numeric questions (years, CTC, notice period), reply with the "
        "number only, converted to the unit the question asks for. The "
        "candidate's notice period is given in DAYS: if asked in months, "
        "convert (30 days = 1 month); if asked in weeks, convert (30 days = "
        "4 weeks); if asked 'how soon can you join' reply with the notice "
        "period in the unit asked (default days).\n"
        "- Never explain your answer. Never add extra sentences."
    )


def _call_claude(user_prompt: str) -> str | None:
    """Single message call to the Anthropic API. Returns text or None."""
    global _consecutive_failures

    payload = {
        "model": getattr(config, "AI_MODEL", "claude-haiku-4-5"),
        "max_tokens": getattr(config, "AI_MAX_TOKENS", 100),
        "system": _system_prompt(),
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": _api_key(),
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    try:
        res = requests.post(
            ANTHROPIC_API_URL,
            headers=headers,
            json=payload,
            timeout=getattr(config, "AI_TIMEOUT_SECONDS", 20),
        )
        if not res.ok:
            _consecutive_failures += 1
            logger.warning("Claude API error %s: %s", res.status_code, res.text[:200])
            return None

        data = res.json()
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        ).strip()

        if not text:
            _consecutive_failures += 1
            return None

        _consecutive_failures = 0
        return text

    except Exception as exc:  # network errors, timeouts, bad JSON
        _consecutive_failures += 1
        logger.warning("Claude API call failed: %s", exc)
        return None


def _clean_text_answer(text: str) -> str:
    # Collapse to a single line, strip wrapping quotes, and cap length.
    answer = " ".join(text.split()).strip().strip('"').strip("'").strip()
    return answer[:500]


def ai_text_answer(question_text: str) -> str | None:
    """Answer a free-text question. Returns None if AI is off or fails."""
    if not ai_enabled() or not question_text.strip():
        return None

    cache_key = f"text::{question_text.strip().lower()}"
    if cache_key in _answer_cache:
        return _answer_cache[cache_key]

    prompt = (
        f"Questionnaire question: {question_text.strip()}\n"
        "Reply with only the answer text."
    )
    raw = _call_claude(prompt)
    if not raw:
        return None

    answer = _clean_text_answer(raw)
    if not answer:
        return None

    _answer_cache[cache_key] = answer
    print(f"🤖 AI answered (text):\n   Q: {question_text.strip()}\n   A: {answer}")
    return answer


def ai_option_answer(question_text: str, options: dict) -> str | None:
    """Pick the best option key for a multiple-choice question.

    Returns a key from ``options`` or None if AI is off, fails, or replies
    with something that cannot be mapped back to an option.
    """
    if not ai_enabled() or not options:
        return None

    normalized = {str(k): str(v) for k, v in options.items()}
    cache_key = (
        f"option::{question_text.strip().lower()}"
        f"::{'|'.join(sorted(normalized.values())).lower()}"
    )
    if cache_key in _answer_cache:
        return _answer_cache[cache_key]

    option_lines = "\n".join(f"{k}: {v}" for k, v in normalized.items())
    prompt = (
        f"Questionnaire question: {question_text.strip()}\n"
        f"Options (key: label):\n{option_lines}\n"
        "Reply with only the key of the single best option."
    )
    raw = _call_claude(prompt)
    if not raw:
        return None

    reply = _clean_text_answer(raw)

    # Exact key match first.
    key = None
    if reply in normalized:
        key = reply
    else:
        # Fall back to matching the reply against option labels.
        reply_lower = reply.lower()
        for k, label in normalized.items():
            if label.lower() == reply_lower:
                key = k
                break
        if key is None:
            for k, label in normalized.items():
                if reply_lower and reply_lower in label.lower():
                    key = k
                    break

    if key is None:
        logger.warning(
            "Claude reply %r did not match any option for %r", reply, question_text
        )
        return None

    _answer_cache[cache_key] = key
    options_str = " | ".join(normalized.values())
    print(
        f"🤖 AI answered (option):\n   Q: {question_text.strip()}\n"
        f"   Options: {options_str}\n"
        f"   A: {normalized.get(key, key)}"
    )
    return key
