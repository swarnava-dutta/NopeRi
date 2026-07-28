from src.agents.job_store import load_job_ids, save_applied_job
from src.agents.job_utils import empty_stats, job_label
from src.config import agent_config as config
from src.utils import humanizer


class EasyApplyAgent:
    """Applies Naukri easy-apply jobs and skips external company links."""

    def __init__(self, job_client, external_link_agent) -> None:
        self.job_client = job_client
        self.external_link_agent = external_link_agent
        self.applied_job_ids = load_job_ids(config.APPLIED_JOBS_CSV)

    @staticmethod
    def _is_blocked_company(job) -> bool:
        company = (job.company or "").lower()
        return any(blocked in company for blocked in config.BLOCKED_COMPANIES)

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
            elif self._is_blocked_company(job):
                stats["skipped_blocked"] += 1
                print(f"🚫 Blocked company: {job_label(job)}")
            else:
                pending_jobs.append(job)

        if not pending_jobs:
            print(f"ℹ️ No new {label} jobs to apply.")
            return stats

        # Humans don't work strictly top-to-bottom through search results —
        # let each job drift a few positions in the apply order.
        pending_jobs = humanizer.light_shuffle(pending_jobs, config.JOB_ORDER_DRIFT)

        print(f"🚀 Applying {label}: {len(pending_jobs)} jobs")

        for job in pending_jobs:
            if stats["applied"] >= daily_remaining:
                print("🛑 Daily apply limit reached.")
                break

            # Abort the run entirely if the server keeps pushing back —
            # continuing turns a soft block into a real ban.
            if humanizer.too_many_blocks():
                print("🛑 Too many server blocks this run — stopping to protect the account.")
                break

            self._apply_one(job, source, stats)
            humanizer.note_job()  # feeds the session-fatigue slow-down

            # Randomized human-like pause between applies, plus occasional
            # long "walked away" breaks.
            humanizer.human_delay(config.APPLY_DELAY_MIN_SECONDS, config.APPLY_DELAY_MAX_SECONDS)
            humanizer.maybe_long_break()

        return stats

    def _apply_one(self, job, source: str, stats: dict) -> None:
        label_text = job_label(job)

        try:
            self._apply_core(job, source, stats, label_text)
        except Exception as exc:
            # A rotated/expired nauk_at mid-run surfaces as 403 "Invalid
            # User". Refresh the session token and retry this job once —
            # it's a stale credential, not a problem with the job.
            if "invalid user" in str(exc).lower() and self.job_client.refresh_auth():
                print(f"🔄 Session token refreshed — retrying: {label_text}")
                try:
                    self._apply_core(job, source, stats, label_text)
                    return
                except Exception as retry_exc:
                    exc = retry_exc
            stats["failed"] += 1
            print(f"⚠️ Failed: {label_text} | {exc}")

    def _apply_core(self, job, source: str, stats: dict, label_text: str) -> None:
        details = self.job_client.get_job_details(job.job_id)
        if self.job_client.is_external(details):
            stats["skipped_ext"] += 1
            self.external_link_agent.document(job, source)
            print(f"❌ External link: {label_text}")
            return

        # Human "reads" the job description before hitting apply —
        # longer JDs take proportionally longer.
        jd_text = (details.get("job") or {}).get("description") or job.description or ""
        humanizer.reading_pause(len(jd_text))

        # Sometimes a human opens a job, reads it, and just moves on.
        # The job stays eligible for a future run and costs no budget.
        if humanizer.window_shopping():
            stats["skipped_browse"] += 1
            print(f"👀 Browsed only: {label_text}")
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
        questionnaire_answers = []
        if job_result.get("questionnaire"):
            # Humans take time to fill in a questionnaire.
            humanizer.human_delay(2.0, 8.0)
            sid = humanizer.generate_sid()
            questionnaire_result = self.job_client.handle_static_questionnaire_and_apply(
                job,
                questionnaire=job_result["questionnaire"],
                sid=sid,
                mandatory_skills=mandatory,
                optional_skills=optional,
                source=source,
            )
            questionnaire_answers = questionnaire_result.get("_questionnaire_answers") or []
            if questionnaire_result.get("success") is False:
                error = questionnaire_result.get("error") or "unknown questionnaire error"
                raise RuntimeError(f"Questionnaire apply failed: {error}")

        save_applied_job(job, questionnaire_answers=questionnaire_answers)
        self.applied_job_ids.add(job.job_id)
        stats["applied"] += 1
        print(f"✅ Applied: {label_text}")
