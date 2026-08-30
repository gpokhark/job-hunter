from datetime import UTC, datetime, timedelta

from job_hunter.models import Job, LocationConfidence
from job_hunter.normalizer import description_hash
from job_hunter.storage import Storage


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
