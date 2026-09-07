import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from refilter_archive import _assessment_note, _render_job_rows, refilter  # noqa: E402

from job_hunter.config import CandidateProfile, Settings
from job_hunter.models import (
    Assessment,
    Job,
    LocationConfidence,
    SponsorshipStatus,
    WorkArrangement,
)
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


# --- rich HTML report (the "why is 32 gained but the profile-term diff only shows 5" fix) ---


def test_render_job_rows_includes_link_tags_date_and_feedback_buttons():
    now = datetime(2026, 9, 6, tzinfo=UTC)
    job = make_job(
        source_key="gm",
        job_id="1",
        title="Autonomous Vehicle Test Engineer",
        company="General Motors",
        url="https://example.com/av-test",
        posted_at=datetime(2026, 9, 2, tzinfo=UTC),  # 4 days old -> [New]
        work_arrangement=WorkArrangement.HYBRID,
        visa_sponsorship=SponsorshipStatus.NOT_AVAILABLE,
    )
    html_out = _render_job_rows([job], now=now, empty_message="unused")
    assert 'href="https://example.com/av-test"' in html_out
    assert 'target="_blank"' in html_out
    assert 'tag-new">New' in html_out
    assert 'tag-hybrid">Hybrid' in html_out
    assert 'tag-sponsor-no">No Sponsorship' in html_out
    assert 'data-source-key="gm"' in html_out
    assert 'data-job-id="1"' in html_out
    assert 'data-label="relevant"' in html_out
    assert "no assessment on record" in html_out


def test_render_job_rows_empty_shows_message():
    html_out = _render_job_rows([], now=datetime(2026, 9, 6, tzinfo=UTC), empty_message="Nothing gained.")
    assert "Nothing gained." in html_out


def test_assessment_note_reflects_prior_assessment_attached_by_active_jobs():
    """_active_jobs only ever attaches prior_assessment when the content_hash still matches,
    so its mere presence already means valid — no separate staleness check needed here."""
    job = make_job(job_id="1")
    assert _assessment_note(job) == "no assessment on record"
    job.prior_assessment = Assessment(
        source_key="apple", job_id="1", company="Apple", title="x", url="https://example.com/1",
        score=82, recommended=True,
    )
    assert _assessment_note(job) == "has a valid prior assessment (score 82)"


def test_refilter_excludes_a_source_that_failed_this_archives_run(monkeypatch, tmp_path):
    """Regression test for the exact bug reported live: a source that failed THIS archive's run
    (e.g. a stealth_html source missing its optional dependency) still has previously-active jobs
    sitting in SQLite from an earlier, unrelated successful run — those must NOT be pulled back in
    as spurious "Gained" jobs on refilter, since this archive never had them to lose in the first
    place and no profile change is responsible for their reappearance. Only sources source_health
    marked as having actually succeeded are in scope, not merely attempted."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(source_key="apple", job_id="1", title="Verification Engineer"))
        # A job from a source that failed THIS run but is still active from a prior success.
        storage.upsert_job(make_job(source_key="waymo", job_id="2", title="Verification Lead"))

    monkeypatch.setattr("refilter_archive.load_profile", lambda: CandidateProfile(target_domains=["verification"]))
    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings())

    data = _archive(
        [{"source_key": "apple", "job_id": "1"}],  # waymo's job was never in the original archive
        source_health=[{"source_key": "apple", "status": "ok"}, {"source_key": "waymo", "status": "failed"}],
    )
    result = refilter(data, now=datetime(2026, 9, 5, tzinfo=UTC), database_path=db_path)
    assert [c["source_key"] for c in result["candidates"]] == ["apple"]


def test_refilter_keeps_a_source_with_no_status_field_at_all(monkeypatch, tmp_path):
    """An opt-out (exclude known-bad), not opt-in (require known-good), design: a source_health
    row missing its `status` field entirely (a degraded/unexpected shape, distinct from
    source_health being absent) has no positive evidence of failure, so stays in scope — matching
    this project's general preference for false negatives over false positives in filtering."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(source_key="apple", job_id="1", title="Verification Engineer"))

    monkeypatch.setattr("refilter_archive.load_profile", lambda: CandidateProfile(target_domains=["verification"]))
    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings())

    data = _archive(
        [{"source_key": "apple", "job_id": "1"}],
        source_health=[{"source_key": "apple"}],  # no "status" key at all
    )
    result = refilter(data, now=datetime(2026, 9, 5, tzinfo=UTC), database_path=db_path)
    assert [c["source_key"] for c in result["candidates"]] == ["apple"]


def test_refilter_all_sources_failed_restricts_to_nothing(monkeypatch, tmp_path):
    """Distinct from source_health being entirely absent (falls back to unrestricted, see
    test_refilter_with_no_source_health_falls_back_to_no_restriction): a source_health that
    exists but recorded every source as failed is a *known* scope of zero, not an unknown one —
    it must not fall back to unrestricted."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(source_key="apple", job_id="1", title="Verification Engineer"))

    monkeypatch.setattr("refilter_archive.load_profile", lambda: CandidateProfile(target_domains=["verification"]))
    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings())

    data = _archive(
        [{"source_key": "apple", "job_id": "1"}],
        source_health=[{"source_key": "apple", "status": "failed"}],
    )
    result = refilter(data, now=datetime(2026, 9, 5, tzinfo=UTC), database_path=db_path)
    assert result["candidates"] == []


def test_main_writes_archive_diff_report_excluding_a_failed_sources_stale_job(monkeypatch, tmp_path, capsys):
    """End-to-end: the HTML report's Gained/Lost sections must reflect the same
    source-scope-excludes-failures fix as refilter() itself, not just the JSON output."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(source_key="apple", job_id="1", title="Verification Engineer"))
        # A job from a source that failed THIS run but is still active from a prior success.
        storage.upsert_job(make_job(source_key="waymo", job_id="2", title="Verification Lead"))

    monkeypatch.setattr("refilter_archive.load_profile", lambda: CandidateProfile(target_domains=["verification"]))
    monkeypatch.setattr("refilter_archive.load_settings", lambda: Settings(database_path=db_path))
    monkeypatch.chdir(tmp_path)

    search_path = tmp_path / "data" / "searches" / "default_2026-09-06.json"
    search_path.parent.mkdir(parents=True)
    archive = _archive(
        [{"source_key": "apple", "job_id": "1"}],  # waymo's job was never in the original archive
        source_health=[{"source_key": "apple", "status": "ok"}, {"source_key": "waymo", "status": "failed"}],
    )
    search_path.write_text(json.dumps(archive))

    import refilter_archive

    monkeypatch.setattr(sys, "argv", ["refilter_archive.py", "--search", str(search_path)])
    refilter_archive.main()

    out = capsys.readouterr().out
    assert "0 gained" in out
    report_line = next(line for line in out.splitlines() if line.startswith("Wrote "))
    report_path = Path(report_line.removeprefix("Wrote "))
    assert report_path.exists()
    report_html = report_path.read_text()
    gained_section = report_html.split("<h2>Gained</h2>")[1].split("<h2>Lost</h2>")[0]
    assert "Verification Lead" not in gained_section  # the failed-source job stays excluded
    assert "Nothing gained." in gained_section
