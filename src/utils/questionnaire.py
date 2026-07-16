"""Rule-based questionnaire answering with an AI fallback.

Fixed rules answer the common screening questions (CTC, notice period,
relocation, gender, domain experience, ...). Anything not covered is passed
to the Claude fallback in ``ai_answer``; if that is disabled or fails, the
static defaults from ``agent_config`` apply.

All hint lists (NO_QUESTION_HINTS, RELOCATION_HINTS, ...) live in
``agent_config.py`` so behaviour can be tuned without touching this module.
"""

import re

from src.config import agent_config as config
from src.utils.ai_answer import ai_option_answer, ai_text_answer

_MASTERS_HINTS = (
    "master", "post graduat", "postgraduat", "post-graduat",
    "pg degree", "m.tech", "mtech", "m.sc", "msc", "mba", "phd",
)

_COMPANY_HISTORY_WORDS = (
    "company", "organization", "organisation", "employer", "employee",
    "with us", "for us", "at us", "here",
    "ex-employee", "ex employee", "former employee",
    "previously employed", "previously associated",
    "applied before", "applied earlier",
    "interviewed before", "interviewed earlier",
    "relatives", "criminal",
)

_COMPANY_HISTORY_ACTIONS = (
    "worked", "employed", "associated", "applied",
    "interviewed", "relative", "criminal",
)

_PREVIOUS_INTERVIEW_HINTS = (
    "interviewed before", "interviewed earlier", "previously interviewed",
    "previous interview", "interview before", "interview earlier",
)


# ---------------------------------------------------------------------------
# Option pickers
# ---------------------------------------------------------------------------

def _pick_yes(options: dict) -> str:
    """Prefer any option whose label contains 'yes'."""
    for k, v in options.items():
        if "yes" in v.lower():
            return k
    return next(iter(options))


def _pick_no(options: dict) -> str:
    """Prefer any option whose label is/starts with 'no' (but not 'know'),
    falling back to the last option."""
    for k, v in options.items():
        label = v.lower().strip()
        if label == "no" or label.startswith("no,") or label.startswith("no "):
            return k
    for k, v in options.items():
        if "no" in v.lower() and "know" not in v.lower():
            return k
    return list(options.keys())[-1]


def _pick_gender(options: dict, gender: str) -> str:
    target = (gender or "Male").lower().strip()
    for k, v in options.items():
        if str(v).lower().strip() == target:
            return k
    for k, v in options.items():
        if re.search(rf"(?<![a-z]){re.escape(target)}(?![a-z])", str(v).lower()):
            return k
    return next(iter(options))


def _pick_over_5_years(options: dict) -> str | None:
    for k, v in options.items():
        label = " ".join(str(v).lower().split())
        compact = re.sub(r"[\s\-]+", "", label)
        if compact in (">5years", ">5yrs", "5+years", "5+yrs"):
            return k
        if re.search(r"\b(more than|above|over|greater than)\s*5\s*(years|yrs?)\b", label):
            return k
    return None


def _pick_notice(options: dict, notice_days: int) -> str:
    """Match the closest notice period bucket to notice_days."""
    for k, v in options.items():
        val = v.lower()
        if ("immediate" in val or "0 day" in val) and notice_days <= 0:
            return k
        if "15" in val and notice_days <= 15:
            return k
        if ("30 day" in val or "30 days" in val or "1 month" in val or "one month" in val) \
                and notice_days <= 30:
            return k
        if ("45" in val or "60 day" in val or "2 month" in val or "two month" in val) \
                and notice_days <= 60:
            return k
        if ("90" in val or "3 month" in val or "three month" in val) \
                and notice_days <= 90:
            return k
    return next(iter(options))


def _notice_text_answer(qtext: str, notice_days: int) -> str:
    """Answer notice/joining questions in the unit the question asks for."""
    if "month" in qtext:
        return str(max(1, round(notice_days / 30)))
    if "week" in qtext:
        return str(max(1, round(notice_days / 7)))
    return str(notice_days)  # default / explicit "days"


# ---------------------------------------------------------------------------
# Question classifiers
# ---------------------------------------------------------------------------

def _word_match(text: str, phrase: str) -> bool:
    """True when phrase appears in text with word boundaries, so short terms
    like 'ai'/'ml' don't hit inside words like 'email', 'main', or 'html'."""
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text))


def _is_ai_related(qtext: str) -> bool:
    return any(_word_match(qtext, term) for term in config.AI_EXPERIENCE_TERMS)


def _is_joining_question(qtext: str) -> bool:
    """'notice period' is always a joining-time question. For join/onboard
    wording, also require a time-ish word so 'Why do you want to join us?'
    is NOT matched."""
    if "notice" in qtext:
        return True
    join_words = ("join", "onboard", "start date", "date of joining")
    time_words = (
        "how soon", "when", "day", "week", "month",
        "immediate", "early", "soon", "time",
    )
    return any(j in qtext for j in join_words) and any(t in qtext for t in time_words)


def _is_plain_interview_question(qtext: str) -> bool:
    """Interview availability questions that are NOT face-to-face and NOT
    about previous interviews with the company."""
    if "interview" not in qtext:
        return False
    if any(h in qtext for h in config.F2F_INTERVIEW_HINTS):
        return False
    return not any(h in qtext for h in _PREVIOUS_INTERVIEW_HINTS)


