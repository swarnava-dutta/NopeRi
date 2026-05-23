import logging
import json
import os
import time
import functools
from src.client.session import build_session
from src.config.constants import *
from src.exceptions.exceptions import *
from src.models.models import *
from src.utils.request_helper import with_exponential_retry
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_handler)

DEFAULT_COOKIE_FILE = "cookies.json"
DEFAULT_COOKIE_DOMAIN = ".naukri.com"


# ------------------------------------------------------------------
# IMPORTANT — IP / HOSTING ADVICE (read before deploying)
#################################
# Naukri actively fingerprints the IP of every login and API request.
# Through testing, certain hosting environments consistently trigger
# MFA challenges or outright bans:
#
#   AVOID:
#     - Microsoft Azure (any region)     → flagged heavily, MFA on first req
#     - GitHub Actions / CI runners      → Azure-backed IPs, same result
#     - Google Cloud (some regions)      → increasingly flagged
#     - Any datacenter IP on known CIDR  → Naukri blocks entire ranges
#
#   WORKS RELIABLY:
#     - AWS (residential NAT gateway or EC2 with Elastic IP)
#     - Home broadband / personal IP     → most reliable, zero flags
#     - Mobile hotspot                   → works, good for testing
#     - Residential proxy                → works if clean IP
#
# WHY:
#   Naukri's fraud/bot detection checks whether the IP belongs to a
#   known cloud/datacenter ASN. Azure and GitHub Actions share the
#   same Microsoft AS8075 IP ranges — Naukri recognises these
#   immediately and forces MFA, effectively breaking any headless
#   client. AWS consumer-facing IPs (especially us-east-1 NAT) are
#   less aggressively flagged, but a home server or residential IP
#   is the gold standard.
#
# RECOMMENDATION FOR AGENTS / SCHEDULED WORKERS:
#   - Run the harvester (nk_param_getter.py) and the job client
#     from a home server, a Raspberry Pi, or an AWS EC2 instance
#     with a dedicated Elastic IP (not a shared NAT).
#   - If you must use cloud, attach a residential proxy to the
#     requests session in src/client/session.py:
#
#   - Never run from GitHub Actions — the IP pool is fully burned
#     for Naukri and will MFA-block on every single run.
#
# NOTE:
#   Your login Bearer token and session cookies are tied to the IP
#   that logged in. Switching IPs mid-session will invalidate the
#   session and force a re-login, which may itself trigger MFA.
#   Keep the same IP for the full session lifetime.
# ------------------------------------------------------------------

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
  "x-requested-with": "XMLHttpRequest"
}

# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class NaukriLoginClient:

    def __init__(self, username=None, cookie_file=DEFAULT_COOKIE_FILE):
        self.username = username
        self.cookie_file = cookie_file
        self.session = build_session()
        self.naukri_session = None
        self.account_id = None
        self._cookie_payload = None
        self._cookie_payload_shape = None

    def _build_headers(self, auth=False, extra=None):
        headers = DEFAULT_HEADERS.copy()
        if auth:
            if not self.naukri_session:
                raise NaukriAuthError("Login required")
            headers["authorization"] = f"Bearer {self.naukri_session.bearer_token}"
            headers["systemid"] = "Naukri"
        if extra:
            headers.update(extra)
        return headers

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def _get_cookie_value(self, name):
        try:
            cookie = self.session.get_cookie(name)
            if cookie and getattr(cookie, "value", None):
                return cookie.value
        except Exception:
            pass

        try:
            value = self.session.cookies.get(name)
            if value:
                return value
        except Exception:
            pass

        try:
            for cookie in self.session.cookies:
                if isinstance(cookie, dict):
                    if (cookie.get("name") or cookie.get("key")) == name:
                        return cookie.get("value")
                elif getattr(cookie, "name", None) == name:
                    return cookie.value
        except Exception:
            pass

        try:
            return self.session.cookies.get_dict().get(name)
        except Exception:
            return None

    def _normalise_cookie_records(self, payload):
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

    def _set_session_cookie(
        self,
        name,
        value,
        domain=DEFAULT_COOKIE_DOMAIN,
        path="/",
        expires=None,
        secure=False,
        http_only=False,
        same_site="",
    ):
        try:
            self.session.set_cookie(
                name,
                value,
                domain=domain or DEFAULT_COOKIE_DOMAIN,
                path=path or "/",
                secure=bool(secure),
                http_only=bool(http_only),
                same_site="",
            )
            return
        except Exception:
            pass

        kwargs = {}
        if domain:
            kwargs["domain"] = domain
        if path:
            kwargs["path"] = path
        if expires is not None:
            try:
                kwargs["expires"] = int(expires)
            except (TypeError, ValueError):
                pass

        try:
            self.session.cookies.set(name, value, **kwargs)
            return
        except Exception:
            pass

        try:
            self.session.cookies.set(name, value)
            return
        except Exception:
            pass

        if isinstance(self.session.cookies, list):
            for cookie in self.session.cookies:
                if isinstance(cookie, dict) and (cookie.get("name") or cookie.get("key")) == name:
                    cookie["value"] = value
                    return
            self.session.cookies.append({
                "name": name,
                "value": value,
                "domain": domain or DEFAULT_COOKIE_DOMAIN,
                "path": path or "/",
            })
            return

        raise NaukriAuthError(f"Could not set cookie {name}")

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

            domain = record.get("domain") or record.get("host") or DEFAULT_COOKIE_DOMAIN
            path = record.get("path") or "/"
            expires = record.get("expires") or record.get("expirationDate")
            secure = record.get("secure", False)
            http_only = record.get("httpOnly", record.get("http_only", False))
            same_site = record.get("sameSite") or record.get("same_site") or ""
            self._set_session_cookie(
                str(name),
                str(value),
                domain=domain,
                path=path,
                expires=expires,
                secure=secure,
                http_only=http_only,
                same_site=same_site,
            )
            loaded += 1

        if not loaded:
            raise NaukriAuthError(f"{self.cookie_file} does not contain any usable cookies")

        return loaded

    def _session_cookie_records(self):
        records = []

        try:
            iterator = list(self.session.cookies)
        except Exception:
            iterator = []

        for cookie in iterator:
            if isinstance(cookie, dict):
                name = cookie.get("name") or cookie.get("key")
                value = cookie.get("value")
                domain = cookie.get("domain") or cookie.get("host") or DEFAULT_COOKIE_DOMAIN
                path = cookie.get("path") or "/"
                secure = cookie.get("secure")
                http_only = cookie.get("httpOnly", cookie.get("http_only"))
                expires = cookie.get("expires") or cookie.get("expirationDate")
                same_site = cookie.get("sameSite") or cookie.get("same_site")
            else:
                name = getattr(cookie, "name", None)
                value = getattr(cookie, "value", None)
                domain = getattr(cookie, "domain", None) or DEFAULT_COOKIE_DOMAIN
                path = getattr(cookie, "path", None) or "/"
                secure = getattr(cookie, "secure", None)
                http_only = getattr(cookie, "http_only", None)
                expires = getattr(cookie, "expires", None)
                same_site = getattr(cookie, "same_site", None)

            if not name or value is None:
                continue

            record = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
            }

            if secure is not None:
                record["secure"] = secure
            if http_only is not None:
                record["httpOnly"] = http_only
            if expires:
                record["expirationDate"] = expires
            if same_site:
                record["sameSite"] = same_site

            records.append(record)

        if records:
            return records

        try:
            cookies = self.session.cookies.get_dict()
        except Exception:
            cookies = dict(self.session.cookies)

        return [
            {
                "name": name,
                "value": value,
                "domain": DEFAULT_COOKIE_DOMAIN,
                "path": "/",
            }
            for name, value in cookies.items()
        ]

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

    def get_cookies(self):
        try:
            return self.session.cookies.get_dict()
        except Exception:
            return {
                record["name"]: record["value"]
                for record in self._session_cookie_records()
            }

    def build_required_cookies(self):
        cookies = self.get_cookies()
        result = {
            "test": "naukri.com",
            "is_login": "1",
        }

        for key in ["nauk_rt", "nauk_sid", "MYNAUKRI[UNID]"]:
            if cookies.get(key):
                result[key] = cookies[key]

        return result

    def get_bearer_token(self):
        return self._get_cookie_value("nauk_at")

    def login(self):
        self.load_cookies()
        token = self._get_cookie_value("nauk_at")
        if not token:
            raise NaukriAuthError(f"{self.cookie_file} does not contain nauk_at")

        self.naukri_session = NaukriSession(token, self.session.cookies)

        try:
            self._verify_cookie_session()
        except Exception as exc:
            self.naukri_session = None
            raise NaukriAuthError(f"Cookie login failed or expired: {exc}") from exc

        refreshed_token = self._get_cookie_value("nauk_at")
        if refreshed_token:
            self.naukri_session.bearer_token = refreshed_token

        self.save_cookies()
        return self.naukri_session

    # ------------------------------------------------------------------
    # OTP helpers
    # ------------------------------------------------------------------
 



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
        """
        Verify an OTP challenge issued by Naukri during login.

        Args:
            otp:        The 6-digit OTP received via SMS/email.
            username:   Phone number (if is_mobile=True) or email. Defaults
                        to the username supplied at client construction.
            is_mobile:  True if username is a mobile number (default),
                        False for email-based OTP.

        Returns:
            NaukriSession with the bearer token extracted from cookies.

        Raises:
            NaukriAuthError: On HTTP error or missing token in response.
        """
        target = username or self.username
        res = self._verify_otp_request(target, otp, is_mobile)

        if not res.ok:
            logger.error("OTP verification failed: %s %s", res.status_code, res.text)
            raise NaukriAuthError(f"OTP verification failed ({res.status_code})")

        token = self.session.cookies.get("nauk_at")
        if not token:
            # Some flows return the token in the JSON body instead
            try:
                token = res.json().get("authToken") or res.json().get("token")
            except Exception:
                pass

        if not token:
            raise NaukriAuthError("OTP verified but no auth token received")

        self.naukri_session = NaukriSession(token, self.session.cookies)

        return self.naukri_session


    @with_exponential_retry(label="send_otp")
    def _send_otp_request(self, username: str, is_mobile: bool):
        payload = {
            "username": username,
            "flowId": "login",
            "isLoginByEmail": not is_mobile,
            "isLoginByMobile": is_mobile,
        }
        otp_header=self._build_headers()
        otp_header["appid"]="100"
        return self.session.post(
            OTP_SEND_URL,
            headers=otp_header,
            json=payload,
        )

    def send_otp(self, username: str = None, is_mobile: bool = True):
        """
        Trigger Naukri to send an OTP to the user's phone/email.

        Args:
            username:   Phone number or email. Defaults to the username
                        supplied at client construction.
            is_mobile:  True for SMS OTP (default), False for email OTP.

        Returns:
            dict: Parsed JSON response from Naukri (contains flowId, etc.)

        Raises:
            NaukriAuthError: If the request fails.
        """
        target = username or self.username
        res = self._send_otp_request(target, is_mobile)

        if not res.ok:
            logger.error("Send OTP failed: %s %s", res.status_code, res.text)
            raise NaukriAuthError(f"Failed to send OTP ({res.status_code})")

        try:
            return res.json()
        except Exception:
            return {}
    @with_exponential_retry(label="verify_session")
    def _fetch_dashboard(self):
        return self.session.get(DASHBOARD_URL, headers=self._build_headers(auth=True))

    def _verify_cookie_session(self):
        if self.account_id:
            return self.account_id

        res = self._fetch_dashboard()
        if not res.ok:
            raise NaukriAuthError(f"session verify failed with HTTP {res.status_code}")

        try:
            data = res.json()
        except Exception as exc:
            content_type = ""
            try:
                content_type = res.headers.get("content-type", "")
            except Exception:
                pass
            raise NaukriAuthError(
                "session verify returned a non-JSON response "
                f"(HTTP {res.status_code}, content-type: {content_type or 'unknown'}). "
                "The saved cookies are expired, IP-bound to another connection, or blocked by Naukri."
            ) from exc

        account_id = data.get("profileId") or data.get("dashBoard", {}).get("profileId")
        if not account_id:
            raise NaukriParseError("account id missing")

        self.account_id = account_id
        return account_id

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
        """
        Fetch job application history.
        
        Args:
            page_size:    Number of results per page (default 10)
            days:         How far back to look (default 90)
            page_number:  Page number (default 1)
            mobile:       Use mobile headers (default False)
        """
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
