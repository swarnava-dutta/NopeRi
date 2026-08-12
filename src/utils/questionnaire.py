"""Rule-based questionnaire answering with an AI fallback.

Fixed rules answer the common screening questions (CTC, notice period,
relocation, gender, domain experience, ...). Anything not covered is passed
to the Claude fallback in ``ai_answer``; if that is disabled or fails, the
static defaults from ``agent_config`` apply.

All hint lists (NO_QUESTION_HINTS, RELOCATION_HINTS, ...) live in
``agent_config.py`` so behaviour can be tuned without touching this module.
"""

import re
from decimal import Decimal, InvalidOperation

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


def _notice_option_upper_days(label: str) -> float | None:
    """Convert an option label into its upper duration bound in days."""
    value = " ".join(str(label).lower().split())
    if "immediate" in value:
        return 0
    if re.search(r"(?:more than|above|over|greater than|>)\s*\d", value):
        return float("inf")

    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", value)]
    if not numbers:
        number_words = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6,
        }
        numbers = [float(number_words[word]) for word in value.split()
                   if word in number_words]
    if not numbers:
        return None

    upper = max(numbers)
    if "month" in value:
        return upper * 30
    if "week" in value:
        return upper * 7
    # A bare numeric label on a notice-period option means days.
    return upper


def _pick_notice(options: dict, notice_days: int) -> str:
    """Pick the smallest duration bucket that still contains notice_days."""
    ranked = []
    serving_key = None
    not_serving_key = None
    for index, (key, label) in enumerate(options.items()):
        normalized = " ".join(str(label).lower().split())
        if "serving notice" in normalized:
            if re.search(r"(?<![a-z])not(?![a-z])", normalized):
                not_serving_key = not_serving_key or key
            else:
                serving_key = serving_key or key
        upper_days = _notice_option_upper_days(label)
        if upper_days is not None:
            ranked.append((upper_days, index, key))

    notice_days = max(int(notice_days), 0)
    if not ranked:
        if notice_days > 0 and serving_key is not None:
            return serving_key
        if notice_days == 0 and not_serving_key is not None:
            return not_serving_key
        return next(iter(options))

    fitting = [item for item in ranked if item[0] >= notice_days]
    if fitting:
        return min(fitting, key=lambda item: (item[0], item[1]))[2]
    if notice_days > 0 and serving_key is not None:
        return serving_key
    return max(ranked, key=lambda item: (item[0], -item[1]))[2]


_NUMERIC_ONLY_HINTS = (
    "numeric input only", "numeric only", "number only", "numbers only",
    "digits only", "only numeric", "only numbers", "only digits",
)


def _requires_bare_number(qtext: str) -> bool:
    return any(hint in qtext for hint in _NUMERIC_ONLY_HINTS)


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _ctc_text_answer(qtext: str, annual_inr) -> str:
    """Render annual INR in the unit requested, with LPA for a unitless box."""
    try:
        raw_inr = Decimal(str(annual_inr).replace(",", "").strip())
    except InvalidOperation:
        return str(annual_inr or "")

    # Naukri's explicit numeric-only CTC boxes use raw annual INR.
    if _requires_bare_number(qtext) or any(
        unit in qtext for unit in ("inr", "rupee", "₹")
    ):
        return _decimal_text(raw_inr)

    lpa = _decimal_text(raw_inr / Decimal("100000"))
    if any(unit in qtext for unit in ("lpa", "lac", "lakh")):
        return lpa
    return f"{lpa} LPA"


def _notice_text_answer(qtext: str, notice_days: int) -> str:
    """Answer notice/joining questions in the requested or clearest unit."""
    notice_days = max(int(notice_days), 0)
    if "month" in qtext:
        value = 0 if notice_days == 0 else max(1, round(notice_days / 30))
        unit = "month" if value == 1 else "months"
    elif "week" in qtext:
        value = 0 if notice_days == 0 else max(1, round(notice_days / 7))
        unit = "week" if value == 1 else "weeks"
    else:
        value = notice_days
        unit = "day" if value == 1 else "days"

    unit_already_given = bool(
        re.search(r"(?<![a-z])(days?|weeks?|months?)(?![a-z])", qtext)
    )
    if _requires_bare_number(qtext) or unit_already_given:
        return str(value)
    return f"{value} {unit}"


# ---------------------------------------------------------------------------
# Question classifiers
# ---------------------------------------------------------------------------

def _word_match(text: str, phrase: str) -> bool:
    """True when phrase appears in text with word boundaries, so short terms
    like 'ai'/'ml' don't hit inside words like 'email', 'main', or 'html'."""
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text))


def _is_ai_related(qtext: str) -> bool:
    return any(_word_match(qtext, term) for term in config.AI_EXPERIENCE_TERMS)


