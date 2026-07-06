"""Thin client over Naukri's internal APIs using an authenticated session.

Handles job search, recommendations, and apply workflows:
- Builds correct headers (authenticated and non-authenticated)
- Generates SEO-style keys for the search endpoint
- Attaches the required signed ``nkparam`` header (403 without it)
- Parses raw API responses into the Job model
- Handles common failure cases (403, 406, 429, malformed JSON)
"""

import logging
import re
from datetime import datetime

from src.models.models import Job
from src.client.naukri_client import NaukriLoginClient
from src.exceptions.exceptions import NaukriAuthError, NaukriParseError
from src.utils.nkparam_generator import generate_nkparam
from src.config.constants import RECOMMENDED_JOBS_URL, JOB_SEARCH_URL, APPLY_JOB_URL
from src.config import agent_config as config
from src.utils.ai_answer import ai_text_answer, ai_option_answer
from src.utils import humanizer

logger = logging.getLogger(__name__)

APPLY_SRC_MAP = {
    "recommended": ("drecomm_apply", "--drecomm_apply-1-F-0-1--{sid}-"),
    "search":      ("srp",           "--srp-1-F-0-1--{sid}-"),
}


class NaukriJobClient:

    def __init__(self, login_client: NaukriLoginClient):
        if not login_client.session:
            raise NaukriAuthError("Login required")

        self._session = login_client.session
        self._client = login_client

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_job(self, raw: dict) -> Job:
        # Extract location from the placeholders list if present.
        location = next(
            (p["label"] for p in raw.get("placeholders", []) if p.get("type") == "location"),
            "N/A",
        )
        return Job(
            job_id=str(raw.get("jobId") or raw.get("id") or ""),
            title=raw.get("title") or raw.get("jobTitle") or "N/A",
            company=raw.get("companyName") or raw.get("company") or "N/A",
            location=location,
            experience=raw.get("experienceText") or raw.get("experience") or "N/A",
            salary=raw.get("salaryDetail") or raw.get("salary") or "Not disclosed",
            posted_date=raw.get("footerPlaceholderLabel") or raw.get("postedDate") or "N/A",
            apply_link=raw.get("jdURL") or f"https://www.naukri.com/job-listings-{raw.get('jobId', '')}",
            description=raw.get("jobDescription") or "",
            tags=(
                [t.strip() for t in raw.get("tagsAndSkills", "").split(",")]
                if raw.get("tagsAndSkills")
                else []
            ),
        )

    def _build_seo_key(self, keyword: str, location: str, page: int) -> str:
        # Produces the seoKey param expected by the search endpoint.
        # Example: "python-developer-jobs-in-bangalore-2"
        kw_slug = (
            keyword.strip().lower()
            .replace(".", "-dot-")
            .replace(" ", "-")
            .replace("+", "-")
            .strip("-")
        )

        if location.strip():
            loc_slug = location.strip().lower().replace(" ", "-")
            return f"{kw_slug}-jobs-in-{loc_slug}-{page}"

        return f"{kw_slug}-jobs-{page}"

    def _search_headers(self) -> dict:
        # Non-auth base headers plus the appid, gid, and nkparam fields
        # required by the search API.
        headers = self._client._build_headers(auth=False)
        headers.update({
            "authority":       "www.naukri.com",
            "accept":          "application/json",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "en-US,en;q=0.9",
            "appid":           "109",
            "gid":             "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
            "nkparam":         generate_nkparam("srp"),
        })
        return headers

    def _build_apply_payload(
        self,
        job: Job,
        sid: str,
        source: str,
        mandatory_skills: list[str] | None,
        optional_skills: list[str] | None,
    ) -> dict:
        apply_src, logstr_template = APPLY_SRC_MAP.get(source, APPLY_SRC_MAP["recommended"])
        return {
            "strJobsarr":       [job.job_id],
            "logstr":           logstr_template.format(sid=sid),
            **config.APPLY_PAYLOAD_DEFAULTS,
            "mandatory_skills": mandatory_skills or [],
            "optional_skills":  optional_skills or [],
            "applyTypeId":      config.APPLY_TYPE_ID,
            "applySrc":         apply_src,
            "sid":              sid,
        }

    @staticmethod
    def _json_or_raise(res) -> dict:
        try:
            return res.json()
        except Exception:
            raise NaukriParseError(f"Invalid JSON response: {res.text}")

    # ------------------------------------------------------------------
    # Job details
    # ------------------------------------------------------------------

    def get_job_details(self, job_id: str, sid: str = "") -> dict:
        if not job_id:
            raise ValueError("job_id is required")

        # Randomized sid — timestamp + 7 random digits, like a real browser
        # session (never a constant "0000000" fingerprint).
        sid = sid or humanizer.generate_sid()

        url = f"https://www.naukri.com/jobapi/v1/job/{job_id}"
        params = {
            "microsite": "y",
            "src":       "jobsearchDesk",
            "sid":       sid,
            "xp":        "1",
            "px":        "1",
        }

        headers = self._client._build_headers(auth=True)
        headers.update({
            "nkparam":        generate_nkparam("srp"),
            "appid":          "121",
            "systemid":       "Naukri",
            "clientid":       "d3skt0p",
            "accept":         "application/json",
            "referer":        "https://www.naukri.com/",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        })

        humanizer.pace("job_details")
        res = self._session.get(url, headers=headers, params=params)

        if res.status_code in (401, 403):
            if res.status_code == 403:
                humanizer.register_block(res.status_code)
            try:
                msg = res.json().get("message", "Auth failed")
            except Exception:
                msg = res.text
            raise NaukriAuthError(msg)

        if not res.ok:
            raise NaukriParseError(f"Job details fetch failed: {res.status_code} — {res.text}")

        return self._json_or_raise(res)

    def is_external_apply(self, job_id: str, sid: str = "") -> bool:
        # Returns True if the job redirects to an external company URL for apply.
        data = self.get_job_details(job_id, sid)
        return data.get("job", {}).get("responseManager") == "companyUrl"

    # ------------------------------------------------------------------
    # Apply job
    # ------------------------------------------------------------------

    def apply_job(
        self,
        job: Job,
        mandatory_skills: list[str] = None,
        optional_skills:  list[str] = None,
        sid:    str = "",
        source: str = "recommended",
    ) -> dict:
        if not job.job_id:
            raise ValueError("Invalid job_id")

        sid = sid or humanizer.generate_sid()
        payload = self._build_apply_payload(job, sid, source, mandatory_skills, optional_skills)

        headers = self._client._build_headers(auth=True)
        headers.update({
            "appid":     "121",
            "systemid":  "jobseeker",
            "clientid":  "d3skt0p",
            "accept":    "application/json",
        })

        humanizer.pace("apply")
        res = self._session.post(APPLY_JOB_URL, headers=headers, json=payload)

        if res.status_code in (401, 403, 429):
            if res.status_code in (403, 429):
                humanizer.register_block(res.status_code)
            try:
                msg = res.json().get("message", "Auth failed")
            except Exception:
                msg = res.text
            raise NaukriAuthError(msg)

        if not res.ok:
            raise NaukriParseError(f"Apply failed: {res.status_code} — {res.text}")

        return self._json_or_raise(res)

    # ------------------------------------------------------------------
    # Apply job with questionnaire answers
    # ------------------------------------------------------------------

    def handle_static_questionnaire_and_apply(
        self,
        job,
        questionnaire,
        sid,
        mandatory_skills=None,
        optional_skills=None,
        source="recommended",
    ) -> dict:

        profile = config.QUESTIONNAIRE_PROFILE

        def build_smart_answers(questionnaire: list, profile: dict) -> dict:
            answers = {}
            profile_skills = [skill.lower() for skill in profile["skills"]]

            def pick_yes(options: dict) -> str:
                # Prefer any option whose label contains "yes".
                for k, v in options.items():
                    if "yes" in v.lower():
                        return k
                return list(options.keys())[0]

            def pick_no(options: dict) -> str:
                # Prefer any option whose label contains "no" (but not "know"
                # / "notice"), falling back to the last option.
                for k, v in options.items():
                    label = v.lower().strip()
                    if label == "no" or label.startswith("no,") or label.startswith("no "):
                        return k
                for k, v in options.items():
                    if "no" in v.lower() and "know" not in v.lower():
                        return k
                return list(options.keys())[-1]

            def pick_gender(options: dict, gender: str) -> str:
                target = (gender or "Male").lower().strip()
                for k, v in options.items():
                    label = str(v).lower().strip()
                    if label == target:
                        return k
                for k, v in options.items():
                    label = str(v).lower()
                    if re.search(rf"(?<![a-z]){re.escape(target)}(?![a-z])", label):
                        return k
                return list(options.keys())[0]

            def pick_over_5_years(options: dict) -> str | None:
                for k, v in options.items():
                    label = " ".join(str(v).lower().split())
                    compact = re.sub(r"[\s\-]+", "", label)
                    if compact in (">5years", ">5yrs", "5+years", "5+yrs"):
                        return k
                    if re.search(
                        r"\b(more than|above|over|greater than)\s*5\s*(years|yrs?)\b",
                        label,
                    ):
                        return k
                return None

            def pick_notice(options: dict, notice_days: int) -> str:
                # Match the closest notice period bucket to notice_days.
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
                return list(options.keys())[0]

            def notice_text_answer(qtext: str, notice_days: int) -> str:
                # Answer notice/joining questions in the unit the question asks for.
                if "month" in qtext:
                    return str(max(1, round(notice_days / 30)))
                if "week" in qtext:
                    return str(max(1, round(notice_days / 7)))
                # Default / explicit "days"
                return str(notice_days)

            no_hints = config.NO_QUESTION_HINTS
            relocation_hints = config.RELOCATION_HINTS
            domain_experience_hints = getattr(config, "DOMAIN_EXPERIENCE_HINTS", [])
            f2f_interview_hints = getattr(config, "F2F_INTERVIEW_HINTS", [])
            ai_terms = config.AI_EXPERIENCE_TERMS

            def is_ai_related(qtext: str) -> bool:
                # Word-boundary match so short terms like "ai"/"ml" don't hit
                # inside words like "email", "main", or "html".
                return any(
                    re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", qtext)
                    for term in ai_terms
                )

            def is_joining_question(qtext: str) -> bool:
                # "notice period" is always a joining-time question. For
                # join/onboard wording, also require a time-ish word so
                # "Why do you want to join us?" is NOT matched.
                if "notice" in qtext:
                    return True
                join_words = ("join", "onboard", "start date", "date of joining")
                time_words = (
                    "how soon", "when", "day", "week", "month",
                    "immediate", "early", "soon", "time",
                )
                return any(j in qtext for j in join_words) and any(
                    t in qtext for t in time_words
                )

            def is_plain_interview_question(qtext: str) -> bool:
                if "interview" not in qtext:
                    return False
                if any(h in qtext for h in f2f_interview_hints):
                    return False
                previous_interview_hints = (
                    "interviewed before",
                    "interviewed earlier",
                    "previously interviewed",
                    "previous interview",
                    "interview before",
                    "interview earlier",
                )
                return not any(h in qtext for h in previous_interview_hints)

            def is_domain_experience_question(qtext: str) -> bool:
                if not any(h in qtext for h in domain_experience_hints):
                    return False

                def has_phrase(phrase: str) -> bool:
                    return bool(
                        re.search(
                            rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])",
                            qtext,
                        )
                    )

                company_history_words = (
                    "company",
                    "organization",
                    "organisation",
                    "employer",
                    "employee",
                    "with us",
                    "for us",
                    "at us",
                    "here",
                    "ex-employee",
                    "ex employee",
                    "former employee",
                    "previously employed",
                    "previously associated",
                    "applied before",
                    "applied earlier",
                    "interviewed before",
                    "interviewed earlier",
                    "relatives",
                    "criminal",
                )
                company_history_actions = (
                    "worked",
                    "employed",
                    "associated",
                    "applied",
                    "interviewed",
                    "relative",
                    "criminal",
                )
                if any(has_phrase(w) for w in company_history_words) and any(
                    has_phrase(a) for a in company_history_actions
                ):
                    return False
                return any(
                    h in qtext
                    for h in (
                        "have you",
                        "do you",
                        "are you",
                        "worked",
                        "experience",
                        "exposure",
                        "familiar",
                        "knowledge",
                        "handled",
                        "built",
                        "developed",
                        "client",
                        "project",
                    )
                )

            for q in questionnaire:
                qid   = q["questionId"]
                qtext = (q.get("questionName") or "").lower()
                qtype = (q.get("questionType") or "").lower()
                options = q.get("answerOption") or {}

                # Hard rules that apply regardless of question type:
                #   - "worked here before / applied earlier" style → always No
                #   - relocation questions → always Yes
                #   - masters / postgraduation → always No (candidate has none)
                is_no_question = any(h in qtext for h in no_hints)
                is_relocation = any(h in qtext for h in relocation_hints)
                is_masters_q = any(
                    h in qtext
                    for h in ("master", "post graduat", "postgraduat", "post-graduat",
                              "pg degree", "m.tech", "mtech", "m.sc", "msc", "mba", "phd")
                )
                is_domain_experience = is_domain_experience_question(qtext)
                is_gender_q = "gender" in qtext
                is_plain_interview = is_plain_interview_question(qtext)

                if qtype == "text box":
                    if (is_no_question and not is_domain_experience) or is_masters_q:
                        ans = "No"
                    elif is_gender_q:
                        ans = profile.get("gender", "Male")
                    elif is_domain_experience:
                        ans = "Yes"
                    elif is_relocation:
                        ans = "Yes"
                    elif is_plain_interview:
                        ans = "Yes"
                    elif "linkedin" in qtext:
                        ans = profile.get("linkedin_url", "")
                    elif "github" in qtext or "git hub" in qtext:
                        ans = profile.get("github_url", "")
                    elif "phone" in qtext or "mobile" in qtext or "contact number" in qtext:
                        ans = profile.get("phone", "")
                    elif "current location" in qtext or "current city" in qtext \
                            or "where are you" in qtext or "based out of" in qtext \
                            or "residing" in qtext or ("location" in qtext and "preferred" not in qtext):
                        ans = profile.get("current_location", "")
                    elif "current ctc" in qtext:
                        ans = profile["current_ctc"]
                    elif "expected ctc" in qtext:
                        ans = profile["expected_ctc"]
                    elif "experience" in qtext:
                        # AI-related experience → exp_ai; everything else → exp_total.
                        ans = profile["exp_ai"] if is_ai_related(qtext) else profile["exp_total"]
                    elif is_joining_question(qtext):
                        # Notice period / joining time, in the unit asked
                        # (days by default, converted for months/weeks).
                        ans = notice_text_answer(qtext, profile["notice_days"])
                    else:
                        # Not covered by fixed rules — let Claude answer it.
                        ans = ai_text_answer(q.get("questionName") or "") \
                            or config.DEFAULT_TEXTBOX_ANSWER

                else:
                    if options:
                        over_5_years_key = pick_over_5_years(options)
                        if (is_no_question and not is_domain_experience) or is_masters_q:
                            key = pick_no(options)
                        elif is_gender_q:
                            key = pick_gender(options, profile.get("gender", "Male"))
                        elif over_5_years_key is not None:
                            key = over_5_years_key
                        elif is_domain_experience:
                            key = pick_yes(options)
                        elif is_relocation:
                            key = pick_yes(options)
                        elif is_plain_interview:
                            key = pick_yes(options)
                        elif is_joining_question(qtext):
                            key = pick_notice(options, profile["notice_days"])
                        else:
                            # Everything else: the LLM decides which option
                            # maximizes screening success. Only if the AI is
                            # off/fails do we fall back to heuristics.
                            key = ai_option_answer(
                                q.get("questionName") or "", options
                            )
                            if key is None:
                                if any(skill in qtext for skill in profile_skills):
                                    key = pick_yes(options)
                                elif any(x in qtext for x in config.YES_QUESTION_HINTS):
                                    key = pick_yes(options)
                                else:
                                    key = list(options.keys())[0]

                        # Option-type answers must always be wrapped in a list.
                        ans = [key]
                    else:
                        if (is_no_question and not is_domain_experience) or is_masters_q:
                            ans = "No"
                        elif is_gender_q:
                            ans = profile.get("gender", "Male")
                        elif is_domain_experience:
                            ans = "Yes"
                        elif is_relocation:
                            ans = "Yes"
                        elif is_plain_interview:
                            ans = "Yes"
                        else:
                            ans = ai_text_answer(q.get("questionName") or "") \
                                or config.DEFAULT_TEXTBOX_ANSWER

                answers[qid] = ans

            return answers

        def format_answer(question: dict, answer):
            options = question.get("answerOption") or {}
            if isinstance(answer, list):
                return [options.get(str(item), str(item)) for item in answer]
            return options.get(str(answer), answer)

        def build_questionnaire_records(questionnaire: list, answers: dict) -> list[dict]:
            return [
                {
                    "question_id": question.get("questionId"),
                    "question": question.get("questionName") or "",
                    "answer": format_answer(question, answers.get(question.get("questionId"))),
                    "raw_answer": answers.get(question.get("questionId")),
                }
                for question in questionnaire
            ]

        answers = build_smart_answers(questionnaire, profile)
        questionnaire_records = build_questionnaire_records(questionnaire, answers)
        logger.debug("Generated answers: %s", answers)

        payload = self._build_apply_payload(job, sid, source, mandatory_skills, optional_skills)
        payload["applyData"] = {job.job_id: {"answers": answers}}

        headers = self._client._build_headers(auth=True)
        humanizer.pace("questionnaire_apply")
        res = self._session.post(APPLY_JOB_URL, headers=headers, json=payload)

        if res.status_code in (403, 429):
            humanizer.register_block(res.status_code, cooldown=False)

        if not res.ok:
            logger.debug("Apply failed: %s", res.text)
            return {
                "success": False,
                "error": res.text,
                "_questionnaire_answers": questionnaire_records,
            }

        try:
            parsed_result = res.json()
        except Exception:
            result = {"success": False, "error": "Invalid JSON response"}
        else:
            result = parsed_result if isinstance(parsed_result, dict) else {"response": parsed_result}

        result["_questionnaire_answers"] = questionnaire_records
        return result

    # ------------------------------------------------------------------
    # Recommended jobs
    # ------------------------------------------------------------------

    def get_recommended_jobs(self) -> list[Job]:
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        humanizer.pace("recommended")
        res = self._session.post(
            RECOMMENDED_JOBS_URL,
            headers=self._client._build_headers(auth=True),
            json={
                "clusterId":        None,
                "src":              "recommClusterApi",
                "clusterSplitDate": {
                    "apply":        now,
                    "preference":   now,
                    "profile":      now,
                    "similar_jobs": now,
                },
            },
        )

        if not res.ok:
            raise NaukriParseError(f"Recommended jobs fetch failed: {res.status_code}")

        raw_jobs = res.json().get("jobDetails") or []
        logger.debug("Recommended jobs fetched: %d", len(raw_jobs))
        return [self._parse_job(j) for j in raw_jobs]

    # ------------------------------------------------------------------
    # Search jobs
    # ------------------------------------------------------------------

    def search_jobs(
        self,
        keyword:          str,
        location:         str = "",
        page:             int = 2,
        job_age:          int = 3,
        experience:       int = 2,
        results_per_page: int = 20,
        lat_long:         str = "",
    ) -> list[Job]:

        params = {
            "noOfResults":    results_per_page,
            "urlType":        "search_by_keyword",
            "searchType":     "adv",
            "keyword":        keyword,
            "k":              keyword,
            "pageNo":         page,
            "experience":     experience,
            "jobAge":         job_age,
            "nignbevent_src": "jobsearchDeskGNB",
            "seoKey":         self._build_seo_key(keyword, location, page),
            "src":            "jobsearchDesk",
            "latLong":        lat_long,
        }

        humanizer.pace("search")
        res = self._session.get(JOB_SEARCH_URL, headers=self._search_headers(), params=params)

        if res.status_code == 403:
            humanizer.register_block(res.status_code)
            raise NaukriAuthError("403 Forbidden — nkparam token likely expired")

        if res.status_code == 429:
            humanizer.register_block(res.status_code)
            raise NaukriParseError("429 Too Many Requests — rate limited by server")

        if res.status_code == 406:
            logger.debug("406 Validation error: %s", res.text)
            return []

        if not res.ok:
            raise NaukriParseError(f"Search failed: {res.status_code} — {res.text}")

        data = res.json()
        raw_jobs = data.get("jobDetails") or data.get("jobs") or []

        if not raw_jobs:
            logger.debug("No jobs returned for keyword=%r page=%d", keyword, page)

        return [self._parse_job(j) for j in raw_jobs]
