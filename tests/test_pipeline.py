import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunter.config import CandidateProfile, Settings
from job_hunter.models import PipelineManifest, PipelineStage, PipelineStatus
from job_hunter.pipeline import (
    _decode_timeout_output,
    _fingerprint,
    _run_stage_subprocess,
    _StageTimedOut,
    latest_run_id,
    manifest_path,
    new_run_id,
    read_manifest,
    run_pipeline,
    write_manifest,
)
from job_hunter.runlock import RunLockHeld, run_lock


def test_new_run_id_is_stable_for_the_same_instant_up_to_its_random_suffix():
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    run_id = new_run_id(now)
    assert run_id.startswith("2026-09-17-T-")


def test_new_run_id_is_unique_across_calls():
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    assert new_run_id(now) != new_run_id(now)


def test_manifest_path_nests_under_the_run_id(tmp_path):
    path = manifest_path("abc123", runs_dir=tmp_path)
    assert path == tmp_path / "abc123" / "manifest.json"


def test_write_then_read_manifest_round_trips(tmp_path):
    manifest = PipelineManifest(
        run_id="abc123",
        project_root=str(tmp_path),
        keyword="ADAS",
        stage=PipelineStage.REVIEW,
        status=PipelineStatus.RUNNING,
        candidates=12,
    )
    write_manifest(manifest, runs_dir=tmp_path)
    reloaded = read_manifest("abc123", runs_dir=tmp_path)
    assert reloaded.run_id == "abc123"
    assert reloaded.keyword == "ADAS"
    assert reloaded.stage == PipelineStage.REVIEW
    assert reloaded.candidates == 12


def test_read_manifest_missing_run_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_manifest("nope", runs_dir=tmp_path)


def test_latest_run_id_picks_the_most_recently_written_manifest(tmp_path):
    older = PipelineManifest(run_id="older", project_root=str(tmp_path))
    newer = PipelineManifest(run_id="newer", project_root=str(tmp_path))
    write_manifest(older, runs_dir=tmp_path)
    write_manifest(newer, runs_dir=tmp_path)
    # Force an unambiguous mtime ordering rather than relying on filesystem timestamp
    # resolution between two writes microseconds apart.
    older_mtime = manifest_path("older", runs_dir=tmp_path).stat().st_mtime
    os.utime(manifest_path("newer", runs_dir=tmp_path), (older_mtime + 10, older_mtime + 10))
    assert latest_run_id(runs_dir=tmp_path) == "newer"


def test_latest_run_id_with_no_runs_returns_none(tmp_path):
    assert latest_run_id(runs_dir=tmp_path / "empty") is None


