import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from job_hunter.adapters.base import RateLimitError
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
        # Anchored to the real clock, not a hardcoded date: the collector's own recency
        # check (is_recent) always compares against datetime.now(UTC), so a fixed fake
        # "now" here silently drifts stale as real time passes it — confirmed as a real
        # regression once "fresh-1" (originally 5 days old relative to a hardcoded
        # 2026-08-29) aged past the 30-day cutoff for real.
        now = datetime.now(UTC)
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
        # Relative to the real clock: passes_recency compares against today, so a fixed date
        # here silently ages out of max_posting_age_days and drops all but the newest job.
        now = datetime.now(UTC)
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


def _partial_summary(job_id: str) -> JobSummary:
    return JobSummary(
        source_key="fake", source_platform="fake", company="Acme", job_id=job_id,
        title="New Role", url=f"https://example.com/{job_id}", country="US",
        posted_at=datetime.now(UTC) - timedelta(days=5),
    )


class _PartialAdapter:
    """Registers one job, then fails the way a rate-limited paginating adapter does."""

    error: Exception = RateLimitError(
        "rate limited (HTTP 429) after 1 attempts: https://x",
        http_status=429, url="https://x", retry_after_seconds=90.0,
    )
    register = True

    def __init__(self, company, client, collection, max_posting_age_days=None):
        self.company = company
        self.kept: list[JobSummary] = []

    async def fetch_summaries(self) -> list[JobSummary]:
        if type(self).register:
            self.kept = [_partial_summary("p1")]
        raise type(self).error

    async def fetch_detail(self, summary: JobSummary) -> JobDetail:
        return JobDetail(description=f"Description for {summary.job_id}")

    async def aclose(self) -> None:
        pass


class _NoKeptAdapter:
    """Like the older fakes/third-party adapters: no `kept` attribute at all."""

    def __init__(self, company, client, collection, max_posting_age_days=None):
        self.company = company

    async def fetch_summaries(self) -> list[JobSummary]:
        raise RuntimeError("boom")

    async def aclose(self) -> None:
        pass


def _fake_company() -> CompanyConfig:
    return CompanyConfig(key="fake", company="Acme", adapter="fake", config={})


async def _search(tmp_path, adapter_cls, **collection):
    settings = Settings(
        database_path=tmp_path / "jobs.sqlite3", collection=CollectionConfig(**collection)
    )
    with patch("job_hunter.collector.adapter_class", return_value=adapter_cls):
        return await Collector(settings, [_fake_company()], CandidateProfile()).search(
            include_seen=True
        )


@pytest.mark.asyncio
async def test_rate_limited_listing_keeps_partial_jobs_as_a_warning(tmp_path):
    _PartialAdapter.register = True
    _PartialAdapter.error = RateLimitError(
        "rate limited (HTTP 429) after 1 attempts: https://x",
        http_status=429, url="https://x", retry_after_seconds=90.0,
    )
    result = await _search(tmp_path, _PartialAdapter)
    health = result.source_health[0]
    assert health.status.value == "warning"
    assert health.failure_kind == "rate_limited"
    assert health.http_status == 429
    assert health.retry_after_seconds == 90.0
    assert health.job_count == 1
    assert "rate limited (HTTP 429), Retry-After 90s" in health.message
    assert "kept 1 job(s)" in health.message
    assert result.summary.sources_succeeded == 1
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.get_job("fake", "p1") is not None


@pytest.mark.asyncio
async def test_partial_listing_never_marks_unseen_jobs_missing(tmp_path):
    # A first, complete run stores stale-1 and fresh-1.
    with patch("job_hunter.collector.adapter_class", return_value=_FakeAdapter):
        settings = Settings(database_path=tmp_path / "jobs.sqlite3", collection=CollectionConfig())
        await Collector(settings, [_fake_company()], CandidateProfile()).search(include_seen=True)
    _PartialAdapter.register = True
    await _search(tmp_path, _PartialAdapter)  # partial run only saw p1
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        row = storage.get_job("fake", "fresh-1")
        assert row["status"] == "active"
        assert row["missing_count"] == 0  # OK-only mark_missing never ran


@pytest.mark.asyncio
async def test_partial_run_does_not_replace_the_count_baseline(tmp_path):
    with patch("job_hunter.collector.adapter_class", return_value=_FakeAdapter):
        settings = Settings(database_path=tmp_path / "jobs.sqlite3", collection=CollectionConfig())
        await Collector(settings, [_fake_company()], CandidateProfile()).search(include_seen=True)
    _PartialAdapter.register = True
    await _search(tmp_path, _PartialAdapter)
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.previous_job_count("fake") == 2  # not the partial run's 1


@pytest.mark.asyncio
async def test_rate_limited_listing_with_nothing_kept_is_failed_with_failure_kind(tmp_path):
    _PartialAdapter.register = False
    result = await _search(tmp_path, _PartialAdapter)
    health = result.source_health[0]
    assert health.status.value == "failed"
    assert health.failure_kind == "rate_limited"
    assert health.http_status == 429
    assert health.job_count == 0


@pytest.mark.asyncio
async def test_other_exception_after_partial_pages_is_a_loud_warning(tmp_path):
    _PartialAdapter.register = True
    _PartialAdapter.error = ValueError("job card missing required link/title")
    try:
        result = await _search(tmp_path, _PartialAdapter)
    finally:
        _PartialAdapter.error = RateLimitError("x", http_status=429, url="u")
    health = result.source_health[0]
    assert health.status.value == "warning"
    assert health.failure_kind is None
    assert "ValueError: job card missing required link/title" in health.message
    assert health.error_type == "ValueError"


@pytest.mark.asyncio
async def test_adapter_without_kept_attribute_still_fails_cleanly(tmp_path):
    result = await _search(tmp_path, _NoKeptAdapter)
    health = result.source_health[0]
    assert health.status.value == "failed"
    assert health.message == "boom"


class _SlowAdapter(_PartialAdapter):
    async def fetch_summaries(self) -> list[JobSummary]:
        self.kept = [_partial_summary("slow-1")]
        await asyncio.sleep(10)
        return self.kept


@pytest.mark.asyncio
async def test_listing_timeout_keeps_partial_jobs_and_reports_timeout(tmp_path):
    result = await _search(tmp_path, _SlowAdapter, source_timeout_seconds=0.05)
    health = result.source_health[0]
    assert health.status.value == "warning"
    assert health.failure_kind == "timeout"
    assert health.job_count == 1
    assert "listing timed out" in health.message


def test_source_timeout_default_and_disable():
    assert CollectionConfig().source_timeout_seconds == 1200
    assert CollectionConfig(source_timeout_seconds=None).source_timeout_seconds is None
