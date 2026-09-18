import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunter.config import CandidateProfile, Settings
from job_hunter.models import PipelineManifest, PipelineStage, PipelineStatus
from job_hunter.pipeline import (
    _fingerprint,
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

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

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

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

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

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

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

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

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

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

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

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

    manifest = await run_pipeline(Settings(), tmp_path, no_scrape=True)

    assert manifest.pid == os.getpid()


async def test_refilter_missing_result_json_is_treated_as_failure_not_zero(tmp_path, monkeypatch):
    """A stage that exits 0 but never wrote its --result-json is a broken contract, not a
    legitimate empty result — must surface as FAILED, never silently zero-defaulted."""
    _base_setup(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        assert "scripts/refilter_archive.py" in cmd
        return _fake_proc()  # exits 0, writes nothing to --result-json

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

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

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

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

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

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

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

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

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

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

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

    settings = Settings()
    settings.pipeline.stage_timeout_seconds = 1
    manifest = await run_pipeline(settings, tmp_path, no_scrape=True)

    assert manifest.status == PipelineStatus.TIMED_OUT
    assert manifest.stage == PipelineStage.DONE
    assert "refilter hung" in manifest.error
    assert manifest.completed_at is not None
