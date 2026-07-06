"""Job sources: fetch jobs from a source and hand them to EasyApplyAgent.

Both sources share the same shape — fetch, dedup against this run's seen
ids, print the found count, delegate to the apply agent — so they live as
two plain functions instead of two near-identical classes.
"""

from src.agents.job_utils import dedup_new_jobs, empty_stats
from src.config import agent_config as config
from src.utils import humanizer


def run_recommended(job_client, easy_apply_agent, seen_job_ids: set, daily_remaining: int) -> dict:
    """Fetch and apply recommended jobs from the profile feed."""
    print("\n🌟 Recommended jobs")
    print("🔎 Fetching from profile...")

    try:
        jobs = job_client.get_recommended_jobs()
    except Exception as exc:
        print(f"⚠️ Recommended fetch failed: {exc}")
        return empty_stats()

    new_jobs = dedup_new_jobs(jobs, seen_job_ids)
    print(f"📦 Found: {len(new_jobs)}")

    return easy_apply_agent.run(
        jobs=new_jobs,
        source="recommended",
        label="recommended",
        daily_remaining=daily_remaining,
    )


def run_search_term(job_client, easy_apply_agent, query: dict, seen_job_ids: set, daily_remaining: int) -> dict:
    """Fetch and apply jobs for one configured search term."""
    keyword = query["keyword"]
    location = query["location"] or "All India"

    print("\n🔎 Search jobs")
    print(f"🎯 Query: {keyword} | {location}")

    jobs = _fetch_search_jobs(job_client, query, seen_job_ids)
    print(f"📦 Found: {len(jobs)}")

    return easy_apply_agent.run(
        jobs=jobs,
        source="search",
        label=keyword,
        daily_remaining=daily_remaining,
    )


def _fetch_search_jobs(job_client, query: dict, seen_job_ids: set) -> list:
    all_jobs = []
    keyword = query["keyword"]

    for exp in config.EXPERIENCE_LEVELS:
        for page in range(1, config.SEARCH_PAGES + 1):
            try:
                jobs = job_client.search_jobs(
                    keyword=keyword,
                    location=query["location"],
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

            all_jobs.extend(dedup_new_jobs(jobs, seen_job_ids))

            if not jobs:
                break

            # Randomized pause between search pages.
            humanizer.human_delay(
                config.SEARCH_DELAY_MIN_SECONDS,
                config.SEARCH_DELAY_MAX_SECONDS,
            )

    return all_jobs
