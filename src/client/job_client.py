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
import re
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urlsplit

from src.client.naukri_client import NaukriLoginClient, SessionRecovery
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


_EXTERNAL_FLAG_KEYS = ("isExternalJob", "externalApply", "isExternal")
_EXTERNAL_URL_KEYS = ("externalApplyUrl", "applyRedirectUrl")
_TRUE_VALUES = {"true", "1", "yes", "y"}
_FALSE_VALUES = {"false", "0", "no", "n"}
_ACCESS_STATUS_FIELDS = ("statusCode", "status_code", "responseCode")
_ACCESS_DETAIL_FIELDS = (
    "message",
    "error",
    "errorMessage",
    "failureReason",
    "validationErrors",
)
_AUTH_FAILURE_RE = re.compile(
    r"\b(?:invalid\s+user|unauthori[sz]ed|authentication\s+failed|"
    r"auth\s+failed|invalid\s+(?:access\s+)?token|(?:access\s+)?token\s+expired)\b",
    re.IGNORECASE,
)


def _normalise_http_url(value) -> str:
    """Return a usable HTTP(S) URL, or an empty string for unsafe input."""
    if not isinstance(value, str):
        return ""

    url = value.strip()
    if url.startswith("//"):
        url = f"https:{url}"

    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""

    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    return url


