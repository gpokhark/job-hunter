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


def passes_prefilter(
    job: Job, profile: CandidateProfile, *, keywords: list[str] | None = None
) -> bool:
    """`keywords`, when given (e.g. from `job-hunter search --keyword`), replaces the
    profile's `target_title_terms`/`target_domains` as the positive-match set for this
    run only — an ad-hoc "only ADAS/Robotics/..." search rather than the standing
    profile config. `exclude_title_terms`, `exclude_terms`, and the U.S.-eligibility gate
    still apply either way; a keyword search only ever narrows further, never bypasses
    those hard filters.

    The positive match (profile terms or keyword override) is checked against title +
    department only, never the free-text description — a real-world case proved
    description text unreliable for gating: company-wide "about us" boilerplate ("we
    build autonomous driving technology...") and long lists of optional "preferred"
    bullets routinely inject a target term into a posting with no real connection to it.
    `department` is kept because it's curated, structured metadata (e.g. "Autonomous
    Tech Dev Dep"), not marketing prose, so it doesn't share that failure mode — it can
    still catch a genuinely relevant but genuinely-titled role. `exclude_terms` still
    checks the full description: over-excluding on a disqualifying phrase is low-risk,
    the danger is only ever on the inclusion side."""
    if not job.us_eligible:
        return False
    if any(term.lower() in job.title.lower() for term in profile.exclude_title_terms):
        return False
    full_text = f"{job.title} {job.department or ''} {job.description or ''}".lower()
    if any(term.lower() in full_text for term in profile.exclude_terms):
        return False
    gate_text = f"{job.title} {job.department or ''}".lower()
    positive = keywords if keywords else [*profile.target_title_terms, *profile.target_domains]
    if positive and not any(term.lower() in gate_text for term in positive):
        return False
    # Soft excludes: unlike exclude_terms above, (a) the match itself is scoped to gate_text
    # (title+department), never the description, and (b) even a match there is overridden
    # whenever gate_text also contains one of the narrow, separately-curated
    # strong_relevance_terms. Both restrictions exist for the same reason the positive-match
    # gate above is title+department-scoped: confirmed on real data that description-wide
    # soft-exclude matching produces real false positives — Ford's "Sr. Electrical System
    # Validation Engineer" and "Staff HV Electrical System Validation Engineer" both mention
    # "next-generation vehicle platform architectures for electric vehicles" in their
    # description, a legitimate EV context with nothing to do with the Apple chip-org
    # postings "platform architecture" was meant to catch — and neither title contains
    # anything a title-scoped strong_relevance_terms check could rescue. Unlike categorical
    # exclude_terms (intern/co-op, where over-excluding is low-risk), a soft-exclude is a
    # domain-overlap pattern derived from feedback, not a categorical disqualifier — it needs
    # the same discipline as inclusion, not the description's full text. See
    # docs/feedback-exclusion-plan.md section 4.
    soft_excluded = any(term.lower() in gate_text for term in profile.soft_exclude_terms)
    rescued = any(term.lower() in gate_text for term in profile.strong_relevance_terms)
    return not (soft_excluded and not rescued)


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
