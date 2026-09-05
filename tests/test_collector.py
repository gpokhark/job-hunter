from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from job_hunter.collector import Collector
from job_hunter.config import CandidateProfile, CollectionConfig, CompanyConfig, Settings
from job_hunter.models import Assessment, JobDetail, JobSummary
from job_hunter.normalizer import description_hash
from job_hunter.storage import Storage


class _FakeAdapter:
    detail_calls: list[str] = []

    def __init__(self, company, client, collection, max_posting_age_days=None):
        self.company = company
        self.max_posting_age_days = max_posting_age_days

    async def fetch_summaries(self) -> list[JobSummary]:
        now = datetime(2026, 8, 29, tzinfo=UTC)
        return [
            JobSummary(
                source_key="fake",
                source_platform="fake",
                company="Acme",
                job_id="stale-1",
                title="Old Role",
                url="https://example.com/stale-1",
                country="US",
                posted_at=now - timedelta(days=45),
            ),
            JobSummary(
                source_key="fake",
                source_platform="fake",
                company="Acme",
                job_id="fresh-1",
                title="New Role",
                url="https://example.com/fresh-1",
                country="US",
                posted_at=now - timedelta(days=5),
            ),
        ]

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        _FakeAdapter.detail_calls.append(summary.job_id)
        return JobDetail(description=f"Description for {summary.job_id}")

    async def healthcheck(self):
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_stale_summary_skips_detail_fetch(tmp_path):
    """A job whose listing-level posted_at already proves it's older than the recency
    cutoff should never trigger a detail fetch — it will be excluded by passes_recency
    regardless of its description, so fetching one is pure waste."""
    _FakeAdapter.detail_calls = []
    settings = Settings(database_path=tmp_path / "jobs.sqlite3", collection=CollectionConfig())
    company = CompanyConfig(key="fake", company="Acme", adapter="fake", config={})
    profile = CandidateProfile()

    with patch("job_hunter.collector.adapter_class", return_value=_FakeAdapter):
        result = await Collector(settings, [company], profile).search(include_seen=True)

    assert _FakeAdapter.detail_calls == ["fresh-1"]
    jobs_by_id = {job.job_id: job for job in result.candidates}
    assert "stale-1" not in jobs_by_id


def _seed_assessment(db_path, content_hash: str) -> None:
    with Storage(db_path) as storage:
        storage.upsert_assessment(
            Assessment(
                source_key="fake",
                job_id="fresh-1",
                company="Acme",
                title="New Role",
                url="https://example.com/fresh-1",
                content_hash=content_hash,
                score=88,
                recommended=True,
                matches=["a"],
                gaps=["b"],
            )
        )


@pytest.mark.asyncio
async def test_prior_assessment_attached_when_content_hash_matches(tmp_path):
    """A candidate whose stored assessment was made against its exact current
    description should carry that verdict forward, so the skill can skip re-reviewing
    (and spending tokens on) a job it already assessed."""
    db_path = tmp_path / "jobs.sqlite3"
    _seed_assessment(db_path, description_hash("Description for fresh-1"))
    settings = Settings(database_path=db_path, collection=CollectionConfig())
    company = CompanyConfig(key="fake", company="Acme", adapter="fake", config={})

    with patch("job_hunter.collector.adapter_class", return_value=_FakeAdapter):
        result = await Collector(settings, [company], CandidateProfile()).search(include_seen=True)

    jobs_by_id = {job.job_id: job for job in result.candidates}
    assert jobs_by_id["fresh-1"].prior_assessment is not None
    assert jobs_by_id["fresh-1"].prior_assessment.score == 88


@pytest.mark.asyncio
async def test_prior_assessment_not_attached_when_content_hash_stale(tmp_path):
    """An assessment recorded against different content (the posting changed since it
    was reviewed) must not be silently reused — the job is treated as unassessed."""
    db_path = tmp_path / "jobs.sqlite3"
    _seed_assessment(db_path, "a-completely-different-hash")
    settings = Settings(database_path=db_path, collection=CollectionConfig())
    company = CompanyConfig(key="fake", company="Acme", adapter="fake", config={})

    with patch("job_hunter.collector.adapter_class", return_value=_FakeAdapter):
        result = await Collector(settings, [company], CandidateProfile()).search(include_seen=True)

    jobs_by_id = {job.job_id: job for job in result.candidates}
    assert jobs_by_id["fresh-1"].prior_assessment is None


class _ManyJobsAdapter:
    def __init__(self, company, client, collection, max_posting_age_days=None):
        pass

    async def fetch_summaries(self) -> list[JobSummary]:
        now = datetime(2026, 8, 29, tzinfo=UTC)
        # All titled to pass a "systems" prefilter; posted_at descends so job-0 is newest.
        return [
            JobSummary(
                source_key="fake",
                source_platform="fake",
                company="Acme",
                job_id=f"job-{i}",
                title=f"Systems Engineer {i}",
                url=f"https://example.com/{i}",
                country="US",
                posted_at=now - timedelta(days=i),
            )
            for i in range(5)
        ]

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        return JobDetail(description="")

    async def healthcheck(self):
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_no_default_cap_without_keywords(tmp_path):
    """The profile-driven, keyword-less path no longer caps candidates at all — that cap
    only made sense when passes_prefilter's loose, description-wide matching passed the
    large majority of jobs; now that the gate itself is precise (title + department
    only), every match should reach assessment, newest first."""
    settings = Settings(database_path=tmp_path / "jobs.sqlite3", collection=CollectionConfig())
    company = CompanyConfig(key="fake", company="Acme", adapter="fake", config={})
    profile = CandidateProfile(target_domains=["systems"])

    with patch("job_hunter.collector.adapter_class", return_value=_ManyJobsAdapter):
        result = await Collector(settings, [company], profile).search(include_seen=True)

    assert [job.job_id for job in result.candidates] == [
        "job-0",
        "job-1",
        "job-2",
        "job-3",
        "job-4",
    ]


@pytest.mark.asyncio
async def test_max_candidates_still_caps_explicitly(tmp_path):
    """An explicit --max-candidates remains available as an opt-in cap even though there
    is no longer an automatic default one."""
    settings = Settings(database_path=tmp_path / "jobs.sqlite3", collection=CollectionConfig())
    company = CompanyConfig(key="fake", company="Acme", adapter="fake", config={})
    profile = CandidateProfile(target_domains=["systems"])

    with patch("job_hunter.collector.adapter_class", return_value=_ManyJobsAdapter):
        result = await Collector(settings, [company], profile).search(
            include_seen=True, max_candidates=2
        )

    assert [job.job_id for job in result.candidates] == ["job-0", "job-1"]


@pytest.mark.asyncio
async def test_keyword_search_overrides_profile_terms(tmp_path):
    """A keyword search replaces (not requires) the profile's own target terms — every
    job matching the keyword should reach the candidate list even if none of them match
    the configured profile at all."""
    settings = Settings(database_path=tmp_path / "jobs.sqlite3", collection=CollectionConfig())
    company = CompanyConfig(key="fake", company="Acme", adapter="fake", config={})
    profile = CandidateProfile(target_domains=["totally-unrelated-term"])

    with patch("job_hunter.collector.adapter_class", return_value=_ManyJobsAdapter):
        result = await Collector(settings, [company], profile).search(
            include_seen=True, keywords=["systems"]
        )

    assert [job.job_id for job in result.candidates] == [
        "job-0",
        "job-1",
        "job-2",
        "job-3",
        "job-4",
    ]
