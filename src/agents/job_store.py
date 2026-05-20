import csv
from datetime import datetime
import json
import os

from src.config import agent_config as config


def load_job_ids(csv_file: str) -> set:
    if not os.path.exists(csv_file):
        return set()

    with open(csv_file, "r", newline="", encoding="utf-8") as f:
        return {row["job_id"] for row in csv.DictReader(f) if row.get("job_id")}


def ensure_csv_fieldnames(csv_file: str, required_fieldnames: list[str]) -> list[str]:
    if not os.path.exists(csv_file) or os.path.getsize(csv_file) == 0:
        return required_fieldnames

    with open(csv_file, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        existing_fieldnames = reader.fieldnames or []
        rows = list(reader)

    missing_fieldnames = [
        fieldname for fieldname in required_fieldnames if fieldname not in existing_fieldnames
    ]
    if not missing_fieldnames:
        return existing_fieldnames

    fieldnames = existing_fieldnames + missing_fieldnames
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return fieldnames


def write_csv_row(csv_file: str, fieldnames: list[str], row: dict) -> None:
    needs_header = not os.path.exists(csv_file) or os.path.getsize(csv_file) == 0
    fieldnames = ensure_csv_fieldnames(csv_file, fieldnames)

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
            "applied_at": datetime.utcnow().isoformat(),
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
