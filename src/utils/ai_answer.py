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
import re

from src.config import agent_config as config
from src.utils import llm

logger = logging.getLogger(__name__)

# Hard ceiling on a submitted answer, and the point past which a truncated
# answer is still long enough to stand on its own. Naukri accepts long text,
# but anything past a couple of sentences reads as padding.
_MAX_ANSWER_CHARS = 500
_MIN_ANSWER_CHARS = 80

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
CALLER = "Claude questionnaire answer"

# Answers are cached per run: the same question often repeats across jobs.
_answer_cache: dict[str, str] = {}


def ai_enabled() -> bool:
    """True when the AI fallback is configured, keyed, and still healthy."""
    if not config.USE_AI_ANSWERS:
        return False
    if llm.tripped(CALLER):
        return False
    return bool(llm.api_key("ANTHROPIC_API_KEY"))


# Human-readable labels and disambiguating notes for profile keys. This is
# presentation only: a key with no entry here is still sent, using its own
# name. Adding a field to candidate_profile.json therefore needs NO code
# change — it reaches the model automatically on the next run.
_FIELD_NOTES = {
    "current_ctc": ("Current CTC", "in INR per year, i.e. rupees not lakhs"),
    "current_ctc_fixed": (
        "Current FIXED CTC",
        "in INR per year; the entire current CTC is fixed pay",
    ),
    "current_ctc_variable": (
        "Current VARIABLE CTC / bonus",
        "in INR per year; there is NO variable component at all, it is zero",
    ),
    "expected_ctc": ("Expected CTC", "in INR per year, i.e. rupees not lakhs"),
    "exp_total": ("Total professional experience", "in years"),
    "exp_ai": ("AI / ML / GenAI / LLM / RAG experience", "in years"),
    "notice_days": (
        "Remaining notice period",
        "days until LWD, calculated from the current date",
    ),
    "last_working_day": ("Last working day (LWD)", ""),
    "available_to_join_from": ("Available to join a new job from", ""),
    "team_mentored": (
        "People mentored, guided, led or coached",
        "use this number for any 'how many people have you "
        "mentored/led/guided/coached' or 'team size' question; it is "
        "technical leadership, not formal line management, but it is still "
        "the correct answer to all of those",
    ),
    "email": ("Email", "use for everything except TCS"),
    "tcs_registration_email": ("TCS registration email", "TCS only"),
    "tcs_ep_number": ("TCS registration / EP number", "TCS only"),
    "highest_qualification": (
        "Highest qualification",
        "this is the highest: NO Masters, NO postgraduation, NO PhD",
    ),
    "graduation_year": ("Bachelor's graduation / passing year", ""),
    "location_preference": ("Preferred work locations", "in priority order"),
    "pan_number": ("PAN number", ""),
    "date_of_birth": ("Date of birth", ""),
    "current_company": ("Current company / employer", ""),
    "current_location": ("Current location / city", ""),
    "linkedin_url": ("LinkedIn URL", ""),
    "github_url": ("GitHub URL", ""),
    "full_name": ("Full name", ""),
    "phone": ("Phone number", ""),
}


