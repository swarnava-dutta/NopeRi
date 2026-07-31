
from dataclasses import dataclass, field
import time


@dataclass
class NaukriSession:
    bearer_token: str
    cookies: dict
    login_time: float = field(default_factory=time.time)


@dataclass
class Job:
    job_id: str
    title: str
    company: str
    location: str
    experience: str
    salary: str
    posted_date: str
    apply_link: str
    description: str = ""
    tags: list = field(default_factory=list)
    # Tri-state external-apply hint taken from the listing payload:
    #   True  -> listing explicitly says apply happens on a company URL
    #   False -> listing explicitly says it's an on-site (easy) apply
    #   None  -> listing didn't say; only the job-details call can tell
    external: bool = None


@dataclass
class JobLead:
    """One candidate job plus the context needed to apply to it.

    Search and recommended feeds are collected into a single pool, so each
    job has to carry its own apply source (which drives the applySrc /
    logstr fields) and a human-readable label for logging.
    """

    job: Job
    source: str           # "recommended" | "search"
    label: str            # search keyword, or "recommended"
    score: float = 0.0



@dataclass
class ApplicationStatus:
    status_id: int
    status_value: str
    date_time: str

@dataclass
class ApplicationHistory:
    job_id: str
    job_title: str
    company: str
    location: str
    apply_type: str
    is_open: bool
    ars_score: int
    star_rating: str
    job_type: str
    statuses: list[ApplicationStatus]
    company_rating: float = None
    logo_path: str = None
