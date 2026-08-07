import html
import re
from collections import Counter


def plain_text(markup: str) -> str:
    """Flatten Naukri's HTML job descriptions into readable text.

    ``job.description`` comes back as markup (``<p>``, ``<ul>``, ``<strong>``,
    ``&amp;``). Feeding that straight to the role filter spent most of the
    character budget on tags, and made reading_pause() time the markup rather
    than the words.

    Deliberately not a parser: the consumers are a language model and a
    reading timer, neither of which needs a DOM. Block-level closers become
    newlines so bullet lists don't run together.
    """
    text = re.sub(r"(?i)<(br|/p|/li|/h\d|/div|/tr)[^>]*>", "\n", markup or "")
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[^\S\n]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def empty_stats(found: int = 0) -> Counter:
    """Run counters. Counter returns 0 for keys that were never incremented,
    so there's no key list to keep in sync with the printed summary."""
    return Counter(found=found)


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
