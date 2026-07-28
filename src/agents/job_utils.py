STATS_KEYS = (
    "found",
    "attempted",
    "applied",
    "skipped_ext",
    "skipped_applied",
    "skipped_blocked",
    "skipped_browse",
    "failed",
)


def empty_stats(found: int = 0) -> dict:
    stats = {key: 0 for key in STATS_KEYS}
    stats["found"] = found
    return stats


def add_stats(totals: dict, phase_stats: dict) -> None:
    for key in STATS_KEYS:
        totals[key] += phase_stats[key]


def job_label(job) -> str:
    title = (job.title or "Untitled").strip()
    company = (job.company or "Unknown").strip()
    return f"{title} @ {company}"


def dedup_new_jobs(jobs: list, seen_ids: set) -> list:
    new_jobs = []
    for job in jobs:
        if job.job_id and job.job_id not in seen_ids:
            seen_ids.add(job.job_id)
            new_jobs.append(job)
    return new_jobs

