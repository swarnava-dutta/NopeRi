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

    # ``utf-8-sig`` accepts both normal UTF-8 and Excel-style BOM files.  A
    # BOM previously changed the first key to ``\ufeffjob_id`` and made every
    # recorded application look new.
    with open(csv_file, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return set()
        if "job_id" not in reader.fieldnames:
            raise ValueError(f"{csv_file} is missing required job_id column")
        return {
            str(row.get("job_id") or "").strip()
            for row in reader
            if str(row.get("job_id") or "").strip()
        }


def write_csv_row(csv_file: str, fieldnames: list[str], row: dict) -> None:
    """Append one row without corrupting an existing older CSV schema."""
    needs_header = not os.path.exists(csv_file) or os.path.getsize(csv_file) == 0
    output_fields = list(fieldnames)

    if not needs_header:
        with open(csv_file, "r", newline="", encoding="utf-8-sig") as existing:
            current_fields = next(csv.reader(existing), [])
        if not current_fields:
            needs_header = True
        else:
            # Older applied_jobs.csv files have four columns.  Reusing their
            # actual header safely drops new optional fields instead of
            # appending a five-field row beneath a four-field header.
            output_fields = current_fields

    if "job_id" not in output_fields:
        raise ValueError(f"{csv_file} is missing required job_id column")

    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_fields, extrasaction="ignore")
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
