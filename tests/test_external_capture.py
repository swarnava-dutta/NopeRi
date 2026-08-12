"""Focused checks for external-apply detection, URL retention, and CSV updates.

Run directly:  python tests/test_external_capture.py
"""

import csv
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.external_link_agent import ExternalLinkAgent
from src.client.job_client import NaukriJobClient, external_info
from src.config import agent_config as config


NAUKRI_LINK = "https://www.naukri.com/job-listings-example-123"
DIRECT_LINK = "https://careers.example.com/jobs/123"


def check_shared_external_info() -> None:
    # A company homepage alone is not external evidence.
    assert external_info({"companyUrl": DIRECT_LINK}, NAUKRI_LINK) == (
        None,
        NAUKRI_LINK,
    )

    # Explicit false is respected when there is no positive signal.
    assert external_info({"isExternalJob": "FALSE"}, NAUKRI_LINK) == (
        False,
        NAUKRI_LINK,
    )

    # Any true flag wins over conflicting false flags; bool/string/int forms
    # are normalized, and a corroborated companyUrl becomes the retained link.
    verdict, link = external_info(
        {
            "isExternalJob": 0,
            "externalApply": "yes",
            "isExternal": False,
            "companyUrl": DIRECT_LINK,
        },
        NAUKRI_LINK,
    )
    assert verdict is True and link == DIRECT_LINK

    assert external_info({"isExternalJob": 1}, NAUKRI_LINK)[0] is True
    assert external_info({"isExternalJob": "0"}, NAUKRI_LINK)[0] is False

    # Redirect fields are positive evidence and have deterministic priority.
    primary = "https://jobs.example.com/external/123"
    secondary = "https://jobs.example.com/redirect/123"
    assert external_info(
        {
            "externalApplyUrl": primary,
            "applyRedirectUrl": secondary,
            "isExternalJob": False,
        },
        NAUKRI_LINK,
    ) == (True, primary)

    # Invalid/unsafe candidates are ignored in favor of the next valid URL.
    assert external_info(
        {
            "externalApplyUrl": "javascript:alert(1)",
            "applyRedirectUrl": secondary,
        },
        NAUKRI_LINK,
    ) == (True, secondary)

    # responseManager matching is case/punctuation tolerant and works for the
    # nested job-details response shape through the same helper.
    nested = {
        "job": {
            "responseManager": "Company URL",
            "companyUrl": "//careers.example.com/jobs/456",
        }
    }
    assert external_info(nested, NAUKRI_LINK) == (
        True,
        "https://careers.example.com/jobs/456",
    )
    assert NaukriJobClient.is_external(nested) is True


def check_listing_and_details_retain_links() -> None:
    client = NaukriJobClient.__new__(NaukriJobClient)
    client._jobs_by_id = {}

    listing_job = client._parse_job(
        {
            "jobId": "123",
            "title": "AI Engineer",
            "companyName": "Example",
            "jdURL": "/job-listings-example-123",
            "externalApplyUrl": DIRECT_LINK,
        }
    )
    assert listing_job.external is True
    assert listing_job.apply_link == DIRECT_LINK

    # companyUrl alone keeps the Naukri listing and an unknown verdict.
    ambiguous = client._parse_job(
        {
            "id": "456",
            "title": "ML Engineer",
            "companyName": "Example",
            "companyUrl": "https://example.com",
        }
    )
    assert ambiguous.external is None
    assert ambiguous.apply_link.endswith("job-listings-456")

    # Details-only evidence mutates the same pooled Job object that the writer
    # later receives, despite EasyApplyAgent retaining its two-argument call.
    details_link = "https://careers.example.com/jobs/456/apply"
    verdict, retained = client._retain_external_info(
        "456", {"job": {"externalApplyUrl": details_link}}
    )
    assert verdict is True and retained == details_link
    assert ambiguous.external is True
    assert ambiguous.apply_link == details_link

    # Re-parsing an overlapping search result must not replace the pooled
    # object tracked for later details enrichment.
    duplicate = client._parse_job(
        {
            "jobId": "456",
            "title": "Duplicate result",
            "companyName": "Example",
            "externalApplyUrl": "https://careers.example.com/jobs/456/new",
        }
    )
    assert duplicate is not ambiguous
    assert client._jobs_by_id["456"] is ambiguous
    assert ambiguous.external is True
    assert ambiguous.apply_link == "https://careers.example.com/jobs/456/new"


def _read_rows(path: Path) -> list[dict]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def check_document_status_and_atomic_enrichment() -> None:
    saved_enabled = config.DOCUMENT_EXTERNAL_LINKS
    saved_file = config.EXTERNAL_JOBS_CSV

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "external_jobs.csv"
            config.DOCUMENT_EXTERNAL_LINKS = True
            config.EXTERNAL_JOBS_CSV = str(csv_path)
            agent = ExternalLinkAgent()

            job = SimpleNamespace(
                job_id="123",
                title="AI Engineer",
                company="Example",
                apply_link=NAUKRI_LINK,
            )
            assert agent.document(job, "search") == "written"
            before = _read_rows(csv_path)
            assert len(before) == 1 and before[0]["apply_link"] == NAUKRI_LINK

            # The same id can be enriched in place when details later reveal a
            # direct company URL. Row count/schema/timestamp stay unchanged.
            job.apply_link = DIRECT_LINK
            assert agent.document(job, "search") == "updated"
            after = _read_rows(csv_path)
            assert len(after) == 1
            assert after[0]["apply_link"] == DIRECT_LINK
            assert after[0]["documented_at"] == before[0]["documented_at"]
            assert list(after[0]) == [
                "job_id",
                "title",
                "company",
                "source",
                "apply_link",
                "documented_at",
            ]
            assert not list(Path(temp_dir).glob(".external_jobs_*.tmp"))

            assert agent.document(job, "search") == "duplicate"
            assert agent.document(SimpleNamespace(job_id=""), "search") == "invalid"
            assert agent.document(job, "") == "invalid"

            config.DOCUMENT_EXTERNAL_LINKS = False
            disabled = ExternalLinkAgent()
            assert disabled.document(job, "search") == "disabled"

            # Never claim a write beneath an unknown/malformed CSV header.
            malformed_path = Path(temp_dir) / "malformed.csv"
            malformed_path.write_text("job_id,title\nold,Old\n", encoding="utf-8")
            config.DOCUMENT_EXTERNAL_LINKS = True
            config.EXTERNAL_JOBS_CSV = str(malformed_path)
            malformed = ExternalLinkAgent()
            job.job_id = "new"
            assert malformed.document(job, "search") == "invalid"
            assert malformed_path.read_text(encoding="utf-8") == "job_id,title\nold,Old\n"
    finally:
        config.DOCUMENT_EXTERNAL_LINKS = saved_enabled
        config.EXTERNAL_JOBS_CSV = saved_file


if __name__ == "__main__":
    checks = (
        check_shared_external_info,
        check_listing_and_details_retain_links,
        check_document_status_and_atomic_enrichment,
    )
    for check in checks:
        check()
        print(f"ok  {check.__name__}")
    print("\nall external-capture checks passed")