def test_latest_run_id_picks_by_started_at_not_mtime(tmp_path):
    """docs/agent-runtime-audit.md's "run identity is not authoritative" finding: an
    imported/copied run directory (a backup, a copy between machines) can have a newer filesystem
    mtime than the real latest run despite its own recorded started_at being older -- mtime must
    never be allowed to override the manifest's own timestamp."""
    really_older = PipelineManifest(
        run_id="really-older", project_root=str(tmp_path),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    really_newer = PipelineManifest(
        run_id="really-newer", project_root=str(tmp_path),
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    write_manifest(really_older, runs_dir=tmp_path)
    write_manifest(really_newer, runs_dir=tmp_path)
    # Reverse the mtime ordering relative to started_at -- the "really-older" manifest's file is
    # touched to look newest on disk, exactly the "imported/copied run directory" scenario.
    newer_mtime = manifest_path("really-newer", runs_dir=tmp_path).stat().st_mtime
    os.utime(manifest_path("really-older", runs_dir=tmp_path), (newer_mtime + 10, newer_mtime + 10))

    assert latest_run_id(runs_dir=tmp_path) == "really-newer"


def test_latest_run_id_skips_unparseable_manifests_regardless_of_mtime(tmp_path):
    valid = PipelineManifest(run_id="valid", project_root=str(tmp_path))
    write_manifest(valid, runs_dir=tmp_path)
    corrupt_dir = tmp_path / "corrupt"
    corrupt_dir.mkdir()
    corrupt_path = corrupt_dir / "manifest.json"
    corrupt_path.write_text("{not valid json")
    # Make the corrupt one look newest by mtime -- must still lose to the manifest that parses.
    valid_mtime = manifest_path("valid", runs_dir=tmp_path).stat().st_mtime
    os.utime(corrupt_path, (valid_mtime + 10, valid_mtime + 10))

    assert latest_run_id(runs_dir=tmp_path) == "valid"


def test_fingerprint_of_missing_path_is_none(tmp_path):
    assert _fingerprint(None) is None
    assert _fingerprint(tmp_path / "does-not-exist.yaml") is None


def test_fingerprint_changes_when_content_changes(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text("target_domains: [ADAS]")
    first = _fingerprint(path)
    assert first == _fingerprint(path)  # deterministic for unchanged content
    path.write_text("target_domains: [ADAS, Robotics]")
    assert _fingerprint(path) != first


# --- section 4.2: `pipeline --no-scrape [--review]` ---


def _fake_proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _fake_popen(fake_run):
    """Adapts a `subprocess.run`-style fake (`fake_run(cmd, **kwargs) -> CompletedProcess`,
    raising `subprocess.TimeoutExpired` for a timeout scenario — every test below already defines
    one of these) into a fake `Popen`, since `_run_stage_subprocess` now calls `Popen` directly
    rather than `subprocess.run` (process-group cancellation needs the `Popen` object itself — see
    its own docstring). This keeps every existing per-test `fake_run` closure and assertion
    unchanged; only the monkeypatch target moves from `subprocess.run` to `pipeline._popen` (a
    module-level alias `_run_stage_subprocess` calls instead of `subprocess.Popen` directly —
    patching the real `subprocess.Popen` attribute would mutate the one shared `subprocess`
    module every other code in the process also uses, including `runlock.process_start_time`'s
    own real `subprocess.run(["ps", ...])` call — confirmed live, see `_popen`'s docstring in
    pipeline.py). The
    real code's first `communicate(timeout=...)` call resolves `fake_run` directly; a raised
    `TimeoutExpired` is re-raised so `_run_stage_subprocess` takes its kill-then-redrain path
    (`os.killpg` on this fake's made-up pid harmlessly raises `ProcessLookupError`, caught there),
    and the second, no-timeout `communicate()` call — the post-kill drain — returns whatever
    stdout/stderr the test's own `TimeoutExpired` already carried, so no test needs to separately
    describe "the process's output after being killed" from "the output that proves it was stuck"."""

    class _FakeProc:
        def __init__(self, cmd, **kwargs):
            self._cmd = cmd
            self._kwargs = kwargs
            self.pid = 999999999
            self.returncode = 0
            self._timeout_exc: subprocess.TimeoutExpired | None = None
            self._resolved = False

        def communicate(self, timeout=None):
            if not self._resolved:
                self._resolved = True
                try:
                    result = fake_run(self._cmd, **self._kwargs)
                except subprocess.TimeoutExpired as exc:
                    self._timeout_exc = exc
                    raise
                self.returncode = result.returncode
                return result.stdout, result.stderr
            exc = self._timeout_exc
            return (exc.stdout or "", exc.stderr or "") if exc else ("", "")

    return _FakeProc


def _write_archive(path, prefilter_candidates: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"summary": {"prefilter_candidates": prefilter_candidates}, "candidates": [], "source_health": []})
    )


def _write_result_json_for(cmd: list[str], payload: dict) -> None:
    """Every stage subprocess (refilter/review/radar) writes its structured result to whatever
    path follows its own `--result-json` flag — pipeline.py reads that file, not stdout (see
    docs/agent-runtime-audit.md's "structured stage results" finding). A fake `subprocess.run`
    replacement must write it too, the same way the real script would, or pipeline.py correctly
    treats a zero-exit-but-no-result-file subprocess as a stage failure."""
    result_path = Path(cmd[cmd.index("--result-json") + 1])
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(payload))


async def test_no_scrape_rejects_companies_filter():
    """cli.py rejects this combination before ever calling run_pipeline, but run_pipeline
    guards it too — see its own docstring — so a direct (non-CLI) caller can't silently get a
    companies_filter that quietly does nothing."""
    with pytest.raises(ValueError, match="--companies has no effect with --no-scrape"):
        await run_pipeline(Settings(), None, no_scrape=True, companies_filter="honda")


