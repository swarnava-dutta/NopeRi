import csv
import os
import tempfile
from urllib.parse import urlsplit

from src.agents.job_store import load_job_ids, normalise_job_link, utc_timestamp, write_csv_row
from src.config import agent_config as config


_CSV_FIELDS = ["job_id", "title", "company", "source", "apply_link", "documented_at"]


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return False
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def _is_naukri_url(value: str) -> bool:
    if not _is_http_url(value):
        return False
    host = (urlsplit(value).hostname or "").casefold()
    return host == "naukri.com" or host.endswith(".naukri.com")


def _is_direct_external_url(value: str) -> bool:
    return _is_http_url(value) and not _is_naukri_url(value)


class ExternalLinkAgent:
    """Documents external/company-site apply jobs separately."""

    def __init__(self) -> None:
        self.enabled = config.DOCUMENT_EXTERNAL_LINKS
        self.csv_file = config.EXTERNAL_JOBS_CSV
        self.documented_job_ids = load_job_ids(self.csv_file) if self.enabled else set()

    def _has_expected_schema(self) -> bool:
        if not os.path.exists(self.csv_file) or os.path.getsize(self.csv_file) == 0:
            return True
        with open(self.csv_file, "r", newline="", encoding="utf-8-sig") as f:
            return next(csv.reader(f), []) == _CSV_FIELDS

    def _atomic_enrich(self, job_id: str, direct_link: str) -> bool | None:
        """Replace a legacy Naukri link in place without exposing a partial CSV.

        Returns True when updated, False when no enrichment was needed, and
        None when the on-disk schema is not the six-column schema we own.
        """
        if not os.path.exists(self.csv_file):
            return False

        with open(self.csv_file, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != _CSV_FIELDS:
                return None
            rows = list(reader)

        changed = False
        for row in rows:
            if str(row.get("job_id") or "").strip() != job_id:
                continue
            old_link = (row.get("apply_link") or "").strip()
            if (not old_link or _is_naukri_url(old_link)) and _is_direct_external_url(direct_link):
                row["apply_link"] = direct_link
                changed = True
            break

        if not changed:
            return False

        directory = os.path.dirname(os.path.abspath(self.csv_file))
        fd, temp_path = tempfile.mkstemp(
            prefix=".external_jobs_", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            os.replace(temp_path, self.csv_file)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        return True

    def document(self, job, source: str) -> str:
        """Persist one external job and report what actually happened.

        Return values: ``written``, ``updated``, ``duplicate``, ``disabled``,
        or ``invalid``. Existing rows are only rewritten when a newly captured
        direct company URL can replace their legacy Naukri listing URL.
        """
        if not self.enabled:
            return "disabled"

        job_id = str(getattr(job, "job_id", "") or "").strip()
        source = str(source or "").strip()
        if not job_id or not source:
            return "invalid"

        try:
            apply_link = normalise_job_link(job)
        except (AttributeError, TypeError):
            return "invalid"
        if not _is_http_url(apply_link):
            return "invalid"

        if job_id in self.documented_job_ids:
            if _is_direct_external_url(apply_link):
                enriched = self._atomic_enrich(job_id, apply_link)
                if enriched is True:
                    return "updated"
                if enriched is None:
                    return "invalid"
            return "duplicate"

        if not self._has_expected_schema():
            return "invalid"

        write_csv_row(
            self.csv_file,
            _CSV_FIELDS,
            {
                "job_id": job_id,
                "title": getattr(job, "title", "") or "",
                "company": getattr(job, "company", "") or "",
                "source": source,
                "apply_link": apply_link,
                "documented_at": utc_timestamp(),
            },
        )
        self.documented_job_ids.add(job_id)
        return "written"
