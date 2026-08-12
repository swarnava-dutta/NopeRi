import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.easy_apply_agent import EasyApplyAgent
from src.agents.job_utils import empty_stats
from src.config import agent_config as config
from src.models.models import Job, JobLead


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
        description="Build LLM applications.",
    )


class _Client:
    def __init__(self, first, final=None):
        self.first = first
        self.final = final
        self.questionnaire_calls = 0

    def get_job_details(self, job_id):
        return {"job": {"description": "Build agentic AI systems."}}

    @staticmethod
    def is_external(details):
        return False

    def apply_job(self, job, **kwargs):
        return self.first

    def handle_static_questionnaire_and_apply(self, job, **kwargs):
        self.questionnaire_calls += 1
        return self.final


class EasyApplyCaptureTests(unittest.TestCase):
    def _agent(self, client):
        agent = EasyApplyAgent.__new__(EasyApplyAgent)
        agent.job_client = client
        agent.external_link_agent = None
        agent.applied_job_ids = set()
        agent._role_verdict = lambda *_args: True
        return agent

    def _run_core(self, first, final=None):
        client = _Client(first, final)
        agent = self._agent(client)
        job = _job()
        stats = empty_stats()
        saved = []
        with (
            patch("src.agents.easy_apply_agent.save_applied_job", side_effect=lambda *a, **k: saved.append((a, k))),
            patch("src.agents.easy_apply_agent.humanizer.reading_pause"),
            patch("src.agents.easy_apply_agent.humanizer.human_delay"),
            patch("builtins.print"),
        ):
            result = agent._apply_core(JobLead(job, "search"), stats, "test job")
        return result, agent, client, stats, saved

    def test_confirmed_direct_apply_is_saved_once(self):
        _, agent, _, stats, saved = self._run_core(
            {"success": True, "jobs": [{"jobId": "1001"}]}
        )
        self.assertEqual(len(saved), 1)
        self.assertEqual(stats["applied"], 1)
        self.assertIn("1001", agent.applied_job_ids)

    def test_ambiguous_and_failed_200_responses_are_not_saved(self):
        cases = (
            {},
            {"jobs": []},
            {"success": False, "jobs": [{"jobId": "1001"}]},
            {"jobs": [{"jobId": "1001", "error": "denied"}]},
        )
        for response in cases:
            with self.subTest(response=response):
                with self.assertRaisesRegex(RuntimeError, "Apply not confirmed"):
                    self._run_core(response)

    def test_already_applied_updates_memory_but_not_csv_or_new_count(self):
        _, agent, _, stats, saved = self._run_core(
            {"error": "You have already applied for this job"}
        )
        self.assertEqual(saved, [])
        self.assertIn("1001", agent.applied_job_ids)
        self.assertEqual(stats["skipped_applied"], 1)
        self.assertEqual(stats["applied"], 0)

    def test_questionnaire_requires_confirmed_final_response(self):
        questions = [{"questionId": "q1", "questionName": "Experience?"}]
        first = {"jobs": [{"jobId": "1001", "questionnaire": questions}]}
        final = {
            "success": True,
            "jobs": [{"jobId": "1001"}],
            "_questionnaire_answers": [{"questionId": "q1", "answer": "5"}],
        }
        _, agent, client, stats, saved = self._run_core(first, final)
        self.assertEqual(client.questionnaire_calls, 1)
        self.assertEqual(len(saved), 1)
        self.assertEqual(stats["applied"], 1)
        self.assertIn("1001", agent.applied_job_ids)
        self.assertEqual(
            saved[0][1]["questionnaire_answers"],
            final["_questionnaire_answers"],
        )

        with self.assertRaisesRegex(RuntimeError, "Apply not confirmed"):
            self._run_core(first, {"success": False, "jobs": [{"jobId": "1001"}]})

    def test_csv_failure_does_not_mark_job_applied_in_memory(self):
        client = _Client({"success": True, "jobs": [{"jobId": "1001"}]})
        agent = self._agent(client)
        stats = empty_stats()
        with (
            patch("src.agents.easy_apply_agent.save_applied_job", side_effect=OSError("disk full")),
            patch("src.agents.easy_apply_agent.humanizer.reading_pause"),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                agent._apply_core(JobLead(_job(), "search"), stats, "test job")
        self.assertNotIn("1001", agent.applied_job_ids)
        self.assertEqual(stats["applied"], 0)

    def test_constructor_unions_local_and_server_history_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "applied.csv")
            with open(path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["job_id", "title"])
                writer.writerow(["local", "one"])

            with patch.object(config, "APPLIED_JOBS_CSV", path):
                agent = EasyApplyAgent(object(), object(), {" server ", "local"})
        self.assertEqual(agent.applied_job_ids, {"local", "server"})

    def test_external_counters_match_document_result(self):
        class _External:
            def __init__(self):
                self.results = iter(("written", "updated", "duplicate", "disabled"))

            def document(self, job, source):
                return next(self.results)

        agent = EasyApplyAgent.__new__(EasyApplyAgent)
        agent.external_link_agent = _External()
        stats = empty_stats()
        with patch("builtins.print"):
            for _ in range(4):
                agent._document_external(_job(), "search", stats, "test")

        self.assertEqual(stats["skipped_ext"], 4)
        self.assertEqual(stats["external_written"], 1)
        self.assertEqual(stats["external_updated"], 1)
        self.assertEqual(stats["external_duplicate"], 1)
        self.assertEqual(stats["external_unsaved"], 1)


if __name__ == "__main__":
    unittest.main()