async def test_run_pipeline_finalizes_as_lock_held_instead_of_racing(tmp_path, monkeypatch):
    """A second overlapping `job-hunter pipeline` (or cleanup/refilter/review) run must never
    race SQLite/archive/assessments state — `run_pipeline` shares `run_lock("job-hunter")`
    (see pipeline.py). Held elsewhere, it should finalize the manifest as LOCK_HELD (not leave
    it stuck at RUNNING) and re-raise, rather than falling into the generic FAILED branch."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("job_hunter.pipeline.load_profile", lambda: CandidateProfile())
    monkeypatch.setattr("job_hunter.pipeline.new_run_id", lambda now=None: "fixed-run-id")

    with run_lock("job-hunter"), pytest.raises(RunLockHeld):
        await run_pipeline(Settings(), tmp_path, no_scrape=True, keyword="nope")

    manifest = read_manifest("fixed-run-id")
    assert manifest.status == PipelineStatus.LOCK_HELD
    assert manifest.stage == PipelineStage.DONE
    assert manifest.completed_at is not None
    assert "job-hunter" in manifest.error


async def test_no_scrape_defaults_review_off_and_reaches_radar(tmp_path, monkeypatch):
    """The core --no-scrape/--review asymmetry from section 4.2: with no --review flag, the
    REFILTER stage runs, review is skipped entirely (no subprocess call, manifest.reviewed
    stays None), and the run still reaches radar and DONE with a PARTIAL status."""
    archive = tmp_path / "data" / "searches" / "default_2026-09-17.json"
    _write_archive(archive, prefilter_candidates=5)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("job_hunter.pipeline.load_profile", lambda: CandidateProfile())
    monkeypatch.setattr(
        "job_hunter.pipeline.resolve_search_path", lambda *, search=None, keyword=None: archive
    )

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "scripts/refilter_archive.py" in cmd:
            _write_result_json_for(
                cmd, {"gained": 2, "lost": 1, "diff_report": "data/profile-diff/archive-default_2026-09-17-x.html"}
            )
            return _fake_proc()
        if "scripts/render_radar.py" in cmd:
            _write_result_json_for(cmd, {"report_path": "data/radar/default_2026-09-17.html"})
            return _fake_proc()
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    manifest = await run_pipeline(Settings(), tmp_path, no_scrape=True)

    assert not any("review_with_lm_studio.py" in cmd for cmd in calls)
    assert manifest.reviewed is None
    assert manifest.stage == PipelineStage.DONE
    assert manifest.status == PipelineStatus.PARTIAL
    assert manifest.candidates == 5
    assert manifest.gained == 2
    assert manifest.lost == 1
    assert manifest.diff_report == "data/profile-diff/archive-default_2026-09-17-x.html"
    assert manifest.radar == "data/radar/default_2026-09-17.html"
    assert manifest.archive == str(archive)


async def test_no_scrape_with_review_runs_review_stage_too(tmp_path, monkeypatch):
    archive = tmp_path / "data" / "searches" / "default_2026-09-17.json"
    _write_archive(archive, prefilter_candidates=5)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("job_hunter.pipeline.load_profile", lambda: CandidateProfile())
    monkeypatch.setattr(
        "job_hunter.pipeline.resolve_search_path", lambda *, search=None, keyword=None: archive
    )

    def fake_run(cmd, **kwargs):
        if "scripts/refilter_archive.py" in cmd:
            _write_result_json_for(cmd, {"gained": 0, "lost": 0, "diff_report": None})
            return _fake_proc()
        if "scripts/review_with_lm_studio.py" in cmd:
            _write_result_json_for(cmd, {"reviewed": 2, "skipped_cached": 3, "failed": 0})
            return _fake_proc()
        if "scripts/render_radar.py" in cmd:
            _write_result_json_for(cmd, {"report_path": "data/radar/default_2026-09-17.html"})
            return _fake_proc()
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    manifest = await run_pipeline(Settings(), tmp_path, no_scrape=True, review=True)

    assert manifest.reviewed == 2
    assert manifest.skipped_cached == 3
    assert manifest.status == PipelineStatus.COMPLETE
    assert manifest.stage == PipelineStage.DONE
    assert manifest.radar == "data/radar/default_2026-09-17.html"


async def test_no_scrape_with_zero_candidates_stops_before_review_or_radar(tmp_path, monkeypatch):
    archive = tmp_path / "data" / "searches" / "default_2026-09-17.json"
    _write_archive(archive, prefilter_candidates=0)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("job_hunter.pipeline.load_profile", lambda: CandidateProfile())
    monkeypatch.setattr(
        "job_hunter.pipeline.resolve_search_path", lambda *, search=None, keyword=None: archive
    )

    def fake_run(cmd, **kwargs):
        if "scripts/refilter_archive.py" in cmd:
            _write_result_json_for(cmd, {"gained": 0, "lost": 5, "diff_report": None})
            return _fake_proc()
        raise AssertionError(f"unexpected subprocess call: {cmd} (review/radar must not run)")

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    manifest = await run_pipeline(Settings(), tmp_path, no_scrape=True, review=True)

    assert manifest.status == PipelineStatus.NO_CANDIDATES
    assert manifest.stage == PipelineStage.DONE
    assert manifest.candidates == 0


async def test_no_scrape_refilter_subprocess_failure_finalizes_manifest_as_failed(tmp_path, monkeypatch):
    archive = tmp_path / "data" / "searches" / "default_2026-09-17.json"
    _write_archive(archive, prefilter_candidates=5)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("job_hunter.pipeline.load_profile", lambda: CandidateProfile())
    monkeypatch.setattr(
        "job_hunter.pipeline.resolve_search_path", lambda *, search=None, keyword=None: archive
    )

    def fake_run(cmd, **kwargs):
        assert "scripts/refilter_archive.py" in cmd
        proc = _fake_proc(returncode=1)
        proc.stderr = "Traceback...\nFileNotFoundError: no candidate_profile.yaml\n"
        return proc

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    manifest = await run_pipeline(Settings(), tmp_path, no_scrape=True)

    assert manifest.status == PipelineStatus.FAILED
    assert manifest.stage == PipelineStage.DONE
    assert manifest.error == "FileNotFoundError: no candidate_profile.yaml"
    assert manifest.completed_at is not None


async def test_no_scrape_archive_resolution_failure_finalizes_manifest_instead_of_orphaning_it(
    tmp_path, monkeypatch
):
    """Regression test: resolve_search_path can raise FileNotFoundError (no archive matches the
    given keyword) before any subprocess ever runs — before this fix, the manifest already
    written just above (status=RUNNING) had no code path left to finalize it, so it stayed
    RUNNING forever and `pipeline-status` would report a run that failed instantly as still in
    progress. run_pipeline must still raise (cli.py's own top-level handler is what prints the
    clean error and exit code), but the manifest on disk must reflect FAILED first."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("job_hunter.pipeline.load_profile", lambda: CandidateProfile())
    monkeypatch.setattr("job_hunter.pipeline.new_run_id", lambda now=None: "fixed-run-id")

    def raise_not_found(*, search=None, keyword=None):
        raise FileNotFoundError("No archived search found matching keyword 'nope'")

    monkeypatch.setattr("job_hunter.pipeline.resolve_search_path", raise_not_found)

    with pytest.raises(FileNotFoundError):
        await run_pipeline(Settings(), tmp_path, no_scrape=True, keyword="nope")

    manifest = read_manifest("fixed-run-id")
    assert manifest.status == PipelineStatus.FAILED
    assert manifest.stage == PipelineStage.DONE
    assert "No archived search found" in manifest.error
    assert manifest.completed_at is not None


