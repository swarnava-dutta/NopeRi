"""Cookie-based login client for Naukri.

IP / HOSTING NOTE: Naukri fingerprints the IP of every login and API request.
Datacenter IPs (Azure, GitHub Actions, most GCP) get MFA-challenged or banned
on sight. Run from a residential IP, mobile hotspot, or clean residential
proxy. The bearer token and cookies are tied to the login IP — switching IPs
mid-session invalidates the session.
"""

import json
import logging
import os
import time
from email.utils import formatdate, parsedate_to_datetime

from src.client.session import build_session
from src.config.constants import (
    DASHBOARD_URL,
    HISTORY_URL,
    OTP_SEND_URL,
    OTP_VERIFY_URL,
)
from src.exceptions.exceptions import NaukriAuthError, NaukriParseError
from src.models.models import ApplicationHistory, ApplicationStatus, NaukriSession
from src.utils.request_helper import with_exponential_retry

logger = logging.getLogger(__name__)

DEFAULT_COOKIE_FILE = "cookies.json"
DEFAULT_COOKIE_DOMAIN = ".naukri.com"
COOKIE_REFRESH_URLS = (
    "https://www.naukri.com/mnjuser/profile",
    "https://www.naukri.com/mnjuser/recommendedjobs",
    "https://www.naukri.com/",
)
COOKIE_EXPIRY_SKEW_SECONDS = 300

DEFAULT_HEADERS = {
    "accept": "application/json",
    "appid": "105",
    "clientid": "d3skt0p",
    "content-type": "application/json",
    "referer": "https://www.naukri.com/nlogin/login",
    "systemid": "jobseeker",
    "x-requested-with": "XMLHttpRequest",
}

OTP_HEADERS = {
    "accept": "application/json",
    "appid": "100",
    "content-type": "application/json",
    "referer": "https://www.naukri.com/nlogin/login?URL=//www.naukri.com/mnjuser/recommendedjobs",
    "sec-ch-ua": "\"Chromium\";v=\"146\", \"Not-A.Brand\";v=\"24\", \"Google Chrome\";v=\"146\"",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": "\"Windows\"",
    "systemid": "jobseeker",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "x-requested-with": "XMLHttpRequest",
}


def _expiry_to_epoch(expires) -> float | None:
    """Accepts unix epoch (browser export) or RFC1123 string (httpcloak)."""
    if not expires:
        return None
    try:
        return float(expires)
    except (TypeError, ValueError):
        pass
    try:
        return parsedate_to_datetime(str(expires)).timestamp()
    except Exception:
        return None


