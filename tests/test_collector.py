from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from job_hunter.collector import Collector
from job_hunter.config import CandidateProfile, CollectionConfig, CompanyConfig, Settings
from job_hunter.models import JobDetail, JobSummary


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
