"""Offline regression tests for apply-auth classification and recovery."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.easy_apply_agent import EasyApplyAgent
from src.agents.job_utils import empty_stats
from src.client.job_client import NaukriJobClient
from src.client.naukri_client import NaukriLoginClient, SessionRecovery
from src.exceptions.exceptions import NaukriAuthError, NaukriParseError
from src.models.models import Job, JobLead, NaukriSession


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.ok = 200 <= status_code < 300
        self.headers = {"content-type": "application/json"}

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


def _job(job_id="1001"):
    return Job(
        job_id=job_id,
        title="GenAI Engineer",
        company="Example",
        location="Pune",
        experience="5",
        salary="N/A",
        posted_date="today",
        apply_link=f"https://www.naukri.com/job-listings-{job_id}",
    )


class AccessClassificationTests(unittest.TestCase):
    def test_invalid_user_403_is_auth_without_block_cooldown(self):
        response = _Response(
            403,
            {"message": "Invalid User", "statusCode": 403},
            '{"message":"Invalid User"}',
        )
        with patch("src.client.job_client.humanizer.register_block") as register:
            with self.assertRaisesRegex(NaukriAuthError, "Invalid User"):
                NaukriJobClient._check_auth_response(response, "Apply")
        register.assert_not_called()

    def test_generic_403_and_429_remain_server_pushback(self):
        cases = (
            (_Response(403, ValueError("not json"), "Access Denied"), 403),
            (
                _Response(
                    403,
                    {"statusCode": 403, "message": "Access Denied"},
                    '{"statusCode":403,"message":"Access Denied"}',
                ),
                403,
            ),
            (_Response(429, {"message": "Too Many Requests"}), 429),
        )
        for response, code in cases:
            with self.subTest(code=code):
                with patch("src.client.job_client.humanizer.register_block") as register:
                    with self.assertRaises(NaukriParseError):
                        NaukriJobClient._check_auth_response(response, "Apply")
                register.assert_called_once_with(code)

    def test_http_200_embedded_invalid_user_raises_auth(self):
        payload = {
            "message": "Invalid User",
            "statusCode": 403,
            "validationErrors": [{"field": "userId"}],
        }
        with patch("src.client.job_client.humanizer.register_block") as register:
            with self.assertRaisesRegex(NaukriAuthError, "HTTP 403.*Invalid User"):
                NaukriJobClient._check_embedded_access_payload(payload, "Apply")
        register.assert_not_called()

    def test_auth_marker_in_later_error_field_is_not_hidden_by_generic_message(self):
        payload = {
            "statusCode": 403,
            "message": "Request rejected",
            "errorMessage": "Invalid User",
        }
        with patch("src.client.job_client.humanizer.register_block") as register:
            with self.assertRaisesRegex(NaukriAuthError, "Invalid User"):
                NaukriJobClient._check_embedded_access_payload(payload, "Apply")
        register.assert_not_called()

    def test_nested_job_invalid_user_is_promoted_to_auth(self):
        payload = {
            "statusCode": 200,
            "jobs": [
                {"jobId": "1001", "statusCode": 403, "message": "Invalid User"}
            ],
        }
        with patch("src.client.job_client.humanizer.register_block") as register:
            with self.assertRaisesRegex(NaukriAuthError, "Invalid User"):
                NaukriJobClient._check_embedded_access_payload(payload, "Apply")
        register.assert_not_called()

    def test_embedded_generic_403_keeps_block_cooldown(self):
        payload = {
            "statusCode": 200,
            "jobs": [
                {"jobId": "1001", "statusCode": 403, "message": "Access Denied"}
            ],
        }
        with patch("src.client.job_client.humanizer.register_block") as register:
            with self.assertRaises(NaukriParseError):
                NaukriJobClient._check_embedded_access_payload(payload, "Apply")
        register.assert_called_once_with(403)

    def test_direct_and_questionnaire_apply_promote_embedded_auth(self):
        payload = {"statusCode": 403, "message": "Invalid User"}
        client = NaukriJobClient.__new__(NaukriJobClient)
        client._session = Mock()
        client._session.post.return_value = _Response(200, payload)
        client._client = Mock()
        client._client._build_headers.return_value = {}
        client._api_headers = Mock(return_value={})
        client._build_apply_payload = Mock(return_value={"strJobsarr": ["1001"]})

        with patch("src.client.job_client.humanizer.pace"):
            with self.assertRaises(NaukriAuthError):
                client.apply_job(_job())

        with (
            patch("src.client.job_client.humanizer.pace"),
            patch("src.client.job_client.questionnaire_engine.build_answers", return_value={}),
            patch("src.client.job_client.questionnaire_engine.build_records", return_value=[]),
        ):
            with self.assertRaises(NaukriAuthError):
                client.handle_static_questionnaire_and_apply(
                    _job(), questionnaire=[], sid="sid"
                )


class SessionRecoveryTests(unittest.TestCase):
    def _client(self, token="same"):
        client = NaukriLoginClient.__new__(NaukriLoginClient)
        client.naukri_session = NaukriSession(token, {})
        client.account_id = "cached-profile"
        return client

    def test_boolean_compatibility_only_retries_rotated_tokens(self):
        self.assertTrue(SessionRecovery.REFRESHED)
        self.assertFalse(SessionRecovery.CURRENT_VALID)
        self.assertFalse(SessionRecovery.FAILED)

    def test_refresh_loop_tries_until_token_actually_rotates(self):
        class _Cookie:
            def __init__(self):
                self.value = "same"

        class _Session:
            def __init__(self):
                self.cookie = _Cookie()
                self.calls = []

            def get_cookie(self, _name):
                return self.cookie

            def get(self, url, headers=None):
                self.calls.append(url)
                if len(self.calls) == 2:
                    self.cookie.value = "new"
                return _Response(200, {})

        client = self._client()
        client.session = _Session()
        client._has_usable_access_cookie = Mock(return_value=True)

        self.assertTrue(client._refresh_cookie_session(require_new_token=True))
        self.assertEqual(len(client.session.calls), 2)

    def test_unchanged_token_is_force_validated(self):
        client = self._client()
        client._refresh_cookie_session = Mock(return_value=False)
        client._get_cookie_value = Mock(return_value="same")
        client._cookie_expires_soon = Mock(return_value=False)
        client._verify_cookie_session = Mock(return_value="profile")

        self.assertIs(client.refresh_session_token(), SessionRecovery.CURRENT_VALID)
        client._verify_cookie_session.assert_called_once_with(force=True)

    def test_forced_verification_bypasses_cached_account_id(self):
        client = self._client()
        client._fetch_dashboard = Mock(
            return_value=_Response(200, {"profileId": "fresh-profile"})
        )

        self.assertEqual(client._verify_cookie_session(), "cached-profile")
        client._fetch_dashboard.assert_not_called()
        self.assertEqual(
            client._verify_cookie_session(force=True),
            "fresh-profile",
        )
        client._fetch_dashboard.assert_called_once_with()
        self.assertEqual(client.account_id, "fresh-profile")

    def test_failed_forced_validation_marks_session_dead(self):
        client = self._client()
        client._refresh_cookie_session = Mock(return_value=False)
        client._get_cookie_value = Mock(return_value="same")
        client._cookie_expires_soon = Mock(return_value=False)
        client._verify_cookie_session = Mock(side_effect=NaukriAuthError("invalid"))

        self.assertIs(client.refresh_session_token(), SessionRecovery.FAILED)

    def test_rotated_token_is_saved_and_retryable(self):
        client = self._client()
        client._refresh_cookie_session = Mock(return_value=True)
        client._get_cookie_value = Mock(return_value="new")
        client._cookie_expires_soon = Mock(return_value=False)
        client.save_cookies = Mock()

        self.assertIs(client.refresh_session_token(), SessionRecovery.REFRESHED)
        self.assertEqual(client.naukri_session.bearer_token, "new")
        client.save_cookies.assert_called_once_with()


class ApplyLoopRecoveryTests(unittest.TestCase):
    def _agent(self, recovery):
        agent = EasyApplyAgent.__new__(EasyApplyAgent)
        agent.job_client = Mock()
        agent.job_client.refresh_auth.return_value = recovery
        agent._stop_run = False
        agent._consecutive_auth_failures = 0
        return agent

    def test_refreshed_token_retries_current_job_once(self):
        agent = self._agent(SessionRecovery.REFRESHED)
        agent._apply_core = Mock(side_effect=[NaukriAuthError("invalid"), True])
        stats = empty_stats()

        with patch("builtins.print"):
            result = agent._apply_one(JobLead(_job(), "search"), stats)

        self.assertTrue(result)
        self.assertEqual(agent._apply_core.call_count, 2)
        self.assertEqual(stats["failed"], 0)
        self.assertFalse(agent._stop_run)

    def test_two_current_valid_auth_rejections_stop_apply_requests(self):
        agent = self._agent(SessionRecovery.CURRENT_VALID)
        agent._apply_core = Mock(side_effect=NaukriAuthError("invalid"))
        stats = empty_stats()

        with patch("builtins.print"):
            agent._apply_one(JobLead(_job("1"), "search"), stats)
            self.assertFalse(agent._stop_run)
            agent._apply_one(JobLead(_job("2"), "search"), stats)

        self.assertTrue(agent._stop_run)
        self.assertEqual(stats["failed"], 2)

    def test_non_apply_skip_does_not_hide_repeated_auth_failures(self):
        agent = self._agent(SessionRecovery.CURRENT_VALID)
        agent._apply_core = Mock(
            side_effect=[NaukriAuthError("invalid"), True, NaukriAuthError("invalid")]
        )
        stats = empty_stats()

        with patch("builtins.print"):
            agent._apply_one(JobLead(_job("1"), "search"), stats)
            agent._apply_one(JobLead(_job("skip"), "search"), stats)
            agent._apply_one(JobLead(_job("2"), "search"), stats)

        self.assertTrue(agent._stop_run)
        self.assertEqual(stats["failed"], 2)

    def test_failed_recovery_stops_immediately(self):
        agent = self._agent(SessionRecovery.FAILED)
        agent._apply_core = Mock(side_effect=NaukriAuthError("invalid"))
        stats = empty_stats()

        with patch("builtins.print"):
            agent._apply_one(JobLead(_job(), "search"), stats)

        self.assertTrue(agent._stop_run)
        self.assertEqual(stats["failed"], 1)

    def test_run_does_not_touch_next_lead_after_stop_signal(self):
        agent = self._agent(SessionRecovery.FAILED)
        leads = [JobLead(_job("1"), "search"), JobLead(_job("2"), "search")]

        def stop_after_first(_lead, _stats):
            agent._stop_run = True
            return True

        agent.filter_applicable = Mock(return_value=leads)
        agent._apply_one = Mock(side_effect=stop_after_first)

        with (
            patch(
                "src.agents.easy_apply_agent.humanizer.light_shuffle",
                side_effect=lambda items, _drift: list(items),
            ),
            patch("src.agents.easy_apply_agent.humanizer.too_many_blocks", return_value=False),
            patch("builtins.print"),
        ):
            agent.run(leads, daily_remaining=10)

        self.assertEqual(agent._apply_one.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