def _is_lwd_question(qtext: str) -> bool:
    """Questions asking for the candidate's exact last working day."""
    return "last working day" in qtext or _word_match(qtext, "lwd")


def _is_joining_date_question(qtext: str) -> bool:
    """Questions asking when the candidate can actually start a new role."""
    date_phrases = (
        "available to join", "availability to join", "when can you join",
        "when would you join", "how soon can you join", "joining date",
        "date of joining", "earliest joining",
        "available to start", "when can you start", "start date",
    )
    return any(phrase in qtext for phrase in date_phrases)


def _is_date_of_birth_question(qtext: str) -> bool:
    return (
        "date of birth" in qtext
        or "birth date" in qtext
        or _word_match(qtext, "dob")
    )


def _is_pan_question(qtext: str) -> bool:
    return (
        "pan card" in qtext
        or "pan number" in qtext
        or "pan no" in qtext
        or "permanent account number" in qtext
        or _word_match(qtext, "pan")
    )


def _is_tcs_email_question(qtext: str) -> bool:
    """TCS asks for the email a candidate registered on its own portal with,
    which is deliberately different from the default contact email."""
    return "tcs" in qtext and ("email" in qtext or "mail id" in qtext)


def _is_tcs_ep_question(qtext: str) -> bool:
    """TCS registration / EP reference number.

    'ep' is matched with word boundaries and only next to an identifier word,
    so 'prep', 'deployment', or a stray 'no' elsewhere in the sentence cannot
    turn an unrelated question into an EP-number answer.
    """
    if re.search(r"(?<![a-z0-9])ep\s*(number|no\b|id|ref|reference)", qtext):
        return True
    return "tcs" in qtext and (
        "registration no" in qtext
        or "registration number" in qtext
        or "reference number" in qtext
    )


def _name_answer(qtext: str, profile: dict) -> str | None:
    """First / last / full name, split from the profile's full_name."""
    if "name" not in qtext:
        return None
    # "Company name", "College name", "Reference name" etc. are not the
    # candidate's own name.
    if any(w in qtext for w in ("company", "employer", "college", "school",
                                "university", "institute", "manager",
                                "reference", "referral", "project", "client",
                                "relative", "friend", "spouse", "father",
                                "mother", "recruiter")):
        return None
    parts = (profile.get("full_name") or "").split()
    if not parts:
        return None
    if "first name" in qtext or "given name" in qtext:
        return parts[0]
    if "last name" in qtext or "surname" in qtext or "family name" in qtext:
        return parts[-1] if len(parts) > 1 else parts[0]
    if ("full name" in qtext or "your name" in qtext
            or "candidate name" in qtext or qtext.strip(" :?*") == "name"):
        return profile.get("full_name")
    return None


def _is_compound_question(qtext: str) -> bool:
    """Two or more different facts asked in one box.

    e.g. "Date of Birth and Highest education:" — the first matching fixed
    rule would answer only half of it, so these go to the AI, which can put
    both values in one line.
    """
    separators = (" and ", " & ", ",", "/", ";", "+")
    if not any(sep in qtext for sep in separators):
        return False
    topics = (
        "date of birth", "dob", "pan", "aadhaar", "aadhar", "education",
        "qualification", "graduation", "ctc", "notice", "location",
        "experience", "email", "phone", "mobile", "company", "linkedin",
        "last working day", "lwd", "pin code", "address", "gender",
    )
    return sum(1 for t in topics if t in qtext) >= 2


def _is_team_size_question(qtext: str) -> bool:
    """'How many people have you mentored / led / managed?'

    Answered from the profile so the number is consistent across every
    application, instead of the LLM picking a different headcount each time.
    """
    # "How many YEARS have you led AI projects?" is a duration despite the
    # leading verb, so a time unit always wins over the headcount reading.
    if re.search(r"\b(years?|yrs?|months?|experience)\b", qtext):
        return False
    if not re.search(
        r"\b(mentor|manag|led|lead|manage|supervis|guid|coach|report)\w*\b",
        qtext,
    ):
        return False
    return bool(
        re.search(
            r"\b(how\s+many|number\s+of|team\s+size|size\s+of\s+(?:your|the)\s+team|"
            r"headcount|people|members|engineers|developers|juniors|resources)\b",
            qtext,
        )
    )