class NaukriLoginClient:

    def __init__(self, username=None, cookie_file=DEFAULT_COOKIE_FILE):
        self.username = username
        self.cookie_file = cookie_file
        self.session = build_session()
        self.naukri_session = None
        self.account_id = None
        self._cookie_payload = None
        self._cookie_payload_shape = None

    def build_headers(self, auth=False, extra=None):
        headers = DEFAULT_HEADERS.copy()
        if auth:
            if not self.naukri_session:
                raise NaukriAuthError("Login required")
            # nauk_at is a short-lived JWT that Naukri rotates mid-session
            # (set-cookie auto-updates the jar). Always send the live cookie
            # value so the bearer never goes stale during a long run —
            # a stale bearer gets 403 "Invalid User" from the apply API.
            live_token = self._get_cookie_value("nauk_at")
            if live_token and live_token != self.naukri_session.bearer_token:
                logger.debug("nauk_at rotated — updating bearer token")
                self.naukri_session.bearer_token = live_token
            headers["authorization"] = f"Bearer {self.naukri_session.bearer_token}"
            headers["systemid"] = "Naukri"
        if extra:
            headers.update(extra)
        return headers

    # Backwards-compatible alias (used by older integrations).
    _build_headers = build_headers

    def _build_web_headers(self):
        return {
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "referer": "https://www.naukri.com/",
            "sec-ch-ua": OTP_HEADERS["sec-ch-ua"],
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "upgrade-insecure-requests": "1",
            "user-agent": OTP_HEADERS["user-agent"],
        }

    # ------------------------------------------------------------------
    # Cookie handling (httpcloak native cookie API)
    # ------------------------------------------------------------------

    def _get_cookie_value(self, name):
        cookie = self.session.get_cookie(name)
        return cookie.value if cookie else None

    def _cookie_expires_soon(self, name, skew_seconds=COOKIE_EXPIRY_SKEW_SECONDS):
        cookie = self.session.get_cookie(name)
        expires = _expiry_to_epoch(getattr(cookie, "expires", None)) if cookie else None
        if not expires:
            return False
        return expires <= time.time() + skew_seconds

    def load_cookies(self):
        if not os.path.exists(self.cookie_file):
            raise NaukriAuthError(f"{self.cookie_file} not found")

        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except json.JSONDecodeError as exc:
            raise NaukriAuthError(f"{self.cookie_file} is not valid JSON: {exc}") from exc

        self._cookie_payload = payload
        records = self._normalise_cookie_records(payload)
        loaded = 0

        for record in records:
            if not isinstance(record, dict):
                continue

            name = record.get("name") or record.get("key")
            value = record.get("value")
            if not name or value is None:
                continue

            # httpcloak wants expires as an RFC1123 string; browser exports
            # store an epoch under expirationDate.
            expires_epoch = _expiry_to_epoch(
                record.get("expirationDate") or record.get("expires")
            )
            self.session.set_cookie(
                str(name),
                str(value),
                domain=record.get("domain") or record.get("host") or DEFAULT_COOKIE_DOMAIN,
                path=record.get("path") or "/",
                secure=bool(record.get("secure", False)),
                http_only=bool(record.get("httpOnly", record.get("http_only", False))),
                expires=formatdate(expires_epoch, usegmt=True) if expires_epoch else None,
            )
            loaded += 1

        if not loaded:
            raise NaukriAuthError(f"{self.cookie_file} does not contain any usable cookies")

        return loaded

    def _normalise_cookie_records(self, payload):
        """Accepts a browser-export list, {"cookies": [...]}, or a flat
        name→value object. Remembers the shape so save_cookies() can write
        the same format back."""
        if isinstance(payload, list):
            self._cookie_payload_shape = "list"
            return payload

        if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
            self._cookie_payload_shape = "wrapped_list"
            return payload["cookies"]

        if isinstance(payload, dict):
            self._cookie_payload_shape = "dict"
            return [
                {"name": name, "value": value}
                for name, value in payload.items()
                if not isinstance(value, (dict, list))
            ]

        raise NaukriAuthError(f"{self.cookie_file} must contain a cookie list or object")

    def _session_cookie_records(self):
        records = []
        for cookie in self.session.get_cookies_detailed():
            if not cookie.name or cookie.value is None:
                continue

            record = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or DEFAULT_COOKIE_DOMAIN,
                "path": cookie.path or "/",
            }
            if cookie.secure:
                record["secure"] = cookie.secure
            if cookie.http_only:
                record["httpOnly"] = cookie.http_only
            if cookie.same_site:
                record["sameSite"] = cookie.same_site

            expires_epoch = _expiry_to_epoch(cookie.expires)
            if expires_epoch:
                record["expirationDate"] = expires_epoch

            records.append(record)

        return records

    def _merge_cookie_records(self, original_records, current_records):
        current_by_name = {
            record["name"]: record
            for record in current_records
            if record.get("name")
        }
        seen = set()
        merged = []

        for original in original_records:
            if not isinstance(original, dict):
                continue

            name = original.get("name") or original.get("key")
            if not name:
                continue

            updated = dict(original)
            current = current_by_name.get(name)
            if current:
                updated["name"] = name
                updated["value"] = current["value"]
                for key in ("domain", "path", "secure", "expirationDate"):
                    if key in current:
                        updated[key] = current[key]
                seen.add(name)

            merged.append(updated)

        for record in current_records:
            if record["name"] not in seen:
                merged.append(record)

        return merged

    def save_cookies(self):
        current_records = self._session_cookie_records()

        if self._cookie_payload_shape == "dict":
            payload = {
                record["name"]: record["value"]
                for record in current_records
            }
        elif self._cookie_payload_shape == "wrapped_list":
            payload = dict(self._cookie_payload or {})
            payload["cookies"] = self._merge_cookie_records(
                self._cookie_payload.get("cookies", []),
                current_records,
            )
        else:
            payload = self._merge_cookie_records(
                self._cookie_payload if isinstance(self._cookie_payload, list) else [],
                current_records,
            )

        temp_path = f"{self.cookie_file}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(temp_path, self.cookie_file)

    def _has_usable_access_cookie(self):
        return bool(self._get_cookie_value("nauk_at")) and not self._cookie_expires_soon("nauk_at")

    def refresh_session_token(self) -> bool:
        """Mid-run recovery for an expired/rotated nauk_at (403 "Invalid User").

        Re-visits the cookie-refresh URLs so the server issues a fresh
        access token, updates the bearer, and persists the cookies for the
        next run. Returns True only if a *different* usable token was
        obtained (retrying with the same token would be pointless).
        """
        if not self.naukri_session:
            return False
        before = self.naukri_session.bearer_token
        self._refresh_cookie_session()
        token = self._get_cookie_value("nauk_at")
        if not token or token == before:
            return False
        self.naukri_session.bearer_token = token
        try:
            self.save_cookies()
        except Exception:
            logger.debug("Could not persist refreshed cookies", exc_info=True)
        return True

    def _refresh_cookie_session(self):
        before = self._get_cookie_value("nauk_at")

        for url in COOKIE_REFRESH_URLS:
            try:
                res = self.session.get(url, headers=self._build_web_headers())
            except Exception:
                continue

            if res.status_code in (401, 403):
                continue

            after = self._get_cookie_value("nauk_at")
            if after and (after != before or self._has_usable_access_cookie()):
                return True

        return self._has_usable_access_cookie()

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def login(self):
        self.load_cookies()
        token = self._get_cookie_value("nauk_at")
        if not token or self._cookie_expires_soon("nauk_at"):
            self._refresh_cookie_session()
            token = self._get_cookie_value("nauk_at")

        if not token or self._cookie_expires_soon("nauk_at"):
            raise NaukriAuthError(
                f"{self.cookie_file} does not contain a usable nauk_at. "
                "Export fresh browser cookies or verify OTP once, then rerun."
            )

        self.naukri_session = NaukriSession(token, self.session.cookies)

        try:
            self._verify_cookie_session()
        except Exception as exc:
            # One retry: refresh the cookies and verify again if the
            # server rotated the token.
            self._refresh_cookie_session()
            refreshed_token = self._get_cookie_value("nauk_at")
            if not refreshed_token or refreshed_token == token:
                self.naukri_session = None
                raise NaukriAuthError(f"Cookie login failed or expired: {exc}") from exc

            self.naukri_session = NaukriSession(refreshed_token, self.session.cookies)
            try:
                self._verify_cookie_session()
            except Exception as retry_exc:
                self.naukri_session = None
                raise NaukriAuthError(f"Cookie login failed or expired: {retry_exc}") from retry_exc

        refreshed_token = self._get_cookie_value("nauk_at")
        if refreshed_token:
            self.naukri_session.bearer_token = refreshed_token

        self.save_cookies()
        return self.naukri_session

    @with_exponential_retry(label="verify_session")
    def _fetch_dashboard(self):
        return self.session.get(DASHBOARD_URL, headers=self.build_headers(auth=True))

    def _verify_cookie_session(self):
        if self.account_id:
            return self.account_id

        res = self._fetch_dashboard()
        if not res.ok:
            raise NaukriAuthError(f"session verify failed with HTTP {res.status_code}")

        try:
            data = res.json()
        except Exception as exc:
            content_type = res.headers.get("content-type", "") or "unknown"
            raise NaukriAuthError(
                "session verify returned a non-JSON response "
                f"(HTTP {res.status_code}, content-type: {content_type}). "
                "The saved cookies are expired, IP-bound to another connection, or blocked by Naukri."
            ) from exc

        account_id = data.get("profileId") or data.get("dashBoard", {}).get("profileId")
        if not account_id:
            raise NaukriParseError("account id missing")

        self.account_id = account_id
        return account_id

    # ------------------------------------------------------------------
    # OTP helpers
    # ------------------------------------------------------------------

    @with_exponential_retry(label="send_otp")
    def _send_otp_request(self, username: str, is_mobile: bool):
        payload = {
            "username": username,
            "flowId": "login",
            "isLoginByEmail": not is_mobile,
            "isLoginByMobile": is_mobile,
        }
        return self.session.post(
            OTP_SEND_URL,
            headers=self.build_headers(extra={"appid": "100"}),
            json=payload,
        )

    def send_otp(self, username: str = None, is_mobile: bool = True):
        """Trigger Naukri to send an OTP to the user's phone/email."""
        target = username or self.username
        res = self._send_otp_request(target, is_mobile)

        if not res.ok:
            logger.error("Send OTP failed: %s %s", res.status_code, res.text)
            raise NaukriAuthError(f"Failed to send OTP ({res.status_code})")

        try:
            return res.json()
        except Exception:
            return {}

    @with_exponential_retry(label="verify_otp")
    def _verify_otp_request(self, username: str, otp: str, is_mobile: bool):
        payload = {
            "username": username,
            "token": otp,
            "flowId": "login",
            "isLoginByEmail": not is_mobile,
            "isLoginByMobile": is_mobile,
        }
        return self.session.post(
            OTP_VERIFY_URL,
            headers=OTP_HEADERS,
            json=payload,
        )

    def verify_otp(self, otp: str, username: str = None, is_mobile: bool = True):
        """Verify an OTP challenge issued by Naukri during login.

        Returns a NaukriSession with the bearer token extracted from cookies.
        """
        target = username or self.username
        res = self._verify_otp_request(target, otp, is_mobile)

        if not res.ok:
            logger.error("OTP verification failed: %s %s", res.status_code, res.text)
            raise NaukriAuthError(f"OTP verification failed ({res.status_code})")

        token = self._get_cookie_value("nauk_at")
        if not token:
            # Some flows return the token in the JSON body instead
            try:
                token = res.json().get("authToken") or res.json().get("token")
            except Exception:
                pass

        if not token:
            raise NaukriAuthError("OTP verified but no auth token received")

        self.naukri_session = NaukriSession(token, self.session.cookies)
        self.save_cookies()

        return self.naukri_session

    # ------------------------------------------------------------------
    # Application history
    # ------------------------------------------------------------------

    @with_exponential_retry(label="fetch_history")
    def _fetch_history_request(self, page_size, days, page_number, mobile=False):
        headers = {
            "accept": "application/json",
            "appid": "135" if mobile else "107",
            "systemid": "135" if mobile else "107",
            "content-type": "application/json",
            "x-requested-with": "XMLHttpRequest",
            "referer": "https://www.naukri.com/apply/historypage" if mobile else "https://www.naukri.com/myapply/historypage",
            "authorization": f"Bearer {self.naukri_session.bearer_token}",
        }
        if mobile:
            headers["clientid"] = "m0b5"

        params = {
            "pageSize": page_size,
            "days": days,
            "pageNumber": page_number,
        }
        if not mobile:
            params["filterInfo"] = 2

        return self.session.get(HISTORY_URL, headers=headers, params=params)

    def get_application_history(self, page_size=10, days=90, page_number=1, mobile=False):
        """Fetch job application history."""
        if not self.naukri_session:
            raise NaukriAuthError("Login first")

        res = self._fetch_history_request(page_size, days, page_number, mobile)

        if not res.ok:
            raise NaukriParseError(f"Failed to fetch history: {res.status_code}")

        return res.json()

    def parse_history(self, raw: dict) -> list[ApplicationHistory]:
        results = []
        for item in raw.get("applyDetails", []):
            statuses = [
                ApplicationStatus(
                    status_id=s["statusId"],
                    status_value=s["statusValue"],
                    date_time=s["dateTime"],
                )
                for s in item.get("status", [])
            ]
            rating = item.get("companyRating", {})
            results.append(ApplicationHistory(
                job_id=item["jobId"],
                job_title=item["jobTitle"],
                company=item["company"],
                location=item["location"],
                apply_type=item["applyType"],
                is_open=item["isOpen"] == "true",
                ars_score=item.get("arsScore", 0),
                star_rating=item.get("starRating", "0"),
                job_type=item.get("jobType", ""),
                statuses=statuses,
                company_rating=float(rating["AggregateRating"]) if rating else None,
                logo_path=item.get("logoPath"),
            ))
        return results