async def test_lock_inherited_stages_receive_the_real_live_lock_token(tmp_path, monkeypatch):
    """docs/agent-runtime-audit.md's "lock bypass is caller-controlled" finding: LOCK_INHERITED_ENV
    must carry the actual current run_lock("job-hunter") token, not a bare "1" -- verifies the env
    dict `_run_stage_subprocess` builds for the refilter/review stages (lock_inherited=True)
    against what's really on disk in data/locks/job-hunter.lock while `run_pipeline`'s own
    `with run_lock("job-hunter"):` block is active, and that the radar stage (never
    lock_inherited) gets no such env override at all."""
    from job_hunter.runlock import LOCK_INHERITED_ENV

    archive = tmp_path / "data" / "searches" / "default_2026-09-17.json"
    _write_archive(archive, prefilter_candidates=5)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("job_hunter.pipeline.load_profile", lambda: CandidateProfile())
    monkeypatch.setattr(
        "job_hunter.pipeline.resolve_search_path", lambda *, search=None, keyword=None: archive
    )

    seen_envs: dict[str, dict | None] = {}

    def fake_run(cmd, **kwargs):
        if "scripts/refilter_archive.py" in cmd:
            seen_envs["refilter"] = kwargs.get("env")
            live_lock = tmp_path / "data" / "locks" / "job-hunter.lock"
            assert live_lock.exists(), "parent's run_lock must still be held during a stage call"
            seen_envs["live_token_at_refilter_time"] = live_lock.read_text().splitlines()[3]
            _write_result_json_for(cmd, {"gained": 0, "lost": 0, "diff_report": None})
            return _fake_proc()
        if "scripts/review_with_lm_studio.py" in cmd:
            seen_envs["review"] = kwargs.get("env")
            _write_result_json_for(cmd, {"reviewed": 1, "skipped_cached": 0, "failed": 0})
            return _fake_proc()
        if "scripts/render_radar.py" in cmd:
            seen_envs["radar"] = kwargs.get("env")
            _write_result_json_for(cmd, {"report_path": "data/radar/default_2026-09-17.html"})
            return _fake_proc()
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    manifest = await run_pipeline(Settings(), tmp_path, no_scrape=True, review=True)

    assert manifest.status == PipelineStatus.COMPLETE
    real_token = seen_envs["live_token_at_refilter_time"]
    assert real_token
    assert seen_envs["refilter"][LOCK_INHERITED_ENV] == real_token
    assert seen_envs["review"][LOCK_INHERITED_ENV] == real_token
    assert seen_envs["radar"] is None