def _is_willingness_to_supply_question(qtext: str) -> bool:
    """'Can you share/provide/upload X?' — a cooperation check, not a request
    for the value itself.

    The only answer that helps at screening is 'Yes'. Left to the LLM these
    became apologies ("I don't have my CIBIL screenshot readily available"),
    which read as a refusal. Questions that ask for the value outright
    ("Share your PAN") are NOT matched here — those fall through to the
    profile rules below, which know the actual value.
    """
    # "Any objection to sharing X?" asks the same thing with inverted
    # polarity, where the helpful answer is "No", not "Yes". Leave those to
    # the NO_QUESTION_HINTS / AI path rather than answering them backwards.
    if re.search(r"\b(objection|problem|issue|hesitat|reluctan|refus)", qtext):
        return False
    asks_capability = bool(
        re.search(
            r"\b(can|could|are\s+you\s+(?:able|willing|ok|comfortable)|"
            r"would\s+you|will\s+you|do\s+you\s+agree)\b",
            qtext,
        )
    )
    if not asks_capability:
        return False
    return bool(
        re.search(
            r"\b(shar|provid|send|upload|submit|furnish|attach|produc|"
            r"present|arrang|bring|carry|show)\w*\b",
            qtext,
        )
    )


def _is_years_question(qtext: str) -> bool:
    """Experience questions, including the ones that never say 'experience'.

    Recruiters write 'How many years of Kubernetes?' or 'Total yrs in Java'
    just as often as the full phrasing, and those used to miss the fixed rule
    and reach the LLM, which then invented a number.
    """
    if "experience" in qtext or "exp." in qtext or _word_match(qtext, "exp"):
        return True
    return bool(re.search(r"(years|yrs)\b", qtext)) and any(
        h in qtext for h in ("how many", "total", "number of", "no of", "no. of")
    )


def _is_email_question(qtext: str) -> bool:
    return (
        "email" in qtext
        or "e-mail" in qtext
        or "mail id" in qtext
        or "mail address" in qtext
    )


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


def _is_notice_duration_question(qtext: str) -> bool:
    """Notice duration, excluding yes/no questions about notice status."""
    if "notice" not in qtext:
        return False

    asks_for_duration = any(
        phrase in qtext
        for phrase in (
            "notice period", "how long", "how many day", "how many week",
            "how many month", "days notice", "day notice", "weeks notice",
            "week notice", "months notice", "month notice",
        )
    )
    if not asks_for_duration:
        return False

    status_only = any(
        phrase in qtext
        for phrase in (
            "serving notice", "serve notice", "on notice", "under notice",
            "notice status",
        )
    )
    duration_request = any(
        phrase in qtext
        for phrase in ("what is", "how long", "how many", "mention", "state")
    )
    return not status_only or duration_request


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

_NUMERIC_QUESTION_HINTS = (
    "how many", "years", "yrs", "ctc", "salary", "lpa", "lacs", "lakh",
    "notice", "how much", "percentage", "%", "marks", "cgpa", "gpa",
)

# Quantities where "1" would understate the candidate badly. A GenAI engineer
# answering "1" to "how many tokens per month" or "how many users did it
# serve" screens out instantly, so an unanswered volume question gets a
# credible production-scale figure instead of a literal one.
_SCALE_QUESTION_HINTS = (
    "token", "user", "request", "query", "queries", "document", "record",
    "transaction", "call", "hit", "row", "customer", "volume", "throughput",
    "per month", "per day", "monthly", "daily",
)


def _fallback_text_answer(qtext: str) -> str:
    """Free-text answer used when no rule matched and the AI gave nothing.

    Never blank and never a refusal: the answer still has to help the
    candidate. "1" only reads sensibly on a small-count question; on a
    volume question it understates, and on "Employee code" it is noise.
    """
    if any(h in qtext for h in _SCALE_QUESTION_HINTS) and any(
        h in qtext for h in ("how many", "how much", "number of", "volume")
    ):
        return config.SCALE_TEXTBOX_ANSWER
    if any(h in qtext for h in _NUMERIC_QUESTION_HINTS):
        return config.DEFAULT_TEXTBOX_ANSWER
    return config.UNKNOWN_TEXT_ANSWER


def _fixed_quantity_answer(qtext: str, profile: dict) -> str | None:
    """Deterministic formatting for salary and remaining notice duration."""
    current_ctc = "current ctc" in qtext
    expected_ctc = "expected ctc" in qtext
    breakup_question = any(
        hint in qtext
        for hint in ("fixed", "variable", "breakup", "break-up", "component")
    )

    # A combined or breakup question needs all requested facts, so let the AI
    # compose it. Single-value questions should never depend on model wording.
    if current_ctc and not expected_ctc and not breakup_question:
        return _ctc_text_answer(qtext, profile.get("current_ctc", ""))
    if expected_ctc and not current_ctc and not breakup_question:
        return _ctc_text_answer(qtext, profile.get("expected_ctc", ""))

    if _is_notice_duration_question(qtext):
        notice = _notice_text_answer(qtext, profile.get("notice_days", 0))
        if _is_lwd_question(qtext) and profile.get("last_working_day"):
            return f"{notice}; LWD: {profile['last_working_day']}"
        return notice
    return None


