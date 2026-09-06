import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunter.config import CandidateProfile
from job_hunter.models import Job, LocationConfidence
from job_hunter.storage import Storage

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import diff_profile  # noqa: E402
from diff_profile import (  # noqa: E402
    ProfileDiffError,
    _advance_baseline,
    _field_term_diffs,
    _non_filter_field_diffs,
    _read_only_jobs,
    _render_field_terms,
    _rollback_baseline,
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


# --- field term diffs (for-reference terms in the profile-diff report) ---

def test_field_term_diffs_reports_added_removed_and_unchanged():
    before = CandidateProfile(
        target_domains=["ADAS", "verification"],
        exclude_terms=["fullstack"],
    )
    after = CandidateProfile(
        target_domains=["ADAS", "perception"],
        exclude_terms=["fullstack"],
    )
    diffs = {d.field: d for d in _field_term_diffs(before, after)}

    domains = diffs["target_domains"]
    assert domains.added == ["perception"]
    assert domains.removed == ["verification"]
    assert domains.unchanged == ["ADAS"]

    excludes = diffs["exclude_terms"]
    assert excludes.added == [] and excludes.removed == [] and excludes.unchanged == ["fullstack"]

    # Every filtering field is represented, even ones neither profile touches.
    assert {d.field for d in _field_term_diffs(before, after)} == {
        "target_domains", "target_title_terms", "exclude_title_terms",
        "exclude_terms", "soft_exclude_terms", "strong_relevance_terms",
    }


def test_field_term_diffs_is_case_insensitive():
    """Matches apply_edits/evaluate_prefilter's own case-insensitive term matching — a term
    re-cased between profiles is "unchanged", not an add+remove pair, but the after-profile's
    casing is what's displayed."""
    before = CandidateProfile(exclude_terms=["CAD"])
    after = CandidateProfile(exclude_terms=["cad"])
    diffs = {d.field: d for d in _field_term_diffs(before, after)}
    excludes = diffs["exclude_terms"]
    assert excludes.added == [] and excludes.removed == []
    assert excludes.unchanged == ["cad"]


def test_field_term_diffs_identical_profiles_have_no_added_or_removed():
    profile = CandidateProfile(target_domains=["ADAS"], exclude_title_terms=["intern"])
    diffs = _field_term_diffs(profile, profile)
    assert all(d.added == [] and d.removed == [] for d in diffs)


def test_render_field_terms_renders_added_removed_and_unchanged_tags():
    before = CandidateProfile(target_domains=["ADAS", "verification"])
    after = CandidateProfile(target_domains=["ADAS", "perception"])
    html_out = _render_field_terms(_field_term_diffs(before, after))
    assert "term-tag-added" in html_out and "perception" in html_out
    assert "term-tag-removed" in html_out and "verification" in html_out
    assert ">ADAS<" in html_out  # unchanged term, plain tag


def test_render_field_terms_empty_profiles_shows_empty_message():
    profile = CandidateProfile(exclude_title_terms=[])  # override the default intern/co-op
    html_out = _render_field_terms(_field_term_diffs(profile, profile))
    assert "No filtering terms configured" in html_out


def test_compute_diff_includes_field_term_diffs(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path):
        pass
    before = CandidateProfile(target_domains=["ADAS"])
    after = CandidateProfile(target_domains=["ADAS", "perception"])
    result = compute_diff(
        before=before, after=after, database_path=db_path, max_age_days=30, keywords=None,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )
    domains = next(d for d in result.field_term_diffs if d.field == "target_domains")
    assert domains.added == ["perception"]
    assert domains.unchanged == ["ADAS"]


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


# --- check-mode baseline snapshot (--rollback-baseline and the implicit no-flags mode) ---

def _use_scratch_snapshot(monkeypatch, tmp_path):
    """Point the module's snapshot paths at tmp_path instead of the real data/ directory."""
    monkeypatch.setattr(diff_profile, "_SNAPSHOT_PATH", tmp_path / "snapshot.yaml")
    monkeypatch.setattr(diff_profile, "_SNAPSHOT_PREV_PATH", tmp_path / "snapshot.prev.yaml")


def test_advance_baseline_first_call_has_no_prev(tmp_path, monkeypatch):
    _use_scratch_snapshot(monkeypatch, tmp_path)
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("target_domains: [ADAS]\n")

    _advance_baseline(profile_path)

    assert diff_profile._SNAPSHOT_PATH.read_text() == "target_domains: [ADAS]\n"
    assert not diff_profile._SNAPSHOT_PREV_PATH.exists()


def test_advance_baseline_second_call_backs_up_the_first(tmp_path, monkeypatch):
    _use_scratch_snapshot(monkeypatch, tmp_path)
    profile_path = tmp_path / "profile.yaml"

    profile_path.write_text("target_domains: [ADAS]\n")
    _advance_baseline(profile_path)
    profile_path.write_text("target_domains: [ADAS, perception]\n")
    _advance_baseline(profile_path)

    assert diff_profile._SNAPSHOT_PATH.read_text() == "target_domains: [ADAS, perception]\n"
    assert diff_profile._SNAPSHOT_PREV_PATH.read_text() == "target_domains: [ADAS]\n"


def test_rollback_baseline_returns_false_with_nothing_to_roll_back_to(tmp_path, monkeypatch):
    _use_scratch_snapshot(monkeypatch, tmp_path)
    assert _rollback_baseline() is False


def test_rollback_baseline_is_a_symmetric_swap(tmp_path, monkeypatch):
    """Regression-shaped test for the exact property that makes one rollback copy safe to rely
    on: rolling back twice in a row is a no-op, not a double-undo that loses the most recent
    state entirely."""
    _use_scratch_snapshot(monkeypatch, tmp_path)
    profile_path = tmp_path / "profile.yaml"

    profile_path.write_text("target_domains: [ADAS]\n")
    _advance_baseline(profile_path)  # snapshot=ADAS, no prev
    profile_path.write_text("target_domains: [ADAS, perception]\n")
    _advance_baseline(profile_path)  # snapshot=ADAS+perception, prev=ADAS

    assert _rollback_baseline() is True
    assert diff_profile._SNAPSHOT_PATH.read_text() == "target_domains: [ADAS]\n"
    assert diff_profile._SNAPSHOT_PREV_PATH.read_text() == "target_domains: [ADAS, perception]\n"

    assert _rollback_baseline() is True  # rolling back again restores the rolled-back state
    assert diff_profile._SNAPSHOT_PATH.read_text() == "target_domains: [ADAS, perception]\n"
    assert diff_profile._SNAPSHOT_PREV_PATH.read_text() == "target_domains: [ADAS]\n"


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