def _is_domain_experience_question(qtext: str) -> bool:
    if not any(h in qtext for h in config.DOMAIN_EXPERIENCE_HINTS):
        return False

    # "Have you worked at this company / applied here before?" style
    # questions must stay in the always-No bucket even when they mention
    # a domain word.
    if any(_word_match(qtext, w) for w in _COMPANY_HISTORY_WORDS) and any(
        _word_match(qtext, a) for a in _COMPANY_HISTORY_ACTIONS
    ):
        return False

    return any(
        h in qtext
        for h in (
            "have you", "do you", "are you", "worked", "experience",
            "exposure", "familiar", "knowledge", "handled", "built",
            "developed", "client", "project",
        )
    )


# ---------------------------------------------------------------------------
# Answer builder
# ---------------------------------------------------------------------------

def _answer_one(q: dict, profile: dict, profile_skills: list[str]):
    qtext = (q.get("questionName") or "").lower()
    qtype = (q.get("questionType") or "").lower()
    options = q.get("answerOption") or {}
    question_name = q.get("questionName") or ""

    # Hard rules that apply regardless of question type:
    #   - "worked here before / applied earlier" style → always No
    #   - masters / postgraduation → always No (candidate has none)
    #   - relocation / domain experience / plain interview → always Yes
    is_no_question = any(h in qtext for h in config.NO_QUESTION_HINTS)
    is_masters_q = any(h in qtext for h in _MASTERS_HINTS)
    is_domain_experience = _is_domain_experience_question(qtext)
    force_no = (is_no_question and not is_domain_experience) or is_masters_q
    is_gender_q = "gender" in qtext
    force_yes = (
        is_domain_experience
        or any(h in qtext for h in config.RELOCATION_HINTS)
        or _is_plain_interview_question(qtext)
    )

    if qtype == "text box":
        if force_no:
            return "No"
        if is_gender_q:
            return profile.get("gender", "Male")
        if force_yes:
            return "Yes"
        if "linkedin" in qtext:
            return profile.get("linkedin_url", "")
        if "github" in qtext or "git hub" in qtext:
            return profile.get("github_url", "")
        if "phone" in qtext or "mobile" in qtext or "contact number" in qtext:
            return profile.get("phone", "")
        if ("current company" in qtext or "current employer" in qtext
                or "present company" in qtext or "present employer" in qtext):
            return profile.get("current_company", "")
        if ("current location" in qtext or "current city" in qtext
                or "where are you" in qtext or "based out of" in qtext
                or "residing" in qtext
                or ("location" in qtext and "preferred" not in qtext)):
            return profile.get("current_location", "")
        if "current ctc" in qtext:
            return profile["current_ctc"]
        if "expected ctc" in qtext:
            return profile["expected_ctc"]
        if "experience" in qtext:
            # AI-related experience → exp_ai; everything else → exp_total.
            return profile["exp_ai"] if _is_ai_related(qtext) else profile["exp_total"]
        if _is_joining_question(qtext):
            # Notice period / joining time, in the unit asked.
            return _notice_text_answer(qtext, profile["notice_days"])
        # Not covered by fixed rules — let the AI answer it.
        return ai_text_answer(question_name) or config.DEFAULT_TEXTBOX_ANSWER

    if options:
        if force_no:
            key = _pick_no(options)
        elif is_gender_q:
            key = _pick_gender(options, profile.get("gender", "Male"))
        elif (over_5_key := _pick_over_5_years(options)) is not None:
            key = over_5_key
        elif force_yes:
            key = _pick_yes(options)
        elif _is_joining_question(qtext):
            key = _pick_notice(options, profile["notice_days"])
        else:
            # Everything else: the LLM decides which option maximizes
            # screening success. Only if the AI is off/fails do we fall
            # back to heuristics.
            key = ai_option_answer(question_name, options)
            if key is None:
                if any(skill in qtext for skill in profile_skills):
                    key = _pick_yes(options)
                elif any(x in qtext for x in config.YES_QUESTION_HINTS):
                    key = _pick_yes(options)
                else:
                    key = next(iter(options))
        # Option-type answers must always be wrapped in a list.
        return [key]

    # Non-textbox question without options.
    if force_no:
        return "No"
    if is_gender_q:
        return profile.get("gender", "Male")
    if force_yes:
        return "Yes"
    return ai_text_answer(question_name) or config.DEFAULT_TEXTBOX_ANSWER


def build_answers(questionnaire: list, profile: dict) -> dict:
    """Map every questionId to its answer using rules + AI fallback."""
    profile_skills = [skill.lower() for skill in profile.get("skills", [])]
    return {q["questionId"]: _answer_one(q, profile, profile_skills) for q in questionnaire}


# ---------------------------------------------------------------------------
# Records for the applied-jobs CSV
# ---------------------------------------------------------------------------

def _format_answer(question: dict, answer):
    options = question.get("answerOption") or {}
    if isinstance(answer, list):
        return [options.get(str(item), str(item)) for item in answer]
    return options.get(str(answer), answer)


def build_records(questionnaire: list, answers: dict) -> list[dict]:
    """Human-readable question/answer records, stored alongside the apply."""
    return [
        {
            "question_id": q.get("questionId"),
            "question": q.get("questionName") or "",
            "answer": _format_answer(q, answers.get(q.get("questionId"))),
            "raw_answer": answers.get(q.get("questionId")),
        }
        for q in questionnaire
    ]