def _text_answer(qtext: str, question_name: str, profile: dict,
                  force_no: bool, force_yes: bool, is_gender_q: bool):
    """Answer a free-text question: strategy rules, then the AI, then rules.

    Order matters, and it is deliberately AI-FIRST for anything factual.

    Only the handful of checks above the AI call are strategic — they encode
    what we want said regardless of what is true or what the model thinks
    ("have you worked here before?" is always No). Everything else is a
    question about the candidate, and the model has the entire
    candidate_profile.json in front of it, so it answers those better than a
    keyword rule can: it reads the unit the recruiter asked for ("CTC in
    Lacs" -> 40.5, not 4050000), handles two facts in one box, and copes with
    phrasings nobody anticipated.

    ``_profile_text_answer`` below is the OFFLINE fallback for when the AI is
    disabled, unkeyed, or failing. It is intentionally still complete.
    """
    if force_no:
        return "No"
    if is_gender_q:
        return profile.get("gender", "Male")
    if force_yes:
        return "Yes"

    # Willingness to hand something over is a cooperation question, not a
    # factual one: "Yes" always beats whatever the model reasons its way to.
    if _is_willingness_to_supply_question(qtext) and not _profile_has_value(
        qtext, profile
    ):
        return "Yes"

    fixed_quantity = _fixed_quantity_answer(qtext, profile)
    if fixed_quantity is not None:
        return fixed_quantity

    answer = ai_text_answer(question_name)
    if answer:
        return answer
    return _profile_text_answer(qtext, profile)


def _profile_has_value(qtext: str, profile: dict) -> bool:
    """True when a fixed rule can answer this from the profile.

    Used to keep "Can you share your PAN?" returning the actual PAN rather
    than a bare "Yes".
    """
    return _profile_text_answer(qtext, profile, default=None) is not None


def _profile_text_answer(qtext: str, profile: dict, default: str | None = ""):
    """Deterministic profile lookup — the offline fallback for _text_answer.

    Kept as a straight keyword cascade on purpose: it runs when the AI is
    unavailable, so it must never depend on it. ``default`` is what comes
    back when nothing matches; callers pass None to probe for a match.
    """
    if (name := _name_answer(qtext, profile)) is not None:
        return name
    if "linkedin" in qtext:
        return profile.get("linkedin_url", "")
    if "github" in qtext or "git hub" in qtext:
        return profile.get("github_url", "")
    if "phone" in qtext or "mobile" in qtext or "contact number" in qtext:
        return profile.get("phone", "")
    if _is_date_of_birth_question(qtext):
        return profile.get("date_of_birth", "")
    if _is_pan_question(qtext):
        return profile.get("pan_number", "")
    # TCS-specific identifiers are checked before the generic email rule,
    # since those questions mention "email" too.
    if _is_tcs_ep_question(qtext):
        return profile.get("tcs_ep_number", "")
    if _is_tcs_email_question(qtext):
        return profile.get("tcs_registration_email", "")
    if _is_email_question(qtext):
        return profile.get("email", "")
    if (("current company" in qtext or "current employer" in qtext
            or "present company" in qtext or "present employer" in qtext)):
        return profile.get("current_company", "")
    if ("current location" in qtext or "current city" in qtext
            or "where are you" in qtext or "based out of" in qtext
            or "residing" in qtext
            or ("location" in qtext and "preferred" not in qtext)):
        return profile.get("current_location", "")
    if "current ctc" in qtext:
        return _ctc_text_answer(qtext, profile["current_ctc"])
    if "expected ctc" in qtext:
        return _ctc_text_answer(qtext, profile["expected_ctc"])
    # Checked before the generic years rule: "How many people have you
    # mentored" also contains "how many", but it is a headcount, not a
    # duration.
    if _is_team_size_question(qtext) and profile.get("team_mentored"):
        return profile["team_mentored"]
    if _is_years_question(qtext):
        # AI-related experience → exp_ai; everything else → exp_total.
        return profile["exp_ai"] if _is_ai_related(qtext) else profile["exp_total"]
    if _is_lwd_question(qtext):
        return profile.get("last_working_day", "")
    if _is_joining_date_question(qtext):
        return profile.get("available_to_join_from", "")
    if _is_joining_question(qtext):
        # Notice period / joining time, in the unit asked.
        return _notice_text_answer(qtext, profile["notice_days"])
    # Nothing in the profile matches. Callers probing for a match get None;
    # the normal path gets a safe, non-damaging placeholder.
    if default is None:
        return None
    return _fallback_text_answer(qtext)


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

    # Every question the candidate types into goes through the same profile
    # rules. Naukri labels these "Text Box", but also "date", "Text Area",
    # and occasionally an option-less "List Menu"; those non-"Text Box" types
    # used to skip all the profile rules and go straight to the LLM, which
    # then improvised a date of birth or a PAN it was never meant to invent.
    if qtype == "text box" or not options:
        return _text_answer(
            qtext, question_name, profile, force_no, force_yes, is_gender_q
        )

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
