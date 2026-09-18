import os
from datetime import UTC, datetime

import pytest

from job_hunter.models import PipelineManifest, PipelineStage, PipelineStatus
from job_hunter.pipeline import (
    _fingerprint,
    _parse_radar_path,
    _parse_review_output,
    latest_run_id,
    manifest_path,
    new_run_id,
    read_manifest,
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