def _external_flag(value) -> bool | None:
    """Normalize common JSON boolean encodings without treating junk as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    return None


def external_info(payload: dict, fallback_url: str = "") -> tuple[bool | None, str]:
    """Return ``(external verdict, preferred apply URL)`` for any API shape.

    Both search/recommended listings and job-details responses use this helper;
    details responses are unwrapped from their top-level ``job`` object. Any
    positive signal wins over false flags. ``companyUrl`` is deliberately only
    a URL candidate after another signal confirms that this is external -- a
    normal company homepage by itself must not remove a job from easy apply.
    """
    raw = payload
    if isinstance(payload, dict) and isinstance(payload.get("job"), dict):
        raw = payload["job"]
    if not isinstance(raw, dict):
        return None, _normalise_http_url(fallback_url)

    fallback = _normalise_http_url(fallback_url)
    redirects = [
        url
        for key in _EXTERNAL_URL_KEYS
        if (url := _normalise_http_url(raw.get(key)))
    ]

    manager = re.sub(
        r"[^a-z0-9]+", "", str(raw.get("responseManager") or "").casefold()
    )
    manager_external = manager == "companyurl"

    flags = [
        flag
        for key in _EXTERNAL_FLAG_KEYS
        if (flag := _external_flag(raw.get(key))) is not None
    ]
    has_true = any(flag is True for flag in flags)
    has_false = any(flag is False for flag in flags)

    # Redirect fields are themselves explicit external-apply evidence. Any
    # positive evidence wins when providers send stale/conflicting false flags.
    if redirects or manager_external or has_true:
        company_url = _normalise_http_url(raw.get("companyUrl"))
        return True, (redirects[0] if redirects else company_url or fallback)

    if has_false:
        return False, fallback
    return None, fallback


def _listing_url(raw: dict, job_id: str) -> str:
    """Normalize the Naukri JD link and always retain a job-id fallback."""
    value = raw.get("jdURL")
    if (
        isinstance(value, str)
        and value.strip().startswith("/")
        and not value.strip().startswith("//")
    ):
        value = f"https://www.naukri.com{value.strip()}"
    return _normalise_http_url(value) or f"https://www.naukri.com/job-listings-{job_id}"


class NaukriJobClient:

    def __init__(self, login_client: NaukriLoginClient):
        if not login_client.session:
            raise NaukriAuthError("Login required")

        self._session = login_client.session
        self._client = login_client
        # The details endpoint is fetched by id, while the writer receives the
        # original Job object. Keep the first parsed object so a details-only
        # redirect can enrich that same object before it is written to CSV.
        self._jobs_by_id: dict[str, Job] = {}

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
    def _payload_status_code(payload) -> int | None:
        if not isinstance(payload, Mapping):
            return None
        for field in _ACCESS_STATUS_FIELDS:
            value = payload.get(field)
            if isinstance(value, bool) or value is None:
                continue
            try:
                return int(str(value).strip())
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _detail_leaves(value, depth=0):
        """Yield bounded text from known error fields, never the whole payload."""
        if depth > 3 or value is None:
            return
        if isinstance(value, str):
            text = " ".join(value.split())
            if text:
                yield text
            return
        if isinstance(value, Mapping):
            priority = ("message", "error", "errorMessage", "reason", "description")
            for key in priority:
                if key in value:
                    yield from NaukriJobClient._detail_leaves(value.get(key), depth + 1)
            for key, nested in value.items():
                if key in priority:
                    continue
                yield from NaukriJobClient._detail_leaves(nested, depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for nested in value:
                yield from NaukriJobClient._detail_leaves(nested, depth + 1)

    @classmethod
    def _payload_details(cls, payload):
        if not isinstance(payload, Mapping):
            return
        for field in _ACCESS_DETAIL_FIELDS:
            for text in cls._detail_leaves(payload.get(field)):
                yield cls._snippet(text, 180)

    @classmethod
    def _payload_detail(cls, payload) -> str:
        return next(cls._payload_details(payload), "")

    @staticmethod
    def _has_auth_validation_marker(payload) -> bool:
        if not isinstance(payload, Mapping):
            return False
        errors = payload.get("validationErrors")
        if not isinstance(errors, (list, tuple)):
            return False
        for error in errors:
            if not isinstance(error, Mapping):
                continue
            field = error.get("field") or error.get("fieldName") or error.get("param")
            normalized = re.sub(r"[^a-z0-9]+", "", str(field or "").casefold())
            if normalized in {"userid", "authtoken", "accesstoken", "authorization"}:
                return True
        return False

    @classmethod
    def _auth_payload_detail(cls, payload) -> str:
        """Return a sanitized message when JSON represents an auth failure."""
        if not isinstance(payload, Mapping):
            return ""
        code = cls._payload_status_code(payload)
        details = list(cls._payload_details(payload))
        auth_detail = next(
            (detail for detail in details if _AUTH_FAILURE_RE.search(detail)),
            "",
        )
        auth_marker = bool(auth_detail) or cls._has_auth_validation_marker(payload)
        # 401 is always an authentication verdict. A 403 is ambiguous: JSON
        # "Access Denied" can be WAF/rate pushback, so require an auth marker.
        if code != 401 and not auth_marker:
            return ""
        detail = auth_detail or (details[0] if details else "")
        if code and detail:
            return f"HTTP {code} — {detail}"
        if code:
            return f"HTTP {code}"
        return detail

    @classmethod
    def _check_embedded_access_payload(cls, payload, action: str) -> None:
        """Promote auth/rate-limit errors hidden inside an HTTP-200 body."""
        if not isinstance(payload, Mapping):
            return

        candidates = [payload]
        jobs = payload.get("jobs")
        if isinstance(jobs, list):
            candidates.extend(item for item in jobs if isinstance(item, Mapping))

        for candidate in candidates:
            code = cls._payload_status_code(candidate)
            if code == 429:
                detail = cls._payload_detail(candidate)
                humanizer.register_block(code)
                suffix = f" — {detail}" if detail else ""
                raise NaukriParseError(f"{action} rate limited: HTTP 429{suffix}")

        for candidate in candidates:
            auth_detail = cls._auth_payload_detail(candidate)
            if auth_detail:
                raise NaukriAuthError(auth_detail)

        for candidate in candidates:
            if cls._payload_status_code(candidate) == 403:
                detail = cls._payload_detail(candidate)
                humanizer.register_block(403)
                suffix = f" — {detail}" if detail else ""
                raise NaukriParseError(f"{action} rejected: HTTP 403{suffix}")

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
        """Raise on auth failures / rate limits; cool down only real pushback."""
        if res.status_code in (401, 403, 429):
            try:
                payload = res.json()
            except Exception:
                payload = None

            detail = (
                NaukriJobClient._payload_detail(payload)
                or NaukriJobClient._snippet(res.text)
                or "empty body"
            )

            if res.status_code == 401:
                raise NaukriAuthError(f"HTTP 401 — {detail}")

            if res.status_code == 403:
                auth_detail = NaukriJobClient._auth_payload_detail(payload)
                if auth_detail:
                    # "Invalid User" is a credential verdict, not rate-limit
                    # pushback. Session recovery decides whether it is global
                    # or only this apply request; no multi-minute sleep first.
                    raise NaukriAuthError(auth_detail)

                humanizer.register_block(403)
                raise NaukriParseError(f"{action} rejected: HTTP 403 — {detail}")

            humanizer.register_block(429)
            raise NaukriParseError(f"{action} rate limited: HTTP 429 — {detail}")

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
        """Compatibility wrapper around the shared listing/details parser."""
        return external_info(raw)[0]

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
        job_id = str(raw.get("jobId") or raw.get("id") or "")
        listing_url = _listing_url(raw, job_id)
        external, preferred_url = external_info(raw, listing_url)

        job = Job(
            job_id=job_id,
            title=raw.get("title") or raw.get("jobTitle") or "N/A",
            company=raw.get("companyName") or raw.get("company") or "N/A",
            location=location,
            experience=raw.get("experienceText") or raw.get("experience") or "N/A",
            salary=raw.get("salaryDetail") or raw.get("salary") or "Not disclosed",
            posted_date=raw.get("footerPlaceholderLabel") or raw.get("postedDate") or "N/A",
            apply_link=preferred_url or listing_url,
            description=raw.get("jobDescription") or "",
            tags=(
                [t.strip() for t in raw.get("tagsAndSkills", "").split(",")]
                if raw.get("tagsAndSkills")
                else []
            ),
            created_ms=self._created_ms(raw),
            external=external,
        )
        # Overlapping search terms parse the same id repeatedly. Keep the
        # first object, which is the one dedup_new_jobs put in the apply pool,
        # but merge any stronger external evidence found by a later listing.
        if not hasattr(self, "_jobs_by_id"):
            self._jobs_by_id = {}
        pooled_job = self._jobs_by_id.setdefault(job_id, job)
        if pooled_job is not job:
            if external is True:
                pooled_job.external = True
                if preferred_url:
                    pooled_job.apply_link = preferred_url
            elif external is False and pooled_job.external is None:
                pooled_job.external = False
        return job

    def _retain_external_info(self, job_id: str, payload: dict) -> tuple[bool | None, str]:
        """Merge details-only external evidence into the pooled Job object."""
        job = getattr(self, "_jobs_by_id", {}).get(str(job_id))
        fallback = job.apply_link if job is not None else _listing_url({}, str(job_id))
        verdict, preferred_url = external_info(payload, fallback)

        if job is not None and verdict is True:
            job.external = True
            if preferred_url:
                job.apply_link = preferred_url
        elif job is not None and verdict is False and job.external is None:
            job.external = False

        return verdict, preferred_url

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
        details = self._json_or_raise(res)
        self._retain_external_info(job_id, details)
        return details

    def refresh_auth(self) -> SessionRecovery:
        """Recover from a stale bearer mid-run (403 'Invalid User').

        Returns whether a token rotated, remained independently valid, or the
        whole authenticated session failed validation.
        """
        try:
            return self._client.refresh_session_token()
        except Exception:
            logger.debug("Auth refresh failed", exc_info=True)
            return SessionRecovery.FAILED

    @staticmethod
    def is_external(details: dict) -> bool:
        """True if the job details say apply happens on an external company URL."""
        return external_info(details)[0] is True

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
        result = self._json_or_raise(res)
        self._log_raw_apply_response("Apply", job.job_id, res.status_code, result)
        self._check_embedded_access_payload(result, "Apply")
        return result

    @staticmethod
    def _log_raw_apply_response(action: str, job_id: str, status_code, payload) -> None:
        """Record the exact apply body so a wrong verdict stays diagnosable.

        The apply endpoint returns HTTP 200 for successes, questionnaires, and
        application-level rejections alike, so the body is the only evidence of
        what really happened. Without it the only clue was the parser's own
        sanitized reason — useless precisely when the parser is what is wrong.
        """
        if not getattr(config, "LOG_RAW_APPLY_RESPONSE", True):
            return
        if not logger.isEnabledFor(logging.DEBUG):
            return
        # Bounded: a questionnaire body is large and carries screening
        # questions, so it must never flood the log file.
        logger.debug(
            "%s raw response job_id=%s http=%s body=%s",
            action,
            job_id,
            status_code,
            NaukriJobClient._snippet(repr(payload), 1500),
        )

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
        # Only the shape, never the values: answers carry CTC, salary
        # expectations, and other personal data that must not land in a log
        # file. The full set is already persisted per applied row in
        # applied_jobs.csv when the application succeeds.
        logger.debug("Generated %d questionnaire answer(s)", len(answers or {}))

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

        if res.status_code in (401, 403, 429):
            self._check_auth_response(res, "Questionnaire apply")

        if not res.ok:
            logger.debug(
                "Questionnaire apply failed job_id=%s http=%s body=%s",
                job.job_id,
                res.status_code,
                self._snippet(res.text),
            )
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

        self._log_raw_apply_response(
            "Questionnaire apply", job.job_id, res.status_code, result
        )
        self._check_embedded_access_payload(result, "Questionnaire apply")
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
