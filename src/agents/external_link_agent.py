from src.agents.job_store import load_job_ids, normalise_job_link, utc_timestamp, write_csv_row
from src.config import agent_config as config


class ExternalLinkAgent:
    """Documents external/company-site apply jobs separately."""

    def __init__(self) -> None:
        self.enabled = config.DOCUMENT_EXTERNAL_LINKS
        self.csv_file = config.EXTERNAL_JOBS_CSV
        self.documented_job_ids = load_job_ids(self.csv_file)

    def document(self, job, source: str) -> None:
        if not self.enabled or not job.job_id:
            return
        if job.job_id in self.documented_job_ids:
            return

        write_csv_row(
            self.csv_file,
            ["job_id", "title", "company", "source", "apply_link", "documented_at"],
            {
                "job_id": job.job_id,
                "title": job.title,
                "company": job.company,
                "source": source,
                "apply_link": normalise_job_link(job),
                "documented_at": utc_timestamp(),
            },
        )
        self.documented_job_ids.add(job.job_id)