def _format_value(value) -> str:
    """Render a JSON value for the prompt without losing its ordering."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items())
    return str(value)


def _profile_context() -> str:
    """Serialize the WHOLE candidate profile for the model.

    Every key in candidate_profile.json is emitted, so the model can answer
    questions this code has never anticipated. Previously each field needed
    its own hand-written line here and its own matching rule in
    questionnaire.py, which meant an unlisted field was invisible to the AI.
    """
    lines = []
    for key, value in config.QUESTIONNAIRE_PROFILE.items():
        rendered = _format_value(value).strip()
        if not rendered:
            continue  # an empty field is noise, and invites invention
        label, note = _FIELD_NOTES.get(key, (key.replace("_", " "), ""))
        lines.append(f"- {label}: {rendered}" + (f" ({note})" if note else ""))

    extra = (config.AI_ANSWER_CONTEXT or "").strip()
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
        "- Answer ONLY what is asked, and answer ALL of what is asked. No "
        "extra facts, no context, no explanations, no contact details, and "
        "no offers to send anything later, unless the question explicitly "
        "asks for them. But if the question asks for two or more things "
        "(e.g. 'city and PIN code'), give every one of them.\n"
        "- Do not restate the question or label a single-value answer. If "
        "the question already specifies a unit, or says numeric/number/digits "
        "only, give the bare value. If a free-text quantity question gives "
        "no unit, include a compact unit: '40.5 LPA', '55 LPA', '16 days', "
        "or '6 years'.\n"
        "- Keep answers short. For factual questions: a number or a few "
        "words. For descriptive 'how/why/describe' questions: 1-2 complete "
        "sentences maximum, never cut off mid-sentence.\n"
        "- Write like a working engineer, not like marketing copy. Plain, "
        "concrete, first-person. Name the specific thing built and the "
        "specific problem it solved. Avoid stacked buzzwords and filler "
        "such as 'cutting-edge', 'end-to-end', 'robust', 'seamless', "
        "'leveraging', 'passionate', 'production-grade', 'best practices', "
        "'innovative solutions', or listing six technologies in one "
        "sentence. A recruiter should read it as someone who did the work, "
        "not someone describing the field.\n"
        "- The profile above IS the candidate's own data: every value in "
        "it is known and must be stated verbatim when asked. Never refer "
        "to the profile, provided information, missing data, "
        "or unavailable information in an answer. Never say 'I don't have "
        "this in the provided profile' or any variation of it. Give only a "
        "direct, recruiter-ready answer.\n"
        "- NEVER refuse, hedge, or defer. Never write 'I don't have', 'I do "
        "not have', 'not available', 'not readily available', 'upon "
        "request', 'during onboarding', 'at the verification stage', 'I "
        "will share it later', or any variation of them. A recruiter reads "
        "any of those as a red flag.\n"
        "- If the profile does not contain the exact value asked for, do NOT "
        "refuse and do NOT answer 'N/A'. COMMIT to the most plausible, "
        "favorable answer a strong candidate with this profile would give, "
        "and state it as fact. Estimate confidently when you must: a "
        "specific realistic number always beats an admission of not "
        "knowing.\n"
        "- If the question asks whether the candidate CAN or WILL supply "
        "something (a document, a screenshot, a certificate, a payslip, an "
        "ID, a score), the answer is simply 'Yes'. Never turn it into an "
        "explanation of when or how it will be supplied.\n"
        "- Reply with exactly 'N/A' ONLY when the value logically cannot "
        "exist for this candidate — for example an employee code at a "
        "company they never worked for. Never use N/A merely because the "
        "profile omits it.\n"

        "- ANSWER IN THE UNIT AND FORMAT THE QUESTION ASKS FOR. The profile "
        "stores CTC in rupees per year, but if the question says 'in Lacs' / "
        "'in LPA' / 'in lakhs', convert it (4050000 rupees = 40.5 lacs). If "
        "it asks in months, give months; in days, give days. If it asks for "
        "a date, give a date. Answering 4050000 to 'current CTC in Lacs' "
        "gets the application filtered out automatically.\n"
        "- NEVER answer a quantity with a range, an approximation, or a "
        "conditional. Not '4-6 years', not '10,000 to 50,000', not "
        "'thousands to millions', not 'depending on the project'. Pick ONE "
        "specific number and state it alone.\n"
        "- CALIBRATE every number to what this candidate could actually have "
        "done in the years shown above, at a services/consulting employer. "
        "An inflated or suspiciously round figure gets the candidate "
        "rejected faster than a modest one. Prefer specific, uneven, "
        "human-sounding numbers (e.g. 12, 40, 850, 3.5 lakh) over round "
        "marketing figures (10, 100, 1 million, 10 million). Never claim "
        "hyperscale volumes, revenue impact, or headcount that a hands-on "
        "engineer at this level would not own.\n"
        "- NEVER split, apportion, or otherwise break down a value the "
        "profile gives as a single figure. If asked for a fixed/variable "
        "CTC breakup, use the fixed and variable fields above exactly as "
        "given — do not assume a typical 80/20 or 90/10 split, and do not "
        "invent a bonus, retention pay, or joining bonus that is not listed. "
        "A salary breakup that contradicts the payslip is caught at "
        "verification.\n"
        "- Never invent employer-confidential specifics you would not be "
        "asked to disclose: no client names, no internal project codenames, "
        "no revenue figures, no colleague or HR contact details. Do not "
        "invent a client industry or domain that the profile does not "
        "state; describe the technical work instead.\n"
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
        "- For joining availability, use the exact available-to-join date "
        "from the profile. Do not claim immediate availability before that "
        "date. Continue answering 'Yes' to work from office/hybrid, shift, "
        "and travel willingness questions.\n"
        "- Always answer 'Yes' to domain, industry, sector, or vertical "
        "experience questions, including banking, medtech, healthcare, "
        "finance, fintech, insurance, retail, telecom, manufacturing, or "
        "any other named business domain.\n"
        "- The candidate has NO Masters degree, NO postgraduation, and NO "
        "PhD — answer 'No' to any question about those. Highest "
        "qualification is a Bachelor's degree.\n"
        "- For LinkedIn/GitHub/portfolio URL, phone number, or current "
        "location questions, reply with the exact value from the profile.\n"
        "- For date of birth/DOB or PAN questions, reply with the exact "
        "value from the profile.\n"
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
        "- For numeric questions (years, CTC, notice period), convert to the "
        "unit the question asks for. Use a bare number only when that unit is "
        "already stated or numeric-only input is requested; otherwise add "
        "the compact unit. Remaining notice period is given in DAYS: if asked "
        "in months, convert (30 days = 1 month); if asked in weeks, convert "
        "(30 days = 4 weeks). For last working day/LWD questions, reply with the exact "
        "last working day from the profile. For when-can-you-join, joining "
        "date, availability, or start-date questions, reply with the exact "
        "available-to-join date from the profile. Keep those two dates "
        "distinct.\n"
        "- Never explain your answer. Never add extra sentences."
    )


def _text_blocks(data: dict) -> str:
    """Join the text blocks of an Anthropic messages response."""
    return "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ).strip()


def _call_claude(user_prompt: str) -> str | None:
    """Single message call to the Anthropic API. Returns text or None."""
    return llm.call(
        CALLER,
        ANTHROPIC_API_URL,
        {
            "x-api-key": llm.api_key("ANTHROPIC_API_KEY"),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        {
            "model": config.AI_MODEL,
            "max_tokens": config.AI_MAX_TOKENS,
            "system": _system_prompt(),
            "messages": [{"role": "user", "content": user_prompt}],
        },
        _text_blocks,
    )


def _clean_text_answer(text: str) -> str:
    """Collapse to one line, strip wrapping quotes, and cap length cleanly.

    A hard slice at 500 chars leaves a sentence cut off mid-word ("...RAGAS
    for retrieval quality me"), which on a real application looks worse than
    a short answer. Trim back to the last completed sentence instead, and
    only fall back to a word boundary if there is no sentence end to use.
    """
    answer = " ".join(text.split()).strip().strip('"').strip("'").strip()
    if len(answer) <= _MAX_ANSWER_CHARS:
        return answer

    clipped = answer[:_MAX_ANSWER_CHARS]
    # Prefer ending on a completed sentence.
    sentence_end = max(clipped.rfind(". "), clipped.rfind("! "),
                       clipped.rfind("? "))
    if sentence_end >= _MIN_ANSWER_CHARS:
        return clipped[:sentence_end + 1]
    # Otherwise end on a whole word rather than mid-token.
    word_end = clipped.rfind(" ")
    if word_end >= _MIN_ANSWER_CHARS:
        return clipped[:word_end].rstrip(",;:-") + "."
    return clipped


# An evasive answer is not a fixed phrase to blacklist — it is a shape.
# Every one of them TALKS ABOUT the answer instead of BEING it, and they are
# all built from the same three constructions:
#
#   1. negated possession/provision  "I don't have it", "cannot furnish it"
#   2. provision promised for later  "I'll share it", "can provide it"
#   3. provision tied to a process   "upon request", "during onboarding"
#
# Matching the grammar rather than the wording means a rewording the model has
# never produced before is still caught, and the lists below stay short enough
# to reason about. A blacklist of observed sentences would need a new entry
# every time the model picks a different synonym.

# Verbs a candidate uses when talking about handing a value over, rather than
# just stating it. Written as regex stems so tense/inflection is covered.
_PROVIDE_VERBS = r"(?:hav|provid|shar|furnish|suppl|disclos|giv|send|upload|submit|offer|produc|present|quot|recall|remember|access|locat|find|confirm)"

# Ways English negates the verbs above.
_NEGATORS = r"(?:not|n't|never|unable\s+to|cannot|can't|can\s+not|don't|do\s+not|doesn't|does\s+not|didn't|did\s+not|without|lack(?:ing|s)?\s+(?:of\s+)?|no\b)"

# Modals that turn a statement into a promise about the future.
_FUTURE_MODALS = r"(?:will|shall|would|can|could|may|might|i'll|i'd|happy\s+to|glad\s+to|willing\s+to|able\s+to|ready\s+to|prepared\s+to)"

# Things you DO, not values you hand over. "I can share my screen" is a
# cooperative offer; "I can share my PAN later" is a deferral. Only the object
# of the verb tells the two apart.
_ACTIVITY_NOUNS = (
    r"(?:screen|thoughts|views|opinion|perspective|experience|story|journey|"
    r"knowledge|insights|approach|understanding|time|availability|feedback)\b"
)

_EVASION_PATTERNS = (
    # 1. Negated possession/provision:  "I do not have ...", "cannot share ..."
    #    The negator must sit within a few words of the verb so an unrelated
    #    "no" elsewhere in a real answer cannot trip it.
    rf"\b{_NEGATORS}\s+(?:\w+\s+){{0,3}}{_PROVIDE_VERBS}\w*\b",
    # 2. Predicate form of the same idea:  "... is not available/provided".
    rf"\b(?:is|are|was|were|it's|its)?\s*{_NEGATORS}\s+(?:\w+\s+){{0,2}}"
    rf"(?:available|provided|specified|mentioned|listed|included|handy|"
    rf"accessible|applicable|on\s+file|with\s+me|at\s+hand)\b",
    # 3. Provision promised for later:  "I will share it", "can supply the
    #    certificate". The verb keeps its own inflection (\w*), since stems
    #    like "suppl" become "supply"/"supplied"/"supplying".
    #    ``(?!{_ACTIVITY_NOUNS})`` keeps offers to DO something ("I can share
    #    my screen during the call") out of it — evasion always defers a
    #    value or a document, never an activity.
    rf"\b{_FUTURE_MODALS}\s+(?:\w+\s+){{0,2}}{_PROVIDE_VERBS}\w*\s+"
    rf"(?:it|this|that|them|these|those|same\b|"
    rf"(?:the|my)\s+(?!{_ACTIVITY_NOUNS})\w+)",
    # 3b. Admitting the value is absent without naming a verb at all:
    #     "I lack the paperwork", "I have no data on that".
    #     The noun list matters: "no data/record/idea" denies the ANSWER,
    #     whereas "no objection/gaps/issues" is a positive statement and must
    #     stay allowed, so this cannot be a bare "no <anything>".
    r"\bi\s+lack\b",
    r"\bno\s+(?:access|record|copy|note|recollection|way\s+to|data|"
    r"information|metrics|visibility|idea|clue|details|figures?|stats|"
    r"numbers|documentation|proof|evidence)\b",
    # 4. Provision tied to a request, a stage, or a process. This is the
    #    "upon request" / "during onboarding" / "at a later stage" family,
    #    generalized to preposition + (any words) + process noun.
    r"\b(?:up)?on\s+(?:\w+\s+){0,2}(?:request|asking|demand|requirement|"
    r"finali[sz]ation|confirmation|selection|joining|shortlisting)\b",
    # 4b. Deferral conditioned on a future event rather than a named stage:
    #     "once shortlisted", "if selected", "when required".
    r"\b(?:once|if|when|after)\s+(?:i\s+am\s+|i'm\s+|we\s+are\s+)?"
    r"(?:short\s*listed|selected|hired|offered|onboarded|required|needed|"
    r"requested|asked)\b",
    r"\b(?:during|after|post|at|before|prior\s+to|closer\s+to|by)\s+"
    r"(?:the\s+|a\s+)?(?:\w+\s+){0,2}(?:onboarding|joining|verification|"
    r"screening|interview|offer|documentation|formalities|induction|bgv|"
    r"background\s+check|later\s+stage|next\s+stage|next\s+round|"
    r"appropriate\s+time|right\s+time)\b",
    r"\b(?:at|in)\s+(?:a\s+|the\s+)?(?:later|appropriate|relevant|required)\s+"
    r"(?:stage|time|point|juncture|moment)\b",
    # 5. Explicitly deferring to a channel instead of answering here.
    r"\b(?:please\s+)?(?:contact|reach\s+out\s+to|email|call|revert\s+to)\s+me\b",
)

_EVASION_RE = re.compile("|".join(_EVASION_PATTERNS), re.IGNORECASE)

# A short, explicit non-answer is exactly what the prompt asks for when a
# value genuinely does not apply, so it must survive the screen above. These
# are compared against the WHOLE answer, never as substrings, because "no" or
# "none" inside a longer sentence is usually part of an evasion.
_ALLOWED_NON_ANSWERS = frozenset(
    ("n/a", "na", "n.a.", "not applicable", "none", "nil", "no", "yes")
)


def _mentions_missing_profile_data(answer: str) -> bool:
    """True when the answer describes the answer instead of giving it.

    Catches refusals ("I don't have my PAN"), promises ("I'll share it
    later"), and process deferrals ("upon request during onboarding") by
    their grammar, so unseen rewordings are caught too.
    """
    value = " ".join(answer.lower().split()).strip(" .!")
    if value in _ALLOWED_NON_ANSWERS:
        return False
    return bool(_EVASION_RE.search(value))


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

    if _mentions_missing_profile_data(answer):
        # Don't just drop it: a discarded answer falls back to "N/A", which
        # reads as a refusal on a willingness question and as inexperience on
        # a quantity question. Re-ask once, naming the failure, so the model
        # commits to a real answer instead.
        logger.warning(
            "Evasive AI answer for %r: %r — retrying", question_text, answer
        )
        retry = _call_claude(
            f"{prompt}\n\n"
            f"Your previous reply was rejected because it refused, hedged, "
            f"or deferred: {answer!r}\n"
            "Answer it properly this time. If the question asks whether you "
            "can supply or share something, reply exactly 'Yes'. If it asks "
            "for a quantity, reply with ONE specific realistic number and "
            "nothing else — never 'not applicable', never a range. "
            "Otherwise commit to the most plausible, favorable specific "
            "value a strong candidate would give, stated as fact. Do not "
            "explain, do not qualify, and do not mention what you do or do "
            "not have."
        )
        answer = _clean_text_answer(retry) if retry else ""
        if not answer or _mentions_missing_profile_data(answer):
            logger.warning(
                "Discarding still-evasive AI answer for %r: %r",
                question_text, answer,
            )
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




