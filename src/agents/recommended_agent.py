from src.agents.job_utils import dedup_new_jobs, empty_stats


class RecommendedJobAgent:
    """Fetches and applies recommended jobs."""

    def __init__(self, job_client, easy_apply_agent, seen_job_ids: set) -> None:
        self.job_client = job_client
        self.easy_apply_agent = easy_apply_agent
        self.seen_job_ids = seen_job_ids

    def run(self, daily_remaining: int) -> dict:
        print("\n🌟 Recommended jobs")
        print("🔎 Fetching from profile...")

        try:
            jobs = self.job_client.get_recommended_jobs()
        except Exception as exc:
            print(f"⚠️ Recommended fetch failed: {exc}")
            return empty_stats()

        new_jobs = dedup_new_jobs(jobs, self.seen_job_ids)
        print(f"📦 Found: {len(new_jobs)}")

        return self.easy_apply_agent.run(
            jobs=new_jobs,
            source="recommended",
            label="recommended",
            daily_remaining=daily_remaining,
        )

