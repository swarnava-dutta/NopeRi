from datetime import datetime
import time

from src.agents.job_store import load_job_ids, save_applied_job
from src.agents.job_utils import empty_stats, job_label
from src.config import agent_config as config


class EasyApplyAgent:
    """Applies Naukri easy-apply jobs and skips external company links."""

    def __init__(self, job_client, external_link_agent) -> None:
        self.job_client = job_client
        self.external_link_agent = external_link_agent
        self.applied_job_ids = load_job_ids(config.APPLIED_JOBS_CSV)

    def run(self, jobs: list, source: str, label: str, daily_remaining: int) -> dict:
        stats = empty_stats(found=len(jobs))

        if daily_remaining <= 0:
            print("🛑 Daily apply limit reached.")
            return stats

        pending_jobs = []
        for job in jobs:
            if job.job_id in self.applied_job_ids:
                stats["skipped_applied"] += 1
                print(f"⏭️ Already applied: {job_label(job)}")
            else:
                pending_jobs.append(job)

        if not pending_jobs:
            print(f"ℹ️ No new {label} jobs to apply.")
            return stats

        print(f"🚀 Applying {label}: {len(pending_jobs)} jobs")

        for job in pending_jobs:
            if stats["applied"] >= daily_remaining:
                print("🛑 Daily apply limit reached.")
                break

            self._apply_one(job, source, stats)
            time.sleep(config.APPLY_DELAY_SECONDS)

        return stats

    def _apply_one(self, job, source: str, stats: dict) -> None:
        label_text = job_label(job)

        try:
            if self.job_client.is_external_apply(job.job_id):
                stats["skipped_ext"] += 1
                self.external_link_agent.document(job, source)
                print(f"❌ External link: {label_text}")
                return

            mandatory = job.tags[:config.MANDATORY_SKILL_COUNT] if job.tags else []
            optional = (
                job.tags[config.MANDATORY_SKILL_COUNT:]
                if len(job.tags) > config.MANDATORY_SKILL_COUNT
                else []
            )

            stats["attempted"] += 1
            result = self.job_client.apply_job(
                job,
                mandatory_skills=mandatory,
                optional_skills=optional,
                source=source,
            )

            job_result = (result.get("jobs") or [{}])[0]
            if job_result.get("questionnaire"):
                sid = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "0000000"
                self.job_client.handle_static_questionnaire_and_apply(
                    job,
                    questionnaire=job_result["questionnaire"],
                    sid=sid,
                    mandatory_skills=mandatory,
                    optional_skills=optional,
                    source=source,
                )

            save_applied_job(job)
            self.applied_job_ids.add(job.job_id)
            stats["applied"] += 1
            print(f"✅ Applied: {label_text}")

        except Exception as exc:
            stats["failed"] += 1
            print(f"⚠️ Failed: {label_text} | {exc}")

