"""Focused self-checks for strict apply-workflow response parsing."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.apply_response import (
    ApplyStatus,
    normalize_job_id,
    parse_apply_response,
)


class ApplyResponseTests(unittest.TestCase):
    JOB_ID = "001234567890"

    def assert_status(self, response, expected, **kwargs):
        outcome = parse_apply_response(response, self.JOB_ID, **kwargs)
        self.assertIs(outcome.status, expected, outcome)
        self.assertEqual(outcome.job_id, self.JOB_ID)
        return outcome

    def test_job_id_normalization_preserves_leading_zeroes(self):
        self.assertEqual(normalize_job_id(" 001234567890 "), self.JOB_ID)
        self.assertEqual(normalize_job_id(123), "123")
        self.assertEqual(normalize_job_id(None), "")
        self.assertEqual(normalize_job_id(True), "")

    def test_missing_or_empty_jobs_fail_closed_as_unknown(self):
        self.assert_status({}, ApplyStatus.UNKNOWN)
        self.assert_status({"success": True}, ApplyStatus.UNKNOWN)
        self.assert_status({"jobs": []}, ApplyStatus.UNKNOWN)
        self.assert_status({"jobs": "not-a-list"}, ApplyStatus.UNKNOWN)

    def test_explicit_false_and_error_shapes_fail(self):
        cases = (
            {"success": False, "jobs": [{"jobId": self.JOB_ID}]},
            {"success": "false", "jobs": [{"jobId": self.JOB_ID}]},
            {"success": 0, "jobs": [{"jobId": self.JOB_ID}]},
            {"errors": [{"message": "profile incomplete"}], "jobs": []},
            {"validationErrors": ["missing answer"], "jobs": []},
            {"statusCode": 500, "jobs": [{"jobId": self.JOB_ID}]},
            {"jobs": [{"jobId": self.JOB_ID, "statusCode": 302}]},
            {"jobs": [{"jobId": self.JOB_ID, "status": "failed"}]},
            {"jobs": [{"jobId": self.JOB_ID, "status": "not applied"}]},
            {"jobs": [{"jobId": self.JOB_ID, "message": "Application unsuccessful"}]},
            {"jobs": [{"jobId": self.JOB_ID, "message": "Application was not successful"}]},
            {"jobs": [{"jobId": self.JOB_ID, "message": "Application not completed"}]},
            {"jobs": [{"jobId": self.JOB_ID, "message": "Application did not succeed"}]},
            {"jobs": [{"jobId": self.JOB_ID, "message": "Application was not successfully completed"}]},
            {"jobs": [{"jobId": self.JOB_ID, "message": "Application has not been applied"}]},
            {"jobs": [{"jobId": self.JOB_ID, "message": "No success"}]},
            {"jobs": [{"jobId": self.JOB_ID, "message": "Application cannot be completed"}]},
            {"jobs": [{"jobId": self.JOB_ID, "message": "Application was not a success"}]},
            {"jobs": [{"jobId": self.JOB_ID, "message": "Application cannot be submitted"}]},
        )
        for response in cases:
            with self.subTest(response=response):
                self.assert_status(response, ApplyStatus.FAILED)

    def test_no_error_status_code_zero_is_not_a_failure(self):
        """``statusCode: 0`` is the gateway's "no error" sentinel, not an HTTP code.

        Grading it like an HTTP status marked every apply-workflow response
        FAILED before the jobs list was even read, so a full run recorded zero
        applications ("top-level failure signal: statusCode=0").
        """
        cases = (
            {"statusCode": 0, "jobs": [{"jobId": self.JOB_ID}]},
            {"jobs": [{"jobId": self.JOB_ID, "statusCode": 0}]},
            {"statusCode": 0, "success": True, "jobs": [{"jobId": self.JOB_ID}]},
            # Shape seen in production: a sibling applyStatus object alongside
            # the sentinel, previously reported as "applyStatus=present".
            {
                "statusCode": 0,
                "applyStatus": {self.JOB_ID: {"status": "APPLIED"}},
                "jobs": [{"jobId": self.JOB_ID}],
            },
        )
        for response in cases:
            with self.subTest(response=response):
                self.assert_status(response, ApplyStatus.APPLIED)

    def test_status_code_zero_never_masks_a_real_failure(self):
        cases = (
            {"statusCode": 0, "success": False, "jobs": [{"jobId": self.JOB_ID}]},
            {"statusCode": 0, "error": "Invalid User", "jobs": [{"jobId": self.JOB_ID}]},
            {"statusCode": 0, "jobs": [{"jobId": self.JOB_ID, "status": "failed"}]},
        )
        for response in cases:
            with self.subTest(response=response):
                self.assert_status(response, ApplyStatus.FAILED)

    def test_status_code_zero_still_yields_questionnaire_and_already_applied(self):
        questions = [{"questionId": "1", "title": "Notice period?"}]
        outcome = self.assert_status(
            {"statusCode": 0, "jobs": [{"jobId": self.JOB_ID, "questionnaire": questions}]},
            ApplyStatus.QUESTIONNAIRE,
        )
        self.assertEqual(outcome.questionnaire, questions)
        self.assert_status(
            {
                "statusCode": 0,
                "jobs": [{"jobId": self.JOB_ID, "message": "You have already applied"}],
            },
            ApplyStatus.ALREADY_APPLIED,
        )

    def test_failure_reason_preserves_sanitized_server_detail(self):
        outcome = self.assert_status(
            {
                "success": False,
                "statusCode": 422,
                "message": "Profile requirement not met",
                "jobs": [{"jobId": self.JOB_ID}],
            },
            ApplyStatus.FAILED,
        )
        self.assertIn("statusCode=422", outcome.reason)
        self.assertIn("success=false", outcome.reason)
        self.assertIn("message=Profile requirement not met", outcome.reason)

    def test_already_applied_is_distinct_from_failure(self):
        self.assert_status(
            {"success": False, "error": "You have already applied for this job"},
            ApplyStatus.ALREADY_APPLIED,
        )
        self.assert_status(
            {"jobs": [{"jobId": self.JOB_ID, "message": "Application already exists"}]},
            ApplyStatus.ALREADY_APPLIED,
        )
        # Historical local wording was ambiguous and must not be interpreted as
        # server confirmation of an existing application.
        self.assert_status(
            {"error": "Already applied or failed"},
            ApplyStatus.FAILED,
        )

    def test_requested_job_is_selected_instead_of_first_entry(self):
        outcome = self.assert_status(
            {
                "jobs": [
                    {"jobId": "999999999999", "status": "failed"},
                    {"jobId": " 001234567890 ", "status": "applied"},
                ]
            },
            ApplyStatus.APPLIED,
        )
        self.assertTrue(outcome.is_applied)

    def test_wrong_job_id_never_inherits_top_level_success(self):
        self.assert_status(
            {"success": True, "jobs": [{"jobId": "999999999999"}]},
            ApplyStatus.UNKNOWN,
        )

    def test_questionnaire_is_intermediate_only(self):
        questions = [{"questionId": "q1", "questionName": "Experience?"}]
        outcome = self.assert_status(
            {"jobs": [{"jobId": self.JOB_ID, "questionnaire": questions}]},
            ApplyStatus.QUESTIONNAIRE,
        )
        self.assertEqual(outcome.questionnaire, questions)

        final = self.assert_status(
            {"jobs": [{"jobId": self.JOB_ID, "questionnaire": questions}]},
            ApplyStatus.FAILED,
            final_submission=True,
        )
        self.assertIsNone(final.questionnaire)

    def test_job_level_failure_beats_top_level_success(self):
        self.assert_status(
            {
                "success": True,
                "jobs": [{"jobId": self.JOB_ID, "success": False}],
            },
            ApplyStatus.FAILED,
        )

    def test_positive_signals_and_final_success(self):
        cases = (
            {"success": True, "jobs": [{"jobId": self.JOB_ID}]},
            {"jobs": [{"jobId": self.JOB_ID, "status": "Application Sent"}]},
            {"jobs": [{"jobId": self.JOB_ID, "statusCode": 200}]},
            {"jobs": [{"status": "applied"}]},
            {
                "jobs": [{
                    "jobId": self.JOB_ID,
                    "message": "Application completed successfully. Do not submit again.",
                }]
            },
            {
                "jobs": [{
                    "jobId": self.JOB_ID,
                    "message": "Successfully applied. Please do not apply again.",
                }]
            },
            {
                "jobs": [{
                    "jobId": self.JOB_ID,
                    "message": "Application sent. You cannot apply twice.",
                }]
            },
        )
        for response in cases:
            with self.subTest(response=response):
                self.assert_status(response, ApplyStatus.APPLIED)

        self.assert_status(
            {"jobs": [{"jobId": self.JOB_ID, "success": True}]},
            ApplyStatus.APPLIED,
            final_submission=True,
        )

    def test_safe_compatibility_requires_exact_matching_item(self):
        self.assert_status(
            {"jobs": [{"jobId": self.JOB_ID}]},
            ApplyStatus.APPLIED,
        )
        self.assert_status({"jobs": [{}]}, ApplyStatus.UNKNOWN)
        self.assert_status(
            {"jobs": [{"jobId": "999999999999"}]},
            ApplyStatus.UNKNOWN,
        )

    def test_unrecognized_workflow_text_never_uses_success_fallback(self):
        cases = (
            {"jobs": [{"jobId": self.JOB_ID, "message": "Pending manual review"}]},
            {"message": "Pending manual review", "jobs": [{"jobId": self.JOB_ID}]},
            {
                "success": True,
                "jobs": [{"jobId": self.JOB_ID, "message": "Pending manual review"}],
            },
        )
        for response in cases:
            with self.subTest(response=response):
                self.assert_status(response, ApplyStatus.UNKNOWN)

    def test_invalid_requested_job_id_fails(self):
        outcome = parse_apply_response(
            {"jobs": [{"jobId": self.JOB_ID}]},
            "   ",
        )
        self.assertIs(outcome.status, ApplyStatus.FAILED)
        self.assertEqual(outcome.job_id, "")


if __name__ == "__main__":
    unittest.main()
