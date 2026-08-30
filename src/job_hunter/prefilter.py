from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .config import CandidateProfile
from .models import Job


def is_recent(posted_at: datetime | None, max_age_days: int, *, now: datetime | None = None) -> bool:
    """Deterministic date-based check: false only for a known posted_at older than
    max_age_days. A missing posted_at (many career sites don't expose one) counts as
    recent — the age can't be determined, and silently dropping it would look identical
    to a source outage. Shared by `passes_recency` and the collector's early skip of a
    detail fetch for a summary already provably stale from its listing-level date alone.
    """
    if posted_at is None:
        return True
    now = now or datetime.now(UTC)
    # Some sources (e.g. date-only strings like "2026-08-26") parse to naive datetimes.
    posted_at = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=UTC)
    return (now - posted_at) <= timedelta(days=max_age_days)


def passes_recency(job: Job, max_age_days: int, *, now: datetime | None = None) -> bool:
    """Deterministic date-based filter: excludes jobs posted more than max_age_days ago.

    A job with no known posted_at (many career sites don't expose one) is kept rather than
    excluded — the collector cannot determine its age, and silently dropping it would look
    identical to a source outage.
    """
    return is_recent(job.posted_at, max_age_days, now=now)


def passes_prefilter(job: Job, profile: CandidateProfile) -> bool:
    if not job.us_eligible:
        return False
    haystack = f"{job.title} {job.department or ''} {job.description or ''}".lower()
    if any(term.lower() in job.title.lower() for term in profile.exclude_title_terms):
        return False
    if any(term.lower() in haystack for term in profile.exclude_terms):
        return False
    positive = [*profile.target_title_terms, *profile.target_domains]
    return not positive or any(term.lower() in haystack for term in positive)


def relevance_score(job: Job, profile: CandidateProfile) -> int:
    """Cheap ordering only; final semantic scoring remains the invoking agent's job."""
    title = job.title.lower()
    description = (job.description or "").lower()
    score = 0
    for term in profile.target_title_terms:
        value = term.lower()
        score += 5 if value in title else 1 if value in description else 0
    for term in profile.target_domains:
        value = term.lower()
        score += 6 if value in title else 2 if value in description else 0
    return score
