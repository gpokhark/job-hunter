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


class RecommendationConfig(BaseModel):
    minimum_score: int = Field(75, ge=0, le=100)


class LoggingConfig(BaseModel):
    level: str = "INFO"


class Settings(BaseModel):
    version: int = 1
    database_path: Path = Path("data/jobs.sqlite3")
    collection: CollectionConfig = CollectionConfig()
    search: SearchConfig = SearchConfig()
    recommendation: RecommendationConfig = RecommendationConfig()
    logging: LoggingConfig = LoggingConfig()


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
