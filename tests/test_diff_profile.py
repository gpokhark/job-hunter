import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunter.config import CandidateProfile
from job_hunter.models import Job, LocationConfidence
from job_hunter.storage import Storage

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from diff_profile import (  # noqa: E402
    ProfileDiffError,
    _non_filter_field_diffs,
    _read_only_jobs,
    _row_to_job,
    apply_edits,
    compute_diff,
)


def make_job(**updates):
    values = dict(
        source_key="acme", source_platform="test", company="Acme", job_id="1",
        title="ADAS Engineer", url="https://example.com/1",
        us_eligible=True, location_confidence=LocationConfidence.HIGH,
    )
    values.update(updates)
    return Job(**values)


# --- apply_edits / validation ---

def test_apply_edits_add_and_remove():
    before = CandidateProfile(target_domains=["ADAS"], exclude_terms=["fullstack"])
    after = apply_edits(before, adds=["soft_exclude_terms:post silicon"], removes=["exclude_terms:fullstack"])
    assert after.soft_exclude_terms == ["post silicon"]
    assert after.exclude_terms == []
    assert before.exclude_terms == ["fullstack"]  # before untouched


def test_apply_edits_rejects_unknown_field():
    before = CandidateProfile()
    with pytest.raises(ProfileDiffError, match="not a patchable field"):
        apply_edits(before, adds=["resume_path:foo"], removes=[])


def test_apply_edits_rejects_blank_term():
    before = CandidateProfile()
    with pytest.raises(ProfileDiffError, match="must not be blank"):
        apply_edits(before, adds=["exclude_terms:  "], removes=[])


def test_apply_edits_rejects_conflicting_add_remove():
    before = CandidateProfile(exclude_terms=["cad"])
    with pytest.raises(ProfileDiffError, match="same field\\+term"):
        apply_edits(before, adds=["exclude_terms:cad"], removes=["exclude_terms:cad"])


def test_apply_edits_rejects_removing_term_not_present():
    before = CandidateProfile(exclude_terms=["cad"])
    with pytest.raises(ProfileDiffError, match="not found"):
        apply_edits(before, adds=[], removes=["exclude_terms:nonexistent"])


def test_apply_edits_add_is_case_insensitive_noop_if_already_present(capsys):
    before = CandidateProfile(exclude_terms=["CAD"])
    after = apply_edits(before, adds=["exclude_terms:cad"], removes=[])
    assert after.exclude_terms == ["CAD"]
    assert "already present" in capsys.readouterr().err


def test_non_filter_field_diffs_reports_other_field_changes():
    before = CandidateProfile(minimum_recommendation_score=75)
    after = CandidateProfile(minimum_recommendation_score=80)
    diffs = _non_filter_field_diffs(before, after)
    assert any("minimum_recommendation_score" in d for d in diffs)


# --- row conversion ---

def test_row_to_job_maps_canonical_url_to_url(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(url="https://example.com/real-job"))

    rows = _read_only_jobs(db_path)
    assert len(rows) == 1
    job = _row_to_job(rows[0])
    assert job.url == "https://example.com/real-job"


# --- compute_diff ---

def test_compute_diff_classifies_gained_lost_retained_still_excluded(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(job_id="retained", title="ADAS Engineer"))
        storage.upsert_job(make_job(job_id="still-excluded", title="Marketing Manager"))
        storage.upsert_job(make_job(job_id="gained", title="Perception Engineer"))
        storage.upsert_job(make_job(job_id="lost", title="Design Verification Engineer"))

    before = CandidateProfile(target_domains=["ADAS", "verification"])
    after = CandidateProfile(
        target_domains=["ADAS", "perception"],
        soft_exclude_terms=["design verification"],
    )

    result = compute_diff(
        before=before, after=after, database_path=db_path, max_age_days=30, keywords=None,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )
    assert result.retained == 1
    assert result.still_excluded == 1
    assert [c.job.job_id for c in result.gained] == ["gained"]
    assert [c.job.job_id for c in result.lost] == ["lost"]


def test_compute_diff_identical_profiles_produce_no_changes(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(job_id="1", title="ADAS Engineer"))
        storage.upsert_job(make_job(job_id="2", title="Marketing Manager"))

    profile = CandidateProfile(target_domains=["ADAS"])
    result = compute_diff(
        before=profile, after=profile, database_path=db_path, max_age_days=30, keywords=None,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )
    assert result.gained == []
    assert result.lost == []
    assert result.retained == 1
    assert result.still_excluded == 1


def test_compute_diff_swapping_profiles_swaps_gains_and_losses(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(job_id="1", title="Perception Engineer"))

    narrow = CandidateProfile(target_domains=["ADAS"])
    broad = CandidateProfile(target_domains=["ADAS", "perception"])
    now = datetime(2026, 9, 5, tzinfo=UTC)

    forward = compute_diff(before=narrow, after=broad, database_path=db_path, max_age_days=30, keywords=None, now=now)
    backward = compute_diff(before=broad, after=narrow, database_path=db_path, max_age_days=30, keywords=None, now=now)

    assert [c.job.job_id for c in forward.gained] == [c.job.job_id for c in backward.lost]
    assert forward.lost == [] and backward.gained == []


def test_compute_diff_keyword_mode_ignores_target_domains_edits(tmp_path):
    """The exact semantic the plan calls out explicitly: --keyword replaces
    target_domains/target_title_terms entirely, so an edit to those fields shows no effect
    when a keyword is given."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(job_id="1", title="Robotics Engineer"))

    before = CandidateProfile(target_domains=["totally-unrelated"])
    after = CandidateProfile(target_domains=["also-unrelated-but-different"])
    result = compute_diff(
        before=before, after=after, database_path=db_path, max_age_days=30,
        keywords=["robotics"], now=datetime(2026, 9, 5, tzinfo=UTC),
    )
    assert result.gained == []
    assert result.lost == []
    assert result.retained == 1  # passes both, via the keyword override, regardless of target_domains


def test_compute_diff_positive_gate_unrestricted_warning(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job())

    before = CandidateProfile(target_domains=["ADAS"])
    after = CandidateProfile(target_domains=[], target_title_terms=[])
    result = compute_diff(
        before=before, after=after, database_path=db_path, max_age_days=30, keywords=None,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )
    assert result.positive_gate_unrestricted is True


def test_compute_diff_recency_boundary_behaves_identically_before_and_after(tmp_path):
    """A job posted exactly at the recency cutoff is either kept by both profiles or dropped
    from consideration entirely by both — recency is a fixed precondition, never itself part
    of what's being diffed."""
    db_path = tmp_path / "jobs.sqlite3"
    now = datetime(2026, 9, 5, tzinfo=UTC)
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(posted_at=now))  # exactly at the boundary, within cutoff

    before = CandidateProfile(target_domains=["ADAS"])
    after = CandidateProfile(target_domains=["verification"])
    result = compute_diff(
        before=before, after=after, database_path=db_path, max_age_days=30, keywords=None, now=now,
    )
    assert result.total_considered == 1
