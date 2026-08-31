from datetime import UTC, datetime, timedelta

from job_hunter.models import Assessment, Job, LocationConfidence
from job_hunter.normalizer import description_hash
from job_hunter.storage import Storage


def make_assessment(**updates):
    values = dict(
        source_key="acme",
        job_id="42",
        company="Acme",
        title="Engineer",
        url="https://example.com/42",
        content_hash="hash-1",
        score=85,
        recommended=True,
        matches=["Python", "ADAS"],
        gaps=["No ROS"],
    )
    values.update(updates)
    return Assessment(**values)


def make_job(description="first", **updates):
    values = dict(
        source_key="acme",
        source_platform="test",
        company="Acme",
        job_id="42",
        title="Engineer",
        url="https://example.com/42",
        us_eligible=True,
        location_confidence=LocationConfidence.HIGH,
        description=description,
        content_hash=description_hash(description),
    )
    values.update(updates)
    return Job(**values)


def test_upsert_detects_new_and_changed(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.upsert_job(make_job()).is_new
        unchanged = storage.upsert_job(make_job())
        assert not unchanged.is_new and not unchanged.is_changed
        assert storage.upsert_job(make_job("second")).is_changed


def test_closes_only_after_three_successful_missing_observations(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job())
        storage.mark_missing("acme", [])
        storage.mark_missing("acme", [])
        assert storage.stats()["active"] == 1
        storage.mark_missing("acme", [])
        assert storage.stats()["closed"] == 1


def test_stale_before_excludes_already_old_jobs_from_missing_accounting(tmp_path):
    """A source that stops paginating once postings are provably older than its own
    recency cutoff (see apple.py/adp_recruiting.py) will never see an already-stale job
    again — without this exclusion it would look permanently "missing" and get falsely
    closed after 3 runs, even though it's still live on the real site."""
    now = datetime.now(UTC)
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job(posted_at=now - timedelta(days=45)))
        stale_before = now - timedelta(days=30)
        for _ in range(5):
            storage.mark_missing("acme", [], stale_before=stale_before)
        assert storage.stats()["active"] == 1
        assert storage.stats().get("closed", 0) == 0


def test_upsert_assessment_roundtrips_and_updates(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_assessment(make_assessment())
        stored = storage.all_assessments()[("acme", "42")]
        assert stored.score == 85
        assert stored.matches == ["Python", "ADAS"]

        storage.upsert_assessment(make_assessment(score=91, recommended=False, gaps=[]))
        updated = storage.all_assessments()[("acme", "42")]
        assert updated.score == 91
        assert updated.recommended is False
        assert updated.gaps == []


def test_export_assessments_matches_all_assessments(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_assessment(make_assessment())
        rows = storage.export_assessments()
    assert len(rows) == 1
    assert rows[0]["job_id"] == "42"
    assert rows[0]["matches"] == ["Python", "ADAS"]
    assert rows[0]["recommended"] is True


def test_reevaluate_sponsorship_backfills_from_stored_description(tmp_path):
    """Regression for a real gap: a row's visa_sponsorship is only ever set by
    upsert_job (i.e. on a fresh successful fetch). A source that's failing (rate
    limited, outages) never gets that chance even though its description — containing
    a real sponsorship clause — is already sitting in the database. reevaluate_sponsorship
    must backfill that from the stored description alone, no re-fetch required."""
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job(description="A role with no sponsorship mention at all."))
        # Simulate a description that already contains a real clause but whose
        # visa_sponsorship column predates that clause being classifiable (e.g. it was
        # collected before sponsorship.py existed, or before a phrase-list fix).
        storage.connection.execute(
            "UPDATE jobs SET description=? WHERE source_key='acme' AND job_id='42'",
            ("GM does not provide immigration-related sponsorship for this role.",),
        )
        storage.connection.commit()
        assert storage.get_job("acme", "42")["visa_sponsorship"] == "unmentioned"

        changed = storage.reevaluate_sponsorship()

        assert changed == 1
        row = storage.get_job("acme", "42")
        assert row["visa_sponsorship"] == "not_available"
        assert row["sponsorship_evidence"]

        # Idempotent: running it again with nothing changed reports zero.
        assert storage.reevaluate_sponsorship() == 0
