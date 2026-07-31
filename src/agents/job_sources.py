"""Job sources: collect jobs from every source into one deduped pool.

These functions only FETCH. Nothing here applies to a job — the whole pool
is gathered first, then ranked, then applied to by EasyApplyAgent. That
split is what lets the daily budget go to the best jobs overall instead of
being burnt by whichever search term happened to run first.
"""

from src.agents.job_utils import dedup_new_jobs
from src.config import agent_config as config
from src.models.models import JobLead
from src.utils import humanizer


def collect_recommended(job_client, seen_job_ids: set) -> list[JobLead]:
    """Fetch recommended jobs from the profile feed (no applying)."""
    print("\n🌟 Recommended jobs")
    print("🔎 Fetching from profile...")

    try:
        jobs = job_client.get_recommended_jobs()
    except Exception as exc:
        print(f"⚠️ Recommended fetch failed: {exc}")
        return []

    new_jobs = dedup_new_jobs(jobs, seen_job_ids)
    print(f"📦 Found: {len(new_jobs)}")

    return [
        JobLead(job=job, source="recommended", label="recommended")
        for job in new_jobs
    ]


def collect_search_term(job_client, query: dict, seen_job_ids: set) -> list[JobLead]:
    """Fetch every page/experience level for one configured search term."""
    keyword = query["keyword"]
    location = query["location"] or "All India"

    print(f"\n🔎 Search: {keyword} | {location}")

    jobs = _fetch_search_jobs(job_client, query, seen_job_ids)
    print(f"📦 Found: {len(jobs)}")

    return [JobLead(job=job, source="search", label=keyword) for job in jobs]


def collect_all(job_client, seen_job_ids: set) -> list[JobLead]:
    """Run every enabled source and return one combined, deduped pool.

    Requests stay strictly sequential and paced — the pool exists to make
    better apply decisions, not to go faster. Parallelising here would
    expose the run as automated while the shared RequestPacer caps real
    throughput anyway.
    """
    leads: list[JobLead] = []

    if config.RUN_RECOMMENDED_PHASE:
        leads.extend(collect_recommended(job_client, seen_job_ids))
    else:
        print("\n⏭️ Recommended phase skipped by config.")

    if not config.RUN_SEARCH_PHASE:
        print("\n⏭️ Search phase skipped by config.")
        return leads

    # Shuffle query order each run so the request sequence is never
    # identical between runs.
    queries = config.SEARCH_QUERIES
    if config.SHUFFLE_SEARCH_QUERIES:
        queries = humanizer.shuffled(queries)

    for index, query in enumerate(queries):
        # A blocked account won't get better results by asking again.
        if humanizer.too_many_blocks():
            print("🛑 Too many server blocks this run — stopping collection.")
            break

        # Randomized pause between search terms (skip before first one).
        if index > 0:
            humanizer.human_delay(
                config.QUERY_DELAY_MIN_SECONDS,
                config.QUERY_DELAY_MAX_SECONDS,
            )

        leads.extend(collect_search_term(job_client, query, seen_job_ids))

    return leads


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
