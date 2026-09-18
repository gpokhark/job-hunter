"""Unit tests for job_hunter.active_pool — migrated from the coverage
tests/test_refilter_archive.py used to carry for its old private `_active_jobs()`, now split
across `raw_active_jobs()` (the unfiltered, optionally multi-source query refilter_archive.py's
own `refilter()` still needs) and `source_jobs()` (the new, fully-filtered, single-source entry
point render_radar.py's stale-source fallback calls) — see
docs/pipeline-refilter-stale-source-plan.md section 4.4."""

from datetime import UTC, datetime

from job_hunter.active_pool import raw_active_jobs, source_jobs
from job_hunter.config import CandidateProfile
from job_hunter.models import Assessment, Job, LocationConfidence
from job_hunter.storage import Storage


def make_job(**updates):
    values = dict(
        source_key="apple", source_platform="test", company="Apple", job_id="1",
        title="Design Verification Engineer", url="https://example.com/1",
        us_eligible=True, location_confidence=LocationConfidence.HIGH,
    )
    values.update(updates)
    return Job(**values)


def test_raw_active_jobs_returns_every_active_us_eligible_job_unfiltered(tmp_path):
    """No prefilter/recency applied at all — this is the raw pool refilter_archive.py's own
    filtering loop still needs so it can tell a prefilter failure apart from a recency one."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(job_id="1", title="Totally Unrelated Role"))
        storage.upsert_job(make_job(job_id="2", posted_at=datetime(2020, 1, 1, tzinfo=UTC)))

    jobs = raw_active_jobs(db_path)
    assert {job.job_id for job in jobs} == {"1", "2"}


def test_raw_active_jobs_restricts_to_source_scope(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(source_key="apple", job_id="1"))
        storage.upsert_job(make_job(source_key="waymo", job_id="2"))

    jobs = raw_active_jobs(db_path, {"apple"})
    assert [job.source_key for job in jobs] == ["apple"]


def test_raw_active_jobs_attaches_prior_assessment_only_when_content_hash_matches(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(job_id="1", content_hash="abc"))
        storage.upsert_assessment(
            Assessment(
                source_key="apple", job_id="1", company="Apple", title="x",
                url="https://example.com/1", score=80, recommended=True, content_hash="abc",
            )
        )
        storage.upsert_job(make_job(job_id="2", content_hash="stale-now"))
        storage.upsert_assessment(
            Assessment(
                source_key="apple", job_id="2", company="Apple", title="x",
                url="https://example.com/2", score=80, recommended=True,
                content_hash="stale-assessment",
            )
        )

    jobs = {job.job_id: job for job in raw_active_jobs(db_path)}
    assert jobs["1"].prior_assessment is not None
    assert jobs["1"].prior_assessment.score == 80
    assert jobs["2"].prior_assessment is None  # content_hash mismatch -> treated as unassessed


def test_source_jobs_applies_prefilter_and_recency_scoped_to_one_source(tmp_path):
    """The behavior render_radar.py's stale-source fallback actually depends on: a single
    source's pool, already filtered by both checks, with no other source's jobs mixed in even
    if they'd also pass."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(
            make_job(source_key="waymo", job_id="1", title="AV Perception Engineer")
        )
        storage.upsert_job(
            make_job(source_key="waymo", job_id="2", title="Totally Unrelated Role")
        )
        # Same title as job 1, but a different source — must never leak into waymo's result.
        storage.upsert_job(
            make_job(source_key="apple", job_id="3", title="AV Perception Engineer")
        )

    profile = CandidateProfile(target_domains=["perception"])
    jobs = source_jobs(db_path, "waymo", profile, 30, now=datetime(2026, 9, 5, tzinfo=UTC))
    assert [job.job_id for job in jobs] == ["1"]


def test_source_jobs_excludes_stale_postings(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(
            make_job(
                source_key="waymo", job_id="1", title="Engineer",
                posted_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

    profile = CandidateProfile(target_domains=["engineer"])
    jobs = source_jobs(db_path, "waymo", profile, 30, now=datetime(2026, 9, 5, tzinfo=UTC))
    assert jobs == []


def test_source_jobs_honors_a_keyword_override(tmp_path):
    """`keywords`, when given, fully replaces the profile's own positive-match terms for this
    one call — same meaning as `job-hunter search --keyword` everywhere else in this project,
    not a narrowing of the profile's terms."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(source_key="waymo", job_id="1", title="Robotics Lead"))

    profile = CandidateProfile(target_domains=["something-unrelated"])
    now = datetime(2026, 9, 5, tzinfo=UTC)
    assert source_jobs(db_path, "waymo", profile, 30, now=now) == []
    matched = source_jobs(db_path, "waymo", profile, 30, keywords=["robotics"], now=now)
    assert [job.job_id for job in matched] == ["1"]


def test_source_jobs_returns_empty_for_a_source_with_no_active_jobs(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path):
        pass  # just initialize the schema, no jobs stored at all

    profile = CandidateProfile(target_domains=["engineer"])
    assert source_jobs(db_path, "waymo", profile, 30) == []
