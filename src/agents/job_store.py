import csv
from datetime import datetime
import os

from src.config import agent_config as config


def load_job_ids(csv_file: str) -> set:
    if not os.path.exists(csv_file):
        return set()

    with open(csv_file, "r", newline="", encoding="utf-8") as f:
        return {row["job_id"] for row in csv.DictReader(f) if row.get("job_id")}


def write_csv_row(csv_file: str, fieldnames: list[str], row: dict) -> None:
    file_exists = os.path.exists(csv_file)
    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def save_applied_job(job) -> None:
    write_csv_row(
        config.APPLIED_JOBS_CSV,
        ["job_id", "title", "company", "applied_at"],
        {
            "job_id": job.job_id,
            "title": job.title,
            "company": job.company,
            "applied_at": datetime.utcnow().isoformat(),
        },
    )


def normalise_job_link(job) -> str:
    link = (job.apply_link or "").strip()
    if link.startswith("/"):
        return f"https://www.naukri.com{link}"
    return link or f"https://www.naukri.com/job-listings-{job.job_id}"

