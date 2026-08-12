"""Offline regression tests for recent Naukri application-history sync."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.client.naukri_client import NaukriLoginClient
from src.config import agent_config as config
from src.exceptions.exceptions import NaukriAuthError, NaukriParseError


class _HistoryFixtureClient(NaukriLoginClient):
    """NaukriLoginClient without a session or network dependency."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_application_history(self, page_size=10, days=90, page_number=1,
                                mobile=False):
        self.calls.append((page_size, days, page_number, mobile))
        value = self.pages.get(page_number, {"matchingRowsCount": 0, "applyDetails": []})
        if isinstance(value, BaseException):
            raise value
        return value


def _item(job_id, *, status_value=None, status_id=None):
    status = {}
    if status_value is not None:
        status["statusValue"] = status_value
    if status_id is not None:
        status["statusId"] = status_id
    return {"jobId": job_id, "status": [status]}


class RecentApplicationHistoryTests(unittest.TestCase):

    def setUp(self):
        self.saved_config = (
            config.APPLICATION_HISTORY_SYNC_ENABLED,
            config.APPLICATION_HISTORY_DAYS,
            config.APPLICATION_HISTORY_PAGE_SIZE,
            config.APPLICATION_HISTORY_MAX_PAGES,
        )
        config.APPLICATION_HISTORY_SYNC_ENABLED = True
        config.APPLICATION_HISTORY_DAYS = 3
        config.APPLICATION_HISTORY_PAGE_SIZE = 2
        config.APPLICATION_HISTORY_MAX_PAGES = 5

    def tearDown(self):
        (
            config.APPLICATION_HISTORY_SYNC_ENABLED,
            config.APPLICATION_HISTORY_DAYS,
            config.APPLICATION_HISTORY_PAGE_SIZE,
            config.APPLICATION_HISTORY_MAX_PAGES,
        ) = self.saved_config

    def test_matching_rows_count_pages_and_deduplicates_normalised_ids(self):
        client = _HistoryFixtureClient({
            1: {
                "matchingRowsCount": "5",
                "applyDetails": [
                    _item(" 123 ", status_value=" Applied "),
                    _item(456, status_value="Application Sent"),
                ],
            },
            2: {
                "matchingRowsCount": 5,
                "applyDetails": [
                    _item("123", status_id=1),
                    _item("789", status_id="2"),
                ],
            },
            3: {
                "matchingRowsCount": 5,
                "applyDetails": [_item("999", status_value="Rejected", status_id=9)],
            },
        })

        self.assertEqual(client.get_recent_application_history(), {"123", "456", "789"})
        self.assertEqual(
            client.calls,
            [(2, 3, 1, False), (2, 3, 2, False), (2, 3, 3, False)],
        )

    def test_malformed_items_are_skipped_without_hiding_valid_siblings(self):
        config.APPLICATION_HISTORY_PAGE_SIZE = 100
        client = _HistoryFixtureClient({
            1: {
                "matchingRowsCount": 9,
                "applyDetails": [
                    None,
                    "bad",
                    {},
                    _item(True, status_id=1),
                    _item([], status_id=1),
                    _item("", status_id=1),
                    {"jobId": "wrong-status-shape", "status": {"statusId": 1}},
                    _item("value-match", status_value="  APPLICATION   SENT "),
                    _item("id-match", status_id=2),
                ],
            },
        })

        self.assertEqual(
            client.get_recent_application_history(),
            {"value-match", "id-match"},
        )

    def test_max_pages_caps_large_matching_count(self):
        config.APPLICATION_HISTORY_PAGE_SIZE = 1
        config.APPLICATION_HISTORY_MAX_PAGES = 2
        client = _HistoryFixtureClient({
            1: {"matchingRowsCount": 999, "applyDetails": [_item("1", status_id=1)]},
            2: {"matchingRowsCount": 999, "applyDetails": [_item("2", status_id=1)]},
            3: {"matchingRowsCount": 999, "applyDetails": [_item("3", status_id=1)]},
        })

        self.assertEqual(client.get_recent_application_history(), {"1", "2"})
        self.assertEqual([call[2] for call in client.calls], [1, 2])

    def test_missing_matching_count_uses_short_page_fallback(self):
        client = _HistoryFixtureClient({
            1: {"applyDetails": [_item("1", status_id=1), _item("2", status_id=1)]},
            2: {"applyDetails": [_item("3", status_id=1)]},
            3: {"applyDetails": [_item("4", status_id=1)]},
        })

        self.assertEqual(client.get_recent_application_history(), {"1", "2", "3"})
        self.assertEqual([call[2] for call in client.calls], [1, 2])

    def test_malformed_page_is_safe_and_valid_later_pages_still_count(self):
        config.APPLICATION_HISTORY_PAGE_SIZE = 1
        client = _HistoryFixtureClient({
            1: {"matchingRowsCount": 3, "applyDetails": {"not": "a list"}},
            2: {"matchingRowsCount": 3, "applyDetails": [_item("2", status_id=1)]},
            3: {"matchingRowsCount": 3, "applyDetails": [_item("3", status_id=2)]},
        })

        self.assertEqual(client.get_recent_application_history(), {"2", "3"})
        self.assertEqual([call[2] for call in client.calls], [1, 2, 3])

    def test_non_mapping_payload_is_skipped_safely(self):
        client = _HistoryFixtureClient({1: ["unexpected", "payload"]})

        self.assertEqual(client.get_recent_application_history(), set())
        self.assertEqual(len(client.calls), 1)

    def test_fetch_failure_propagates_for_caller_fail_soft(self):
        config.APPLICATION_HISTORY_PAGE_SIZE = 1
        client = _HistoryFixtureClient({
            1: {"matchingRowsCount": 2, "applyDetails": [_item("1", status_id=1)]},
            2: NaukriParseError("history page failed"),
        })

        with self.assertRaisesRegex(NaukriParseError, "history page failed"):
            client.get_recent_application_history()

    def test_disabled_sync_makes_no_request(self):
        config.APPLICATION_HISTORY_SYNC_ENABLED = False
        client = _HistoryFixtureClient({1: RuntimeError("must not be called")})

        self.assertEqual(client.get_recent_application_history(), set())
        self.assertEqual(client.calls, [])

    def test_invalid_config_raises_before_request(self):
        config.APPLICATION_HISTORY_DAYS = 0
        client = _HistoryFixtureClient({})

        with self.assertRaisesRegex(ValueError, "positive integers"):
            client.get_recent_application_history()
        self.assertEqual(client.calls, [])


class ExistingApplicationHistoryCompatibilityTests(unittest.TestCase):

    def test_get_application_history_still_returns_raw_json(self):
        payload = {"matchingRowsCount": 1, "applyDetails": [{"jobId": "123"}]}

        class _Response:
            ok = True
            status_code = 200

            @staticmethod
            def json():
                return payload

        client = NaukriLoginClient.__new__(NaukriLoginClient)
        client.naukri_session = object()
        calls = []
        client._fetch_history_request = lambda page_size, days, page_number, mobile=False: (
            calls.append((page_size, days, page_number, mobile)) or _Response()
        )

        self.assertIs(
            client.get_application_history(page_size=7, days=11, page_number=3, mobile=True),
            payload,
        )
        self.assertEqual(calls, [(7, 11, 3, True)])

    def test_get_application_history_still_requires_login(self):
        client = NaukriLoginClient.__new__(NaukriLoginClient)
        client.naukri_session = None

        with self.assertRaisesRegex(NaukriAuthError, "Login first"):
            client.get_application_history()


if __name__ == "__main__":
    unittest.main(verbosity=2)
