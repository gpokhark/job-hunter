import asyncio
import json
import os
import signal
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from job_hunter import background
from job_hunter.adapters.base import RateLimitError
from job_hunter.background import (
    CollectorRunning,
    CollectState,
    SourceProgress,
    cli_collect,
    cli_snapshot,
    effective_status,
    format_status,
    read_state,
    request_stop,
    run_collection,
    snapshot_archive,
    start_background,
    write_snapshot,
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


@pytest.mark.asyncio
async def test_archive_write_failure_is_recorded_and_reraised(project):
    state_path = project / "state.json"
    with patch("job_hunter.collector.adapter_class", return_value=_Ok), patch(
        "job_hunter.background.archive_path", side_effect=OSError("disk full")
    ), pytest.raises(OSError, match="disk full"):
        await run_collection(
            _settings(project), [_company("a")], CandidateProfile(),
            slow=False, state_path=state_path,
        )
    state = read_state(state_path)
    assert state.status == "failed"
    assert "disk full" in state.error
    assert state.completed_at is not None
    assert not (project / "data" / "locks" / "collector.lock").exists()


@pytest.mark.asyncio
async def test_failure_state_write_is_best_effort_and_keeps_original_error(project):
    real_write = background.write_state

    def flaky_write(state, path=background.STATE_PATH):
        if state.status == "failed":
            raise OSError("state write failed")
        real_write(state, path)

    with patch("job_hunter.collector.adapter_class", return_value=_Ok), patch(
        "job_hunter.background.archive_path", side_effect=OSError("disk full")
    ), patch("job_hunter.background.write_state", flaky_write), pytest.raises(OSError) as info:
        await run_collection(
            _settings(project), [_company("a")], CandidateProfile(),
            slow=False, state_path=project / "state.json",
        )
    assert str(info.value) == "disk full"


def _state(status="running", pid=None, **extra) -> CollectState:
    now = datetime.now(UTC)
    return CollectState(
        run_id="run-1", pid=pid if pid is not None else os.getpid(), status=status, slow=False,
        started_at=now, updated_at=now, sources=extra.pop("sources", []), **extra,
    )


def test_start_is_refused_while_a_live_collector_is_running(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    write_state(_state(), state_path)  # pid == this test process: live
    monkeypatch.setattr("job_hunter.background._popen", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(CollectorRunning, match="already running"):
        start_background(project_root=tmp_path, companies=None, slow=False, state_path=state_path)


def test_start_is_allowed_when_the_recorded_run_is_abandoned(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    write_state(_state(pid=999999999), state_path)  # dead pid, still marked running
    spawned = {}

    class _Proc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        spawned["cmd"] = cmd
        spawned["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr("job_hunter.background._popen", _fake_popen)
    pid = start_background(project_root=tmp_path, companies="a,b", slow=True, state_path=state_path)
    assert pid == 4242
    assert spawned["cmd"][1:3] == ["-m", "job_hunter.background"]
    assert "--companies" in spawned["cmd"] and "a,b" in spawned["cmd"] and "--slow" in spawned["cmd"]
    assert spawned["kwargs"]["start_new_session"] is True


def test_stop_signals_only_a_live_collector(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr("job_hunter.background._kill", lambda pid, sig: calls.append((pid, sig)))
    assert request_stop(state_path) is False  # no state file
    write_state(_state(pid=999999999), state_path)
    assert request_stop(state_path) is False  # abandoned
    write_state(_state(), state_path)
    assert request_stop(state_path) is True
    assert calls == [(os.getpid(), signal.SIGTERM)]


def test_format_status_lists_progress_and_rate_limited_sources():
    sources = [
        SourceProgress(source_key="a", company="Acme", status="ok", job_count=5),
        SourceProgress(
            source_key="b", company="Beta", status="warning", job_count=12,
            failure_kind="rate_limited", http_status=429,
        ),
        SourceProgress(source_key="c", company="Gamma"),
    ]
    lines = format_status(_state(sources=sources))
    text = "\n".join(lines)
    assert "2/3 sources finished" in text
    assert "Beta" in text and "rate limited" in text.lower() and "12" in text


def test_cli_status_json_reports_effective_status(tmp_path, capsys):
    state_path = tmp_path / "state.json"
    write_state(_state(pid=999999999), state_path)
    code = cli_collect(SimpleNamespace(collect_command="status", json=True), state_path=state_path)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["status"] == "abandoned" and out["run_id"] == "run-1"


def test_cli_status_without_state_exits_2(tmp_path, capsys):
    code = cli_collect(
        SimpleNamespace(collect_command="status", json=False), state_path=tmp_path / "nope.json"
    )
    assert code == 2 and "no collection" in capsys.readouterr().err.lower()


def _mixed_state() -> CollectState:
    sources = [
        SourceProgress(source_key="a", company="Acme", status="ok", job_count=5),
        SourceProgress(
            source_key="b", company="Beta", status="warning", job_count=12,
            failure_kind="rate_limited", http_status=429, retry_after_seconds=90.0,
            message="rate limited (HTTP 429); kept 12 job(s) fetched before it stopped",
        ),
        SourceProgress(source_key="c", company="Gamma", status="failed", message="boom", error_type="X"),
        SourceProgress(source_key="d", company="Delta", status="running"),
        SourceProgress(source_key="e", company="Eps"),
        SourceProgress(source_key="f", company="Zeta", status="skipped"),
    ]
    return _state(sources=sources)


def test_snapshot_archive_contains_only_finished_sources_with_their_reasons():
    data = snapshot_archive(_mixed_state(), now=datetime(2026, 9, 30, tzinfo=UTC))
    keys = [h["source_key"] for h in data["source_health"]]
    assert keys == ["a", "b", "c"]  # running/pending/skipped are not in scope yet
    limited = data["source_health"][1]
    assert limited["status"] == "warning" and limited["failure_kind"] == "rate_limited"
    assert limited["http_status"] == 429 and limited["job_count"] == 12
    assert data["candidates"] == []
    summary = data["summary"]
    assert (summary["sources_attempted"], summary["sources_succeeded"], summary["sources_failed"]) == (3, 2, 1)
    assert summary["partial_failure"] is True


def test_write_snapshot_writes_a_resolvable_archive(project):
    path = write_snapshot(_mixed_state(), now=datetime(2026, 9, 30, tzinfo=UTC))
    assert path.exists() and path.parent.name == "searches"
    assert json.loads(path.read_text())["source_health"][0]["source_key"] == "a"


def test_cli_snapshot_runs_the_refilter_and_prints_the_next_command(project, monkeypatch, capsys):
    write_state(_mixed_state(), project / "data" / "collect" / "state.json")
    ran = {}

    def _fake_refilter(path, project_root):
        ran["path"] = path
        return 0

    monkeypatch.setattr("job_hunter.background.refilter_snapshot", _fake_refilter)
    code = cli_snapshot(state_path=project / "data" / "collect" / "state.json")
    out = capsys.readouterr().out
    assert code == 0
    assert str(ran["path"]) in out
    assert "pipeline --no-scrape --search" in out


def test_cli_snapshot_without_state_exits_2(project, capsys):
    assert cli_snapshot(state_path=project / "nope.json") == 2
    assert "collect start" in capsys.readouterr().err


def test_cli_snapshot_with_nothing_finished_yet_exits_2(project, capsys):
    write_state(_state(sources=[SourceProgress(source_key="a", company="A")]), project / "s.json")
    assert cli_snapshot(state_path=project / "s.json") == 2
    assert "no source has finished" in capsys.readouterr().err.lower()


def test_write_snapshot_uses_its_own_name_and_spares_the_default_archive(project):
    searches = project / "data" / "searches"
    searches.mkdir(parents=True)
    default = searches / f"default_{datetime.now().date().isoformat()}.json"
    default.write_text("SENTINEL")
    path = write_snapshot(_mixed_state())
    assert path.name.startswith("collect-snapshot_") and path != default
    assert default.read_text() == "SENTINEL"
    assert path.exists()


def test_cli_snapshot_refuses_while_the_shared_lock_is_held(project, monkeypatch, capsys):
    write_state(_mixed_state(), project / "s.json")
    called = []
    monkeypatch.setattr("job_hunter.background.refilter_snapshot", lambda *a: called.append(a) or 0)
    with run_lock("job-hunter"):
        code = cli_snapshot(state_path=project / "s.json")
    assert code == 2 and not called
    assert "in progress" in capsys.readouterr().err
