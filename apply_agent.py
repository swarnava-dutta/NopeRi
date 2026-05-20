from datetime import datetime
import csv
import os
import time

from src.client.naukri_client import NaukriLoginClient
from src.client.job_client import NaukriJobClient
from src.config import agent_config as config


CSV_FILE = config.APPLIED_JOBS_CSV


def load_applied_jobs() -> set:
    if not os.path.exists(CSV_FILE):
        return set()
    with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
        return {row["job_id"] for row in csv.DictReader(f) if row.get("job_id")}


def save_applied_job(job) -> None:
    file_exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["job_id", "title", "company", "applied_at"])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "job_id": job.job_id,
            "title": job.title,
            "company": job.company,
            "applied_at": datetime.utcnow().isoformat(),
        })


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


def fetch_recommended_jobs(jc: NaukriJobClient, seen_ids: set) -> list:
    print("Fetching recommended jobs from profile...")
    try:
        jobs = jc.get_recommended_jobs()
    except Exception as exc:
        print(f"Recommended jobs fetch failed: {exc}")
        return []

    new_jobs = dedup_new_jobs(jobs, seen_ids)
    print(f"Recommended jobs found: {len(new_jobs)}")
    return new_jobs


def fetch_search_term_jobs(jc: NaukriJobClient, query: dict, seen_ids: set) -> list:
    keyword = query["keyword"]
    location = query["location"] or "All India"
    print(f"\nSearching: {keyword} | {location}")

    all_jobs = []
    for exp in config.EXPERIENCE_LEVELS:
        for page in range(1, config.SEARCH_PAGES + 1):
            try:
                jobs = jc.search_jobs(
                    keyword=keyword,
                    location=query["location"],
                    experience=exp,
                    job_age=config.JOB_AGE_DAYS,
                    page=page,
                )
            except Exception as exc:
                print(f"Search failed: {keyword} | exp={exp} | page={page} | {exc}")
                time.sleep(config.SEARCH_ERROR_DELAY_SECONDS)
                continue

            new_jobs = dedup_new_jobs(jobs, seen_ids)
            all_jobs.extend(new_jobs)

            if not jobs:
                break

            time.sleep(config.SEARCH_DELAY_SECONDS)

    print(f"Search jobs found: {len(all_jobs)}")
    return all_jobs


def apply_jobs(
    jc: NaukriJobClient,
    jobs: list,
    source: str,
    label: str,
    applied_jobs_set: set,
    daily_remaining: int,
) -> dict:
    stats = {
        "found": len(jobs),
        "attempted": 0,
        "applied": 0,
        "skipped_ext": 0,
        "skipped_applied": 0,
        "failed": 0,
    }

    if daily_remaining <= 0:
        print("Daily apply limit reached.")
        return stats

    pending_jobs = []
    for job in jobs:
        if job.job_id in applied_jobs_set:
            stats["skipped_applied"] += 1
        else:
            pending_jobs.append(job)

    if not pending_jobs:
        print(f"No new {label} jobs to apply.")
        return stats

    print(f"Applying {label}: {len(pending_jobs)} jobs")
    for index, job in enumerate(pending_jobs, start=1):
        if stats["applied"] >= daily_remaining:
            print("Daily apply limit reached.")
            break

        prefix = f"{index}/{len(pending_jobs)} {job_label(job)}"

        try:
            if jc.is_external_apply(job.job_id):
                stats["skipped_ext"] += 1
                print(f"{prefix} - skipped external")
                continue

            mandatory = job.tags[:config.MANDATORY_SKILL_COUNT] if job.tags else []
            optional = (
                job.tags[config.MANDATORY_SKILL_COUNT:]
                if len(job.tags) > config.MANDATORY_SKILL_COUNT
                else []
            )

            stats["attempted"] += 1
            result = jc.apply_job(
                job,
                mandatory_skills=mandatory,
                optional_skills=optional,
                source=source,
            )

            job_result = (result.get("jobs") or [{}])[0]
            if job_result.get("questionnaire"):
                sid = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "0000000"
                jc.handle_static_questionnaire_and_apply(
                    job,
                    questionnaire=job_result["questionnaire"],
                    sid=sid,
                    mandatory_skills=mandatory,
                    optional_skills=optional,
                    source=source,
                )

            save_applied_job(job)
            applied_jobs_set.add(job.job_id)
            stats["applied"] += 1
            print(f"{prefix} - applied")

        except Exception as exc:
            stats["failed"] += 1
            print(f"{prefix} - failed: {exc}")

        time.sleep(config.APPLY_DELAY_SECONDS)

    return stats


def add_stats(totals: dict, phase_stats: dict) -> None:
    for key in totals:
        totals[key] += phase_stats[key]


def print_summary(totals: dict) -> None:
    print("\nDone")
    print(f"Fetched: {totals['found']}")
    print(f"Attempted: {totals['attempted']}")
    print(f"Applied: {totals['applied']}")
    print(f"Skipped already applied: {totals['skipped_applied']}")
    print(f"Skipped external: {totals['skipped_ext']}")
    print(f"Failed: {totals['failed']}")


if __name__ == "__main__":
    print("Logging in...")
    client = NaukriLoginClient()
    client.login()
    print("Login successful")

    jc = NaukriJobClient(client)
    seen_job_ids = set()
    applied_jobs_set = load_applied_jobs()
    totals = {
        "found": 0,
        "attempted": 0,
        "applied": 0,
        "skipped_ext": 0,
        "skipped_applied": 0,
        "failed": 0,
    }

    recommended_jobs = fetch_recommended_jobs(jc, seen_job_ids)
    add_stats(
        totals,
        apply_jobs(
            jc=jc,
            jobs=recommended_jobs,
            source="recommended",
            label="recommended",
            applied_jobs_set=applied_jobs_set,
            daily_remaining=config.DAILY_APPLY_LIMIT - totals["applied"],
        ),
    )

    for query in config.SEARCH_QUERIES:
        if totals["applied"] >= config.DAILY_APPLY_LIMIT:
            print("Daily apply limit reached. Stopping search.")
            break

        search_jobs = fetch_search_term_jobs(jc, query, seen_job_ids)
        add_stats(
            totals,
            apply_jobs(
                jc=jc,
                jobs=search_jobs,
                source="search",
                label=query["keyword"],
                applied_jobs_set=applied_jobs_set,
                daily_remaining=config.DAILY_APPLY_LIMIT - totals["applied"],
            ),
        )

    print_summary(totals)
