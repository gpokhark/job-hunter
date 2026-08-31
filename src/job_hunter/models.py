from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class WorkArrangement(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class LocationConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SponsorshipStatus(StrEnum):
    """A posting's explicit stance on visa sponsorship, when one is stated — see
    `sponsorship.py`'s module docstring. Purely informational, never a filter: a job
    scored NOT_AVAILABLE still passes every other stage exactly like any other job."""

    AVAILABLE = "available"
    NOT_AVAILABLE = "not_available"
    UNMENTIONED = "unmentioned"


class HealthStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class JobSummary(BaseModel):
    source_key: str
    source_platform: str
    company: str
    job_id: str
    title: str
    url: str
    location_raw: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    work_arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    department: str | None = None
    employment_type: str | None = None
    posted_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @field_validator("job_id", "title", "url")
    @classmethod
    def nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class JobDetail(BaseModel):
    description: str | None = None
    location_raw: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    work_arrangement: WorkArrangement | None = None
    department: str | None = None
    employment_type: str | None = None
    posted_at: datetime | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None


class Assessment(BaseModel):
    """An LLM sub-agent's fitness verdict on one job, persisted so a later run can skip
    re-reviewing it (see `storage.py`'s `assessments` table and `job-hunter
    record-assessment`). Keyed by (source_key, job_id); `content_hash` pins it to the
    exact job content it was evaluated against — a job whose content_hash has since
    changed is treated as unassessed again, not silently reused."""

    source_key: str
    job_id: str
    company: str
    title: str
    url: str
    content_hash: str | None = None
    score: int = Field(ge=0, le=100)
    recommended: bool
    matches: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    resume_path: str | None = None
    assessed_at: datetime = Field(default_factory=utcnow)


class Job(JobSummary):
    us_eligible: bool
    location_confidence: LocationConfidence
    location_evidence: str | None = None
    visa_sponsorship: SponsorshipStatus = SponsorshipStatus.UNMENTIONED
    sponsorship_evidence: str | None = None
    description: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    content_hash: str | None = None
    is_new: bool = False
    is_changed: bool = False
    prior_assessment: Assessment | None = None


class SourceHealth(BaseModel):
    source_key: str
    company: str
    status: HealthStatus
    job_count: int = 0
    message: str | None = None
    error_type: str | None = None
    attempted_at: datetime = Field(default_factory=utcnow)


class SearchSummary(BaseModel):
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    jobs_observed: int = 0
    us_eligible: int = 0
    prefilter_candidates: int = 0
    stale_excluded: int = 0
    partial_failure: bool = False


class RunInfo(BaseModel):
    run_id: str
    started_at: datetime
    completed_at: datetime | None = None


class SearchResult(BaseModel):
    run: RunInfo
    summary: SearchSummary
    source_health: list[SourceHealth]
    candidates: list[Job]
