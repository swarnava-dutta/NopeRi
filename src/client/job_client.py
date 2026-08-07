"""Thin client over Naukri's internal APIs using an authenticated session.

Handles job search, recommendations, and apply workflows:
- Builds correct headers (authenticated and non-authenticated)
- Generates SEO-style keys for the search endpoint
- Attaches the required signed ``nkparam`` header (403 without it)
- Parses raw API responses into the Job model
- Handles common failure cases (403, 406, 429, malformed JSON)
- Treats "page doesn't exist" 400s (code 400007) as end of results
- Retries transient 5xx "System Error" responses with backoff
"""

import logging
import random
import time
from datetime import datetime, timezone

from src.client.naukri_client import NaukriLoginClient
from src.config import agent_config as config
from src.config.constants import APPLY_JOB_URL, JOB_DETAILS_URL, JOB_SEARCH_URL, RECOMMENDED_JOBS_URL
from src.exceptions.exceptions import NaukriAuthError, NaukriParseError
from src.models.models import Job
from src.utils import humanizer
from src.utils import questionnaire as questionnaire_engine
from src.utils.nkparam_generator import generate_nkparam

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

    def _api_headers(self, systemid: str = "Naukri", **extra) -> dict:
        """Authenticated headers with the shared jobapi fields."""
        headers = self._client._build_headers(auth=True)
        headers.update({
            "appid":    "121",
            "systemid": systemid,
            "clientid": "d3skt0p",
            "accept":   "application/json",
        })
        headers.update(extra)
        return headers

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

    @staticmethod
    def _snippet(text: str, limit: int = 300) -> str:
        """Compact a response body for error messages (5xx pages are huge HTML)."""
        text = " ".join((text or "").split())
        return text[:limit] + ("…" if len(text) > limit else "")

    @staticmethod
    def _page_past_end(res) -> bool:
        """True when a 400 means the requested pageNo is past the last page.

        Searches with few results have fewer pages than SEARCH_PAGES; asking
        for a page beyond the end returns 400 with customErrorCode 400007
        ("Requested page number doesn't exists"). That's an end-of-results
        signal, not an error.
        """
        try:
            errors = res.json().get("validationErrors") or []
            return any(
                err.get("field") == "pageNo" or str(err.get("customErrorCode")) == "400007"
                for err in errors
            )
        except Exception:
            return False

    @staticmethod
    def _check_auth_response(res, action: str) -> None:
        """Raise on auth failures / rate limits; register 403/429 blocks."""
        if res.status_code in (401, 403, 429):
            if res.status_code in (403, 429):
                humanizer.register_block(res.status_code)
            try:
                detail = res.json().get("message") or ""
            except Exception:
                detail = ""
            # Always name the status code. A bare "Auth failed" (or worse, an
            # empty string from a {"message": ""} body) filled the logs with
            # "⚠️ Failed: <job> | [AUTH ERROR]" and nothing to diagnose from.
            detail = detail or NaukriJobClient._snippet(res.text) or "empty body"
            raise NaukriAuthError(f"HTTP {res.status_code} — {detail}")

        if not res.ok:
            raise NaukriParseError(
                f"{action} failed: {res.status_code} — {NaukriJobClient._snippet(res.text)}"
            )

    @staticmethod
    def _json_or_raise(res) -> dict:
        try:
            return res.json()
        except Exception:
            raise NaukriParseError(
                f"Invalid JSON response: {NaukriJobClient._snippet(res.text)}"
            )

    @staticmethod
    def _retry_transient(send, action: str):
        """Run ``send()`` and retry transient failures with backoff + jitter.

        Two kinds are transient and both get retried:

        - HTTP 5xx — Naukri's job APIs randomly throw 500 "System Error"
          HTML pages even for perfectly valid requests. Flaky backend, not
          a client bug.
        - A raised transport error — httpcloak raises
          ``HTTPCloakError("Request failed")`` on a TLS reset or timeout.
          Previously this escaped straight to the caller, so a single
          network blip permanently burnt a job ("⚠️ Failed: … | Request
          failed" appears 6 times in logs/noperi_hidden.log).

        Non-5xx responses are returned immediately for normal handling.
        """
        attempts = max(1, config.TRANSIENT_RETRY_ATTEMPTS)
        res = None
        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            try:
                res = send()
                if res.status_code < 500 or last:
                    return res
                reason = f"server error {res.status_code}"
            except Exception as exc:
                if last:
                    raise
                reason = f"{type(exc).__name__}: {exc}"

            wait = min(
                config.TRANSIENT_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                config.TRANSIENT_RETRY_MAX_SECONDS,
            ) + random.uniform(0.5, 2.0)
            logger.debug(
                "%s: %s (attempt %d/%d) — retrying in %.1fs",
                action, reason, attempt, attempts, wait,
            )
            print(f"🔁 {action}: {reason}, retrying in {wait:.0f}s "
                  f"({attempt}/{attempts - 1})...")
            time.sleep(wait)
        return res

    @staticmethod
    def _external_hint(raw: dict) -> bool | None:
        """Best-effort 'is this an external apply?' read of a listing payload.

        Search/recommended listings sometimes already say the apply happens
        on the company's own site. When they do we can drop the job into
        external_jobs.csv without ever spending a job-details request on it.
        Returns None when the listing gives us nothing to go on — only the
        details endpoint can decide in that case.
        """
        if raw.get("responseManager") == "companyUrl":
            return True

        for key in ("applyRedirectUrl", "externalApplyUrl", "companyUrl"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return True

        for key in ("isExternalJob", "externalApply", "isExternal"):
            value = raw.get(key)
            if isinstance(value, bool):
                return value

        return None

    @staticmethod
    def _created_ms(raw: dict) -> int | None:
        """Exact posting time in epoch ms, if the listing carries one.

        Naukri sends ``createdDate`` (and sometimes ``footerPlaceholderLabel``'s
        numeric twin) as epoch milliseconds. This is what the apply order
        sorts on, so it's worth pulling out of every payload shape we've seen.
        Values are sanity-checked: anything not plausibly a recent ms epoch is
        discarded so a bad field can't jump the queue.
        """
        for key in ("createdDate", "createdOn", "postedDate", "jobPostedDate"):
            value = raw.get(key)
            if value is None:
                continue
            try:
                number = int(float(value))
            except (TypeError, ValueError):
                continue
            # Seconds-precision epochs (10 digits) show up occasionally.
            if 1_000_000_000 <= number <= 9_999_999_999:
                number *= 1000
            # Roughly year 2001 -> year 2286 in ms; anything else isn't a date.
            if 1_000_000_000_000 <= number <= 9_999_999_999_999:
                return number
        return None

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
            created_ms=self._created_ms(raw),
            external=self._external_hint(raw),
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

    # ------------------------------------------------------------------
    # Job details
    # ------------------------------------------------------------------

    def get_job_details(self, job_id: str, sid: str = "") -> dict:
        if not job_id:
            raise ValueError("job_id is required")

        # Randomized sid — timestamp + 7 random digits, like a real browser
        # session (never a constant "0000000" fingerprint).
        sid = sid or humanizer.generate_sid()

        params = {
            "microsite": "y",
            "src":       "jobsearchDesk",
            "sid":       sid,
            "xp":        "1",
            "px":        "1",
        }

        headers = self._api_headers(
            nkparam=generate_nkparam("srp"),
            referer="https://www.naukri.com/",
            **{
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
            },
        )

        humanizer.pace("job_details")
        res = self._retry_transient(
            lambda: self._session.get(f"{JOB_DETAILS_URL}/{job_id}", headers=headers, params=params),
            "Job details fetch",
        )
        self._check_auth_response(res, "Job details fetch")
        return self._json_or_raise(res)

    def refresh_auth(self) -> bool:
        """Recover from a stale bearer mid-run (403 'Invalid User').

        Returns True if a fresh token was obtained and a retry makes sense.
        """
        try:
            return self._client.refresh_session_token()
        except Exception:
            logger.debug("Auth refresh failed", exc_info=True)
            return False

    @staticmethod
    def is_external(details: dict) -> bool:
        """True if the job details say apply happens on an external company URL."""
        return ((details or {}).get("job") or {}).get("responseManager") == "companyUrl"

    def is_external_apply(self, job_id: str, sid: str = "") -> bool:
        # Returns True if the job redirects to an external company URL for apply.
        return self.is_external(self.get_job_details(job_id, sid))

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

        humanizer.pace("apply")
        res = self._retry_transient(
            lambda: self._session.post(
                APPLY_JOB_URL,
                headers=self._api_headers(systemid="jobseeker"),
                json=payload,
            ),
            "Apply",
        )
        self._check_auth_response(res, "Apply")
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
        answers = questionnaire_engine.build_answers(
            questionnaire, config.QUESTIONNAIRE_PROFILE
        )
        records = questionnaire_engine.build_records(questionnaire, answers)
        logger.debug("Generated answers: %s", answers)

        payload = self._build_apply_payload(job, sid, source, mandatory_skills, optional_skills)
        payload["applyData"] = {job.job_id: {"answers": answers}}

        humanizer.pace("questionnaire_apply")
        res = self._retry_transient(
            lambda: self._session.post(
                APPLY_JOB_URL,
                headers=self._client._build_headers(auth=True),
                json=payload,
            ),
            "Questionnaire apply",
        )

        if res.status_code in (403, 429):
            humanizer.register_block(res.status_code, cooldown=False)

        if not res.ok:
            logger.debug("Apply failed: %s", res.text)
            return {
                "success": False,
                "error": f"{res.status_code} — {self._snippet(res.text)}",
                "_questionnaire_answers": records,
            }

        try:
            parsed = res.json()
        except Exception:
            result = {"success": False, "error": "Invalid JSON response"}
        else:
            result = parsed if isinstance(parsed, dict) else {"response": parsed}

        result["_questionnaire_answers"] = records
        return result

    # ------------------------------------------------------------------
    # Recommended jobs
    # ------------------------------------------------------------------

    def get_recommended_jobs(self) -> list[Job]:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        humanizer.pace("recommended")
        res = self._retry_transient(
            lambda: self._session.post(
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
            ),
            "Recommended jobs fetch",
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
        sort_by:          str = "",
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

        # Ask the server for date-sorted results ("f" = freshness on the SRP).
        # Without this the API returns relevance order, so the newest postings
        # can sit past SEARCH_PAGES and never be fetched at all — something no
        # amount of local sorting can fix.
        if sort_by:
            params["sort"] = sort_by

        humanizer.pace("search")
        res = self._retry_transient(
            lambda: self._session.get(JOB_SEARCH_URL, headers=self._search_headers(), params=params),
            "Search",
        )

        if res.status_code == 403:
            humanizer.register_block(res.status_code)
            raise NaukriAuthError("403 Forbidden — nkparam token likely expired")

        if res.status_code == 429:
            humanizer.register_block(res.status_code)
            raise NaukriParseError("429 Too Many Requests — rate limited by server")

        if res.status_code == 406:
            logger.debug("406 response: %s", res.text)
            body = self._snippet(res.text, 200)

            # Naukri returns 406 {"message":"recaptcha required"} when it
            # decides the traffic looks automated. That is bot pushback, NOT
            # "no results" — swallowing it silently made whole search terms
            # look legitimately empty while the account was being throttled.
            #
            # Registered as a challenge, not a block: it ends the run at once
            # rather than sleeping through escalating cooldowns (60s, 120s,
            # 180s...) that cannot clear a recaptcha anyway.
            if "recaptcha" in body.lower():
                humanizer.register_challenge(res.status_code)
                raise NaukriParseError(
                    f"406 recaptcha required — server thinks traffic is automated ({keyword!r})"
                )

            print(f"⚠️ Search rejected (406) for {keyword!r}"
                  f"{f' with sort={sort_by!r}' if sort_by else ''} — {body}")
            return []



        if res.status_code == 400 and self._page_past_end(res):
            logger.debug(
                "Page %d doesn't exist for keyword=%r exp=%s — end of results",
                page, keyword, experience,
            )
            return []

        if not res.ok:
            raise NaukriParseError(
                f"Search failed: {res.status_code} — {self._snippet(res.text)}"
            )

        data = res.json()
        raw_jobs = data.get("jobDetails") or data.get("jobs") or []

        if not raw_jobs:
            logger.debug("No jobs returned for keyword=%r page=%d", keyword, page)

        return [self._parse_job(j) for j in raw_jobs]
