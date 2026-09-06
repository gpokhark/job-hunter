import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from refilter_archive import refilter  # noqa: E402

from job_hunter.config import CandidateProfile, Settings
from job_hunter.models import Job, LocationConfidence
from job_hunter.storage import Storage


def make_job(**updates):
    values = dict(
        source_key="apple", source_platform="test", company="Apple", job_id="1",
        title="Design Verification Engineer", url="https://example.com/1",
        us_eligible=True, location_confidence=LocationConfidence.HIGH,
    )
    values.update(updates)
    return Job(**values)


def _archive(candidates, source_health=None):
    return {
        "summary": {"prefilter_candidates": len(candidates)},
        "source_health": source_health or [],
        "candidates": candidates,
    }


def test_refilter_drops_a_job_now_soft_excluded(monkeypatch, tmp_path):
    """The exact scenario this tool exists for: a job that passed prefilter at collection time
    must be dropped after a new soft_exclude_terms entry is added, with no re-fetch."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(job_id="1", title="CAD Automation and Mixed-Signal Simulation Engineer"))
        storage.upsert_job(make_job(job_id="2", title="Design Verification Engineer"))

    monkeypatch.setattr(
        "refilter_archive.load_profile",
        lambda: CandidateProfile(target_domains=["verification"], soft_exclude_terms=["cad automation"]),
    )
    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings())

    data = _archive([{"source_key": "apple", "job_id": "1"}, {"source_key": "apple", "job_id": "2"}])
    result = refilter(data, now=datetime(2026, 9, 5, tzinfo=UTC), database_path=db_path)
    assert [c["job_id"] for c in result["candidates"]] == ["2"]
    assert result["summary"]["prefilter_candidates"] == 1


def test_refilter_recomputes_recency_against_now(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(title="Engineer", posted_at=datetime(2026, 1, 1, tzinfo=UTC)))

    monkeypatch.setattr("refilter_archive.load_profile", lambda: CandidateProfile(target_domains=["engineer"]))
    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings())

    data = _archive([{"source_key": "apple", "job_id": "1"}])
    result = refilter(data, now=datetime(2026, 9, 5, tzinfo=UTC), database_path=db_path)
    assert result["candidates"] == []
    assert result["summary"]["stale_excluded"] == 1


def test_refilter_does_not_mutate_input(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(title="Engineer"))

    monkeypatch.setattr("refilter_archive.load_profile", lambda: CandidateProfile(target_domains=["engineer"]))
    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings())

    data = _archive([{"source_key": "apple", "job_id": "1"}])
    refilter(data, now=datetime(2026, 9, 5, tzinfo=UTC), database_path=db_path)
    assert len(data["candidates"]) == 1  # original untouched


def test_refilter_rescues_a_job_a_prior_refilter_already_dropped(monkeypatch, tmp_path):
    """Regression test for the exact bug this rewrite fixes: narrowing the archive's own
    `candidates` list in place made a later *loosening* edit (a new strong_relevance_terms
    override) unable to restore a job an earlier *tightening* edit (soft_exclude_terms) had
    already removed from the file. Rebuilding from SQLite each time means the loosening edit
    works regardless of what an earlier refilter run already stripped from `candidates`."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(
            make_job(job_id="camera", title="Custom Silicon Validation Engineer - Camera Hardware")
        )

    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings())

    # Round 1: "silicon" gets soft-excluded — the job is dropped, and (as the old design did)
    # the archive's `candidates` list no longer contains it at all.
    monkeypatch.setattr(
        "refilter_archive.load_profile",
        lambda: CandidateProfile(target_domains=["validation"], soft_exclude_terms=["silicon"]),
    )
    data = _archive([{"source_key": "apple", "job_id": "camera"}])
    round1 = refilter(data, now=datetime(2026, 9, 5, tzinfo=UTC), database_path=db_path)
    assert round1["candidates"] == []

    # Round 2: a strong_relevance_terms override is added to rescue it. Feeding round1's
    # already-empty `candidates` back in must still recover the job, since the source of truth
    # is SQLite, not whatever survived the previous run.
    monkeypatch.setattr(
        "refilter_archive.load_profile",
        lambda: CandidateProfile(
            target_domains=["validation"],
            soft_exclude_terms=["silicon"],
            strong_relevance_terms=["camera"],
        ),
    )
    round2 = refilter(round1, now=datetime(2026, 9, 5, tzinfo=UTC), database_path=db_path)
    assert [c["job_id"] for c in round2["candidates"]] == ["camera"]


def test_refilter_restricts_to_archives_own_source_scope(monkeypatch, tmp_path):
    """A dated archive should never silently gain a company's jobs just because that company
    exists in SQLite now (e.g. onboarded after this archive was originally collected) — only
    sources this archive's own source_health actually attempted are in scope."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(source_key="apple", job_id="1", company="Apple", title="Design Verification Engineer"))
        storage.upsert_job(make_job(source_key="newco", job_id="2", company="NewCo", title="Design Verification Engineer"))

    monkeypatch.setattr("refilter_archive.load_profile", lambda: CandidateProfile(target_domains=["verification"]))
    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings())

    data = _archive(
        [{"source_key": "apple", "job_id": "1"}],
        source_health=[{"source_key": "apple"}],
    )
    result = refilter(data, now=datetime(2026, 9, 5, tzinfo=UTC), database_path=db_path)
    assert [c["source_key"] for c in result["candidates"]] == ["apple"]


def test_refilter_with_no_source_health_falls_back_to_no_restriction(monkeypatch, tmp_path):
    """An archive shape missing the optional source_health field (unexpected/older file)
    shouldn't raise — just don't restrict by source."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(source_key="apple", job_id="1", title="Design Verification Engineer"))

    monkeypatch.setattr("refilter_archive.load_profile", lambda: CandidateProfile(target_domains=["verification"]))
    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings())

    data = _archive([{"source_key": "apple", "job_id": "1"}])  # no source_health key
    result = refilter(data, now=datetime(2026, 9, 5, tzinfo=UTC), database_path=db_path)
    assert [c["job_id"] for c in result["candidates"]] == ["1"]