async def test_no_scrape_first_stage_is_refilter_not_search(tmp_path, monkeypatch):
    """The manifest's very first written stage in --no-scrape mode is REFILTER, replacing
    SEARCH as the live-search branch's first stage — read back before the run finishes to
    confirm the initial write itself used the right enum value, not just the final one."""
    archive = tmp_path / "data" / "searches" / "default_2026-09-17.json"
    _write_archive(archive, prefilter_candidates=0)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("job_hunter.pipeline.load_profile", lambda: CandidateProfile())
    monkeypatch.setattr(
        "job_hunter.pipeline.resolve_search_path", lambda *, search=None, keyword=None: archive
    )

    seen_stages: list[PipelineStage] = []
    original_write_manifest = write_manifest

    def spy_write_manifest(manifest, **kwargs):
        seen_stages.append(manifest.stage)
        return original_write_manifest(manifest, **kwargs)

    monkeypatch.setattr("job_hunter.pipeline.write_manifest", spy_write_manifest)

    def fake_run(cmd, **kwargs):
        _write_result_json_for(cmd, {"gained": 0, "lost": 0, "diff_report": None})
        return _fake_proc()

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    await run_pipeline(Settings(), tmp_path, no_scrape=True)

    assert seen_stages[0] == PipelineStage.REFILTER


# --- structured stage results / timeouts (docs/agent-runtime-audit.md) ---


def _base_setup(tmp_path, monkeypatch, *, prefilter_candidates: int = 5):
    archive = tmp_path / "data" / "searches" / "default_2026-09-17.json"
    _write_archive(archive, prefilter_candidates=prefilter_candidates)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("job_hunter.pipeline.load_profile", lambda: CandidateProfile())
    monkeypatch.setattr(
        "job_hunter.pipeline.resolve_search_path", lambda *, search=None, keyword=None: archive
    )
    return archive


async def test_run_pipeline_records_its_own_pid(tmp_path, monkeypatch):
    _base_setup(tmp_path, monkeypatch, prefilter_candidates=0)

    def fake_run(cmd, **kwargs):
        _write_result_json_for(cmd, {"gained": 0, "lost": 0, "diff_report": None})
        return _fake_proc()

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    manifest = await run_pipeline(Settings(), tmp_path, no_scrape=True)

    assert manifest.pid == os.getpid()


