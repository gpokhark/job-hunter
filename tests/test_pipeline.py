import json
import os
import subprocess
from datetime import UTC, datetime

import pytest

from job_hunter.config import CandidateProfile, Settings
from job_hunter.models import PipelineManifest, PipelineStage, PipelineStatus
from job_hunter.pipeline import (
    _fingerprint,
    _parse_radar_path,
    _parse_refilter_output,
    _parse_review_output,
    latest_run_id,
    manifest_path,
    new_run_id,
    read_manifest,
    run_pipeline,
    write_manifest,
)


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


def test_parse_review_output_reads_the_summary_line_and_per_job_failures():
    stdout = (
        "Reviewing [1/3] Acme — Engineer ...\n"
        "  score=82 recommended=True\n"
        "Reviewing [2/3] Acme — Analyst ...\n"
        "  skipped: model returned invalid JSON\n"
        "Reviewing [3/3] Acme — Manager ...\n"
        "  score=61 recommended=False\n"
        "Reviewed 2 job(s); skipped 5 already-assessed (unchanged) job(s).\n"
    )
    reviewed, skipped_cached, failures = _parse_review_output(stdout)
    assert reviewed == 2
    assert skipped_cached == 5
    assert failures == 1


def test_parse_review_output_with_no_summary_line_defaults_to_zero():
    reviewed, skipped_cached, failures = _parse_review_output("")
    assert (reviewed, skipped_cached, failures) == (0, 0, 0)


def test_parse_radar_path_extracts_the_written_path():
    stdout = "Wrote data/radar/adas_2026-09-17.html | strong=3 review=1 below_50=0 never_reviewed=0 (failed=0)\n"
    assert _parse_radar_path(stdout) == "data/radar/adas_2026-09-17.html"


def test_parse_radar_path_with_no_match_is_none():
    assert _parse_radar_path("something else entirely") is None


# --- section 4.2: `pipeline --no-scrape [--review]` ---


def test_parse_refilter_output_reads_gained_lost_and_report_path():
    stdout = (
        "Re-filtered data/searches/default_2026-09-17.json -> data/searches/default_2026-09-17.json: "
        "45 candidate(s) (was 44, 3 removed, 4 gained)\n"
        "Wrote data/profile-diff/archive-default_2026-09-17-2026-09-17-T-10-00-00.html\n"
    )
    gained, lost, diff_report = _parse_refilter_output(stdout)
    assert (gained, lost) == (4, 3)
    assert diff_report == "data/profile-diff/archive-default_2026-09-17-2026-09-17-T-10-00-00.html"


def test_parse_refilter_output_with_no_report_line_has_no_diff_report():
    """--no-report skips the second "Wrote ..." line entirely — must not be confused with a
    parse failure (None), which is exactly what this returns for a genuinely missing report."""
    stdout = "Re-filtered x -> x: 10 candidate(s) (was 10, 0 removed, 0 gained)\n"
    gained, lost, diff_report = _parse_refilter_output(stdout)
    assert (gained, lost) == (0, 0)
    assert diff_report is None


def test_parse_refilter_output_with_no_summary_line_is_none_not_zero():
    """Unlike _parse_review_output's 0-defaulting, a missing refilter summary line must show up
    as None (genuinely couldn't tell), not a legitimate-looking zero."""
    gained, lost, diff_report = _parse_refilter_output("")
    assert (gained, lost, diff_report) == (None, None, None)


def _fake_proc(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _write_archive(path, prefilter_candidates: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"summary": {"prefilter_candidates": prefilter_candidates}, "candidates": [], "source_health": []})
    )


async def test_no_scrape_rejects_companies_filter():
    """cli.py rejects this combination before ever calling run_pipeline, but run_pipeline
    guards it too — see its own docstring — so a direct (non-CLI) caller can't silently get a
    companies_filter that quietly does nothing."""
    with pytest.raises(ValueError, match="--companies has no effect with --no-scrape"):
        await run_pipeline(Settings(), None, no_scrape=True, companies_filter="honda")


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
            return _fake_proc(
                stdout=(
                    f"Re-filtered {archive} -> {archive}: 5 candidate(s) (was 4, 1 removed, 2 gained)\n"
                    "Wrote data/profile-diff/archive-default_2026-09-17-x.html\n"
                )
            )
        if "scripts/render_radar.py" in cmd:
            return _fake_proc(
                stdout="Wrote data/radar/default_2026-09-17.html | strong=1 review=1 below_50=0 never_reviewed=3 source_issues=0 (failed=0)\n"
            )
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
            return _fake_proc(
                stdout=f"Re-filtered {archive} -> {archive}: 5 candidate(s) (was 5, 0 removed, 0 gained)\n"
            )
        if "scripts/review_with_lm_studio.py" in cmd:
            return _fake_proc(stdout="Reviewed 2 job(s); skipped 3 already-assessed (unchanged) job(s).\n")
        if "scripts/render_radar.py" in cmd:
            return _fake_proc(
                stdout="Wrote data/radar/default_2026-09-17.html | strong=1 review=1 below_50=0 never_reviewed=0 source_issues=0 (failed=0)\n"
            )
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
            return _fake_proc(
                stdout=f"Re-filtered {archive} -> {archive}: 0 candidate(s) (was 5, 5 removed, 0 gained)\n"
            )
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
        return _fake_proc(stdout=f"Re-filtered {archive} -> {archive}: 0 candidate(s) (was 0, 0 removed, 0 gained)\n")

    monkeypatch.setattr("job_hunter.pipeline.subprocess.run", fake_run)

    await run_pipeline(Settings(), tmp_path, no_scrape=True)

    assert seen_stages[0] == PipelineStage.REFILTER
