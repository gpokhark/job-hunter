from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class CollectionConfig(BaseModel):
    max_concurrent_sources: int = Field(3, ge=1, le=20)
    max_concurrent_details: int = Field(8, ge=1, le=50)
    max_connections: int = Field(6, ge=1)
    max_keepalive_connections: int = Field(3, ge=0)
    timeout_seconds: float = Field(30, gt=0)
    max_retries: int = Field(3, ge=0, le=10)
    user_agent: str = "JobHunter/0.1 (+manual career search)"


class SearchConfig(BaseModel):
    country: Literal["US"] = "US"
    include_work_arrangements: list[str] = ["onsite", "hybrid", "remote", "unknown"]
    max_posting_age_days: int = Field(30, ge=1)
    # A job with no discoverable posted_at is never excluded by max_posting_age_days above —
    # prefilter.py deliberately keeps it, since its true age can't be determined and silently
    # dropping it would look identical to a source outage. These two settings instead drive
    # report-display-only signals for that specific case, using first_seen_at (when
    # job-hunter's own collector first observed the job) as an imperfect but useful proxy:
    # tag it [New] while still within undated_new_days of first being seen, and tag it
    # "Long-standing" once past undated_stale_days — never a filter, just a way to tell a
    # freshly-surfaced undated posting apart from one that's been sitting in the pool for a
    # long time. See render_radar.py/diff_profile.py's `_job_tags`/date-fallback logic.
    undated_new_days: int = Field(15, ge=1)
    undated_stale_days: int = Field(45, ge=1)


class RecommendationConfig(BaseModel):
    minimum_score: int = Field(75, ge=0, le=100)


class LoggingConfig(BaseModel):
    level: str = "INFO"


class RetentionConfig(BaseModel):
    """`job-hunter cleanup`'s tunable knobs (docs/retention-cleanup-plan.md) — a system/storage
    housekeeping concern, deliberately kept in settings.yaml alongside search.max_posting_age_days
    rather than candidate_profile.yaml, which models resume-matching terms only."""

    closed_job_after_days: int = Field(
        10, ge=1, description="delete a job this many days after it's been status='closed'"
    )
    report_after_days: int = Field(
        15,
        ge=1,
        description="delete a generated profile-diff/radar report this many days after it was written",
    )
    keep_latest_reports_per_slug: int = Field(
        2,
        ge=0,
        description=(
            "never delete the N most recently generated reports of a given slug/group "
            "regardless of age — 0 disables this floor entirely"
        ),
    )


class Settings(BaseModel):
    version: int = 1
    database_path: Path = Path("data/jobs.sqlite3")
    collection: CollectionConfig = CollectionConfig()
    search: SearchConfig = SearchConfig()
    recommendation: RecommendationConfig = RecommendationConfig()
    logging: LoggingConfig = LoggingConfig()
    retention: RetentionConfig = RetentionConfig()


class CompanyConfig(BaseModel):
    key: str
    company: str
    enabled: bool = True
    adapter: str
    platform: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    unsupported_reason: str | None = None

    @model_validator(mode="after")
    def validate_support(self) -> CompanyConfig:
        if self.adapter == "unsupported" and not self.unsupported_reason:
            raise ValueError("unsupported adapters require unsupported_reason")
        return self


class CompaniesFile(BaseModel):
    version: int = 1
    companies: list[CompanyConfig]

    @model_validator(mode="after")
    def unique_keys(self) -> CompaniesFile:
        keys = [company.key for company in self.companies]
        if len(keys) != len(set(keys)):
            raise ValueError("company keys must be unique")
        return self


class CandidateProfile(BaseModel):
    profile_version: int = 1
    resume_path: Path | None = None
    target_domains: list[str] = Field(default_factory=list)
    target_title_terms: list[str] = Field(default_factory=list)
    exclude_title_terms: list[str] = Field(default_factory=lambda: ["intern", "co-op"])
    exclude_terms: list[str] = Field(default_factory=list)
    soft_exclude_terms: list[str] = Field(default_factory=list)
    strong_relevance_terms: list[str] = Field(default_factory=list)
    minimum_recommendation_score: int = Field(75, ge=0, le=100)
    location: dict[str, Any] = Field(default_factory=dict)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    content = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return content


def load_settings(path: Path = Path("config/settings.yaml")) -> Settings:
    return Settings.model_validate(_load_yaml(path))


def load_companies(path: Path = Path("config/companies.yaml")) -> list[CompanyConfig]:
    return CompaniesFile.model_validate(_load_yaml(path)).companies


def load_profile(path: Path = Path("config/candidate_profile.yaml")) -> CandidateProfile:
    if not path.exists():
        example = path.with_name("candidate_profile.example.yaml")
        return CandidateProfile.model_validate(_load_yaml(example))
    return CandidateProfile.model_validate(_load_yaml(path))
