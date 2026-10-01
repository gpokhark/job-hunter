import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from job_hunter import background
from job_hunter.adapters.base import RateLimitError
from job_hunter.background import (
    CollectState,
    SourceProgress,
    effective_status,
    read_state,
    run_collection,
    write_state,
)
from job_hunter.config import (
    BackgroundConfig,
    CandidateProfile,
    CollectionConfig,
    CompanyConfig,
    Settings,
)
from job_hunter.models import JobDetail, JobSummary
from job_hunter.runlock import RunLockHeld, run_lock


def _company(key: str) -> CompanyConfig:
    return CompanyConfig(key=key, company=key.title(), adapter="fake", config={})


def _summary(key: str, job_id: str = "j1") -> JobSummary:
    return JobSummary(
        source_key=key, source_platform="fake", company=key.title(), job_id=job_id,
        title="Role", url=f"https://example.com/{key}/{job_id}", country="US",
        posted_at=datetime.now(UTC) - timedelta(days=1),
    )


class _Ok:
    on_fetch = None  # optional callable(company_key)

    def __init__(self, company, client, collection, max_posting_age_days=None):
        self.company = company
        self.kept: list[JobSummary] = []

    async def fetch_summaries(self):
        if type(self).on_fetch:
            type(self).on_fetch(self.company.key)
        return [_summary(self.company.key)]

    async def fetch_detail(self, summary):
        return JobDetail(description="d")

    async def aclose(self):
        pass


class _Limited(_Ok):
    async def fetch_summaries(self):
        self.kept = [_summary(self.company.key)]
        raise RateLimitError("rate limited (HTTP 429) after 1 attempts: https://x", http_status=429, url="https://x")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _Ok.on_fetch = None
    return tmp_path


def _settings(tmp_path, **collection) -> Settings:
    return Settings(
        database_path=tmp_path / "jobs.sqlite3",
        collection=CollectionConfig(max_concurrent_sources=1, **collection),
    )


@pytest.mark.asyncio
async def test_full_run_writes_state_final_archive_and_releases_the_lock(project):
    state_path = project / "state.json"
    with patch("job_hunter.collector.adapter_class", return_value=_Ok):
        state = await run_collection(
            _settings(project), [_company("a"), _company("b")], CandidateProfile(),
            slow=False, state_path=state_path,
        )
    assert state.status == "complete"
    assert [s.status for s in state.sources] == ["ok", "ok"]
    assert all(s.job_count == 1 for s in state.sources)
    assert state.archive and (project / state.archive).exists()
    assert read_state(state_path) == state
    assert not (project / "data" / "locks" / "collector.lock").exists()


@pytest.mark.asyncio
async def test_rate_limited_source_is_recorded_with_its_reason(project):
    with patch("job_hunter.collector.adapter_class", return_value=_Limited):
        state = await run_collection(
            _settings(project), [_company("a")], CandidateProfile(),
            slow=False, state_path=project / "state.json",
        )
    source = state.sources[0]
    assert (source.status, source.failure_kind, source.http_status, source.job_count) == (
        "warning", "rate_limited", 429, 1,
    )
    assert "kept 1 job(s)" in source.message


@pytest.mark.asyncio
async def test_stop_marks_pending_sources_skipped_and_writes_no_archive(project):
    stop = asyncio.Event()
    _Ok.on_fetch = lambda key: stop.set() if key == "a" else None
    with patch("job_hunter.collector.adapter_class", return_value=_Ok):
        state = await run_collection(
            _settings(project), [_company("a"), _company("b"), _company("c")], CandidateProfile(),
            slow=False, state_path=project / "state.json", stop_event=stop,
        )
    assert state.status == "stopped"
    assert [s.status for s in state.sources] == ["ok", "skipped", "skipped"]
    assert state.archive is None


@pytest.mark.asyncio
async def test_second_collector_is_refused_while_the_lock_is_held(project):
    with run_lock("collector"), pytest.raises(RunLockHeld):
        await run_collection(
            _settings(project), [_company("a")], CandidateProfile(),
            slow=False, state_path=project / "state.json",
        )


def test_effective_status_reports_abandoned_for_a_dead_running_state():
    now = datetime.now(UTC)
    dead = CollectState(
        run_id="r", pid=999999999, status="running", slow=False, started_at=now, updated_at=now, sources=[],
    )
    live = dead.model_copy(update={"pid": os.getpid()})
    done = dead.model_copy(update={"status": "complete"})
    assert effective_status(dead) == "abandoned"
    assert effective_status(live) == "running"
    assert effective_status(done) == "complete"


def test_state_round_trips_and_missing_file_is_none(tmp_path):
    path = tmp_path / "x" / "state.json"
    assert read_state(path) is None
    now = datetime.now(UTC)
    state = CollectState(
        run_id="r", pid=1, status="running", slow=True, started_at=now, updated_at=now,
        sources=[SourceProgress(source_key="a", company="A")],
    )
    write_state(state, path)
    loaded = read_state(path)
    assert loaded.run_id == "r" and loaded.sources[0].status == "pending"


def test_background_defaults():
    config = BackgroundConfig()
    assert (config.max_concurrent_sources, config.source_delay_seconds) == (1, 30.0)
    assert CollectionConfig().background == config


@pytest.mark.asyncio
async def test_slow_mode_applies_background_concurrency_and_delay(project):
    seen = {}
    real = background.Collector

    class _Spy(real):
        def __init__(self, settings, companies, profile, source_delay_seconds=0.0):
            seen["concurrency"] = settings.collection.max_concurrent_sources
            seen["delay"] = source_delay_seconds
            super().__init__(settings, companies, profile, source_delay_seconds=0.0)

    settings = Settings(
        database_path=project / "jobs.sqlite3",
        collection=CollectionConfig(
            max_concurrent_sources=5,
            background=BackgroundConfig(max_concurrent_sources=1, source_delay_seconds=12),
        ),
    )
    with patch("job_hunter.background.Collector", _Spy), patch(
        "job_hunter.collector.adapter_class", return_value=_Ok
    ):
        await run_collection(
            settings, [_company("a")], CandidateProfile(), slow=True, state_path=project / "s.json"
        )
    assert seen == {"concurrency": 1, "delay": 12}
