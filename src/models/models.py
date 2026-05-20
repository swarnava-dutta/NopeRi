
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
