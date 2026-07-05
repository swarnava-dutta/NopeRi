from src.agents.job_utils import dedup_new_jobs
from src.config import agent_config as config
from src.utils import humanizer


class SearchTermApplyAgent:
    """Fetches and applies jobs for one configured search term."""

    def __init__(self, job_client, easy_apply_agent, query: dict, seen_job_ids: set) -> None:
        self.job_client = job_client
        self.easy_apply_agent = easy_apply_agent
        self.query = query
        self.seen_job_ids = seen_job_ids

    def run(self, daily_remaining: int) -> dict:
        keyword = self.query["keyword"]
        location = self.query["location"] or "All India"

        print("\n🔎 Search jobs")
        print(f"🎯 Query: {keyword} | {location}")

        jobs = self._fetch_jobs()
        print(f"📦 Found: {len(jobs)}")

        return self.easy_apply_agent.run(
            jobs=jobs,
            source="search",
            label=keyword,
            daily_remaining=daily_remaining,
        )

    def _fetch_jobs(self) -> list:
        all_jobs = []
        keyword = self.query["keyword"]

        for exp in config.EXPERIENCE_LEVELS:
            for page in range(1, config.SEARCH_PAGES + 1):
                try:
                    jobs = self.job_client.search_jobs(
                        keyword=keyword,
                        location=self.query["location"],
                        experience=exp,
                        job_age=config.JOB_AGE_DAYS,
                        page=page,
                    )
                except Exception as exc:
                    print(f"⚠️ Search failed: {keyword} | exp={exp} | page={page} | {exc}")
                    humanizer.human_delay(
                        config.SEARCH_ERROR_DELAY_MIN_SECONDS,
                        config.SEARCH_ERROR_DELAY_MAX_SECONDS,
                    )
                    continue

                all_jobs.extend(dedup_new_jobs(jobs, self.seen_job_ids))

                if not jobs:
                    break

                # Randomized pause between search pages.
                humanizer.human_delay(
                    config.SEARCH_DELAY_MIN_SECONDS,
                    config.SEARCH_DELAY_MAX_SECONDS,
                )

        return all_jobs

