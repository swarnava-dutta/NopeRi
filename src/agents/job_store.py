"""CSV persistence for applied and documented jobs."""

import csv
import json
import os
from datetime import datetime, timezone

from src.config import agent_config as config


def utc_timestamp() -> str:
    """ISO-8601 UTC timestamp used in every CSV row."""
    return datetime.now(timezone.utc).isoformat()


def load_job_ids(csv_file: str) -> set:
    if not os.path.exists(csv_file):
        return set()

    with open(csv_file, "r", newline="", encoding="utf-8") as f:
        return {row["job_id"] for row in csv.DictReader(f) if row.get("job_id")}


def write_csv_row(csv_file: str, fieldnames: list[str], row: dict) -> None:
    """Append one row, writing the header first if the file is new/empty.

    ponytail: no column migration. If you add a field to one of the row
    schemas below, add the column to the existing CSV by hand (or delete the
    file) — a rewrite-every-row migration on every single apply is not worth
    carrying for a change that happens once a year.
    """
    needs_header = not os.path.exists(csv_file) or os.path.getsize(csv_file) == 0

    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if needs_header:
            writer.writeheader()
        writer.writerow(row)


def save_applied_job(job, questionnaire_answers: list | None = None) -> None:
    write_csv_row(
        config.APPLIED_JOBS_CSV,
        ["job_id", "title", "company", "applied_at", "questionnaire_answers"],
        {
            "job_id": job.job_id,
            "title": job.title,
            "company": job.company,
            "applied_at": utc_timestamp(),
            "questionnaire_answers": json.dumps(
                questionnaire_answers or [],
                ensure_ascii=False,
            ),
        },
    )


def normalise_job_link(job) -> str:
    link = (job.apply_link or "").strip()
    if link.startswith("/"):
        return f"https://www.naukri.com{link}"
    return link or f"https://www.naukri.com/job-listings-{job.job_id}"