async def test_refilter_missing_result_json_is_treated_as_failure_not_zero(tmp_path, monkeypatch):
    """A stage that exits 0 but never wrote its --result-json is a broken contract, not a
    legitimate empty result — must surface as FAILED, never silently zero-defaulted."""
    _base_setup(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        assert "scripts/refilter_archive.py" in cmd
        return _fake_proc()  # exits 0, writes nothing to --result-json

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    manifest = await run_pipeline(Settings(), tmp_path, no_scrape=True)

    assert manifest.status == PipelineStatus.FAILED
    assert "wrote no readable result" in manifest.error


async def test_review_missing_result_json_is_treated_as_failure_not_zero(tmp_path, monkeypatch):
    _base_setup(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if "scripts/refilter_archive.py" in cmd:
            _write_result_json_for(cmd, {"gained": 0, "lost": 0, "diff_report": None})
            return _fake_proc()
        if "scripts/review_with_lm_studio.py" in cmd:
            return _fake_proc()  # exits 0, writes nothing to --result-json
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    manifest = await run_pipeline(Settings(), tmp_path, no_scrape=True, review=True)

    assert manifest.status == PipelineStatus.FAILED
    assert "wrote no readable result" in manifest.error
    assert manifest.reviewed is None


async def test_radar_missing_result_json_is_treated_as_failure_not_zero(tmp_path, monkeypatch):
    _base_setup(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if "scripts/refilter_archive.py" in cmd:
            _write_result_json_for(cmd, {"gained": 0, "lost": 0, "diff_report": None})
            return _fake_proc()
        if "scripts/render_radar.py" in cmd:
            return _fake_proc()  # exits 0, writes nothing to --result-json
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    manifest = await run_pipeline(Settings(), tmp_path, no_scrape=True)

    assert manifest.status == PipelineStatus.FAILED
    assert "wrote no readable result" in manifest.error
    assert manifest.radar is None


async def test_review_stage_timeout_finalizes_manifest_as_timed_out(tmp_path, monkeypatch):
    _base_setup(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if "scripts/refilter_archive.py" in cmd:
            _write_result_json_for(cmd, {"gained": 0, "lost": 0, "diff_report": None})
            return _fake_proc()
        if "scripts/review_with_lm_studio.py" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1, output="", stderr="stuck calling LM Studio\n")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    settings = Settings()
    settings.pipeline.stage_timeout_seconds = 1
    manifest = await run_pipeline(settings, tmp_path, no_scrape=True, review=True)

    assert manifest.status == PipelineStatus.TIMED_OUT
    assert manifest.stage == PipelineStage.DONE
    assert "stuck calling LM Studio" in manifest.error
    assert manifest.completed_at is not None


async def test_radar_stage_timeout_sets_status_and_lets_caller_finalize(tmp_path, monkeypatch):
    """Unlike review's self-finalizing timeout path, radar's own convention leaves
    stage/completed_at/the final write to the caller (which always runs right after) — confirm
    the run still reaches DONE with a real completed_at rather than being left half-finalized."""
    _base_setup(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if "scripts/refilter_archive.py" in cmd:
            _write_result_json_for(cmd, {"gained": 0, "lost": 0, "diff_report": None})
            return _fake_proc()
        if "scripts/render_radar.py" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1, output="", stderr="")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    settings = Settings()
    settings.pipeline.stage_timeout_seconds = 1
    manifest = await run_pipeline(settings, tmp_path, no_scrape=True)

    assert manifest.status == PipelineStatus.TIMED_OUT
    assert manifest.stage == PipelineStage.DONE
    assert manifest.completed_at is not None


async def test_refilter_stage_timeout_finalizes_manifest_as_timed_out(tmp_path, monkeypatch):
    _base_setup(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        assert "scripts/refilter_archive.py" in cmd
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1, output="", stderr="refilter hung\n")

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    settings = Settings()
    settings.pipeline.stage_timeout_seconds = 1
    manifest = await run_pipeline(settings, tmp_path, no_scrape=True)

    assert manifest.status == PipelineStatus.TIMED_OUT
    assert manifest.stage == PipelineStage.DONE
    assert "refilter hung" in manifest.error
    assert manifest.completed_at is not None


# --- TimeoutExpired can carry raw bytes even under text=True (docs/agent-runtime-audit.md) ---


def test_decode_timeout_output_passes_through_str():
    assert _decode_timeout_output("already text") == "already text"


def test_decode_timeout_output_none_becomes_empty_string():
    assert _decode_timeout_output(None) == ""


def test_decode_timeout_output_decodes_valid_utf8_bytes():
    assert _decode_timeout_output("stuck — waiting".encode()) == "stuck — waiting"


def test_decode_timeout_output_replaces_truncated_utf8_instead_of_raising():
    """A real kill can land mid multi-byte character -- must never raise, since this runs inside
    an exception handler that's already unwinding a timeout; raising here would replace a clean
    TIMED_OUT finalization with an unrelated crash."""
    truncated = "stuck —".encode()[:-1]  # chop the em dash's last byte
    result = _decode_timeout_output(truncated)
    assert "�" in result
    assert result.startswith("stuck ")


async def test_review_stage_timeout_with_real_bytes_output_does_not_crash_manifest_write(
    tmp_path, monkeypatch
):
    """Regression test for the exact bug docs/agent-runtime-audit.md's re-audit caught:
    `subprocess.TimeoutExpired.stdout`/`.stderr` can be raw `bytes` even when `text=True` was
    passed to `subprocess.run` -- confirmed live on this project's own Python. Earlier tests only
    ever mocked `TimeoutExpired` with `str` output, which never exercised this path. Truncated,
    invalid-UTF-8 bytes (a kill landing mid multi-byte character) used to reach
    `PipelineManifest.error` and blow up `write_manifest()`'s `model_dump_json()` call with
    `PydanticSerializationError` instead of cleanly recording TIMED_OUT."""
    _base_setup(tmp_path, monkeypatch)

    truncated_stderr = "stuck calling —".encode()[:-1]  # invalid UTF-8: cut mid multi-byte char

    def fake_run(cmd, **kwargs):
        if "scripts/refilter_archive.py" in cmd:
            _write_result_json_for(cmd, {"gained": 0, "lost": 0, "diff_report": None})
            return _fake_proc()
        if "scripts/review_with_lm_studio.py" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1, output=b"partial stdout", stderr=truncated_stderr)
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr("job_hunter.pipeline._popen", _fake_popen(fake_run))

    settings = Settings()
    settings.pipeline.stage_timeout_seconds = 1
    manifest = await run_pipeline(settings, tmp_path, no_scrape=True, review=True)

    assert manifest.status == PipelineStatus.TIMED_OUT
    assert isinstance(manifest.error, str)
    assert "stuck calling" in manifest.error
    # The manifest write itself must have succeeded (no PydanticSerializationError) -- confirm by
    # reading it back from disk, not just from the in-memory object.
    reread = read_manifest(manifest.run_id)
    assert reread.status == PipelineStatus.TIMED_OUT


# --- real process-tree integration test (docs/agent-runtime-audit.md's "process-group
# cancellation" finding) -- every test above mocks subprocess.Popen, which is the right tool for
# manifest/status-transition coverage but structurally cannot prove a whole process *group* gets
# killed, since the fake never has real descendant processes to check. This one runs a genuine
# subprocess with a genuine grandchild and confirms the grandchild is actually gone afterward. ---


def test_stage_timeout_kills_the_whole_process_group_including_grandchildren(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "scripts" / "spawn_grandchild_and_hang.py"
    pid_file = tmp_path / "grandchild.pid"

    with pytest.raises(_StageTimedOut):
        _run_stage_subprocess(
            [sys.executable, str(fixture), str(pid_file)],
            project_root=Path.cwd(),
            timeout=1,
        )

    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
    # SIGKILL delivery/reaping is not instantaneous -- poll briefly instead of asserting the very
    # instant _run_stage_subprocess returns, to avoid a rare false failure on a loaded machine.
    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.1)
    assert not alive, f"grandchild pid {grandchild_pid} was still alive after the stage timeout killed its group"
