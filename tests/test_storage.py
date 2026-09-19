import sqlite3
from datetime import UTC, datetime, timedelta

from job_hunter import storage as storage_module
from job_hunter.models import Assessment, Job, JobFeedback, LocationConfidence
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


def make_feedback(**updates):
    values = dict(
        source_key="apple", job_id="99", company="Apple", title="Some Role",
        department=None, score=68, label="irrelevant",
    )
    values.update(updates)
    return JobFeedback(**values)


def test_upsert_job_feedback_corrects_a_label_in_place(tmp_path):
    """The exact scenario that makes upsert (not append) load-bearing: a reviewer
    relabels the same job between sessions, and the correction must replace the old
    row, not create a second, contradictory one — see docs/feedback-exclusion-plan.md
    section 8."""
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job_feedback(make_feedback(label="okay"))
        rows = storage.export_job_feedback()
        assert len(rows) == 1
        assert rows[0]["label"] == "okay"

        storage.upsert_job_feedback(make_feedback(label="irrelevant"))
        rows = storage.export_job_feedback()
        assert len(rows) == 1
        assert rows[0]["label"] == "irrelevant"


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


def test_reevaluate_salary_backfills_from_stored_description(tmp_path):
    """Same backfill gap as sponsorship above, for salary_evidence: a row's
    salary_min/max/currency/evidence are only ever set by upsert_job, so a job whose
    description already contains a real salary range but predates salary.py (or a
    pattern fix) needs reevaluate_salary to pick it up from the stored description
    alone, no re-fetch required."""
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job(description="A role with no salary mention at all."))
        storage.connection.execute(
            "UPDATE jobs SET description=? WHERE source_key='acme' AND job_id='42'",
            ("The salary range for this role is $76,100.00 to $114,300.00.",),
        )
        storage.connection.commit()
        assert storage.get_job("acme", "42")["salary_evidence"] is None

        changed = storage.reevaluate_salary()

        assert changed == 1
        row = storage.get_job("acme", "42")
        assert row["salary_evidence"] == "$76,100.00 to $114,300.00"
        assert row["salary_min"] == 76100.0
        assert row["salary_max"] == 114300.0
        assert row["salary_currency"] == "USD"

        # Idempotent: running it again with nothing changed reports zero.
        assert storage.reevaluate_salary() == 0


def test_find_stale_closed_jobs_excludes_active_and_recently_closed(tmp_path):
    now = datetime.now(UTC)
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        # Active, never closed — never eligible regardless of last_seen_at.
        storage.upsert_job(make_job(job_id="active", last_seen_at=now - timedelta(days=100)))
        # Closed, but only just now — not old enough yet.
        storage.upsert_job(make_job(job_id="recent"))
        storage.connection.execute(
            "UPDATE jobs SET status='closed' WHERE job_id='recent'"
        )
        # Closed and genuinely stale — the one real target.
        storage.upsert_job(make_job(job_id="stale", last_seen_at=now - timedelta(days=30)))
        storage.connection.execute(
            "UPDATE jobs SET status='closed' WHERE job_id='stale'"
        )
        storage.connection.commit()

        eligible = storage.find_stale_closed_jobs(now - timedelta(days=10))
        assert [row["job_id"] for row in eligible] == ["stale"]


def test_delete_closed_jobs_cascades_to_assessments_and_feedback(tmp_path):
    """The explicit design choice from docs/retention-cleanup-plan.md section 3.6/4: a
    deleted job's own assessment and feedback history is deleted with it, not orphaned."""
    now = datetime.now(UTC)
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job(job_id="stale", last_seen_at=now - timedelta(days=30)))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='stale'")
        storage.connection.commit()
        storage.upsert_assessment(make_assessment(job_id="stale"))
        storage.upsert_job_feedback(make_feedback(source_key="acme", job_id="stale", title="Engineer"))

        # A second, unrelated closed-but-recent job must survive untouched.
        storage.upsert_job(make_job(job_id="recent"))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='recent'")
        storage.connection.commit()
        storage.upsert_assessment(make_assessment(job_id="recent"))

        deleted = storage.delete_closed_jobs(now - timedelta(days=10))

        assert [row["job_id"] for row in deleted["jobs"]] == ["stale"]
        assert [row["job_id"] for row in deleted["assessments"]] == ["stale"]
        assert [row["job_id"] for row in deleted["job_feedback"]] == ["stale"]

        assert storage.get_job("acme", "stale") is None
        assert ("acme", "stale") not in storage.all_assessments()
        assert storage.get_job("acme", "recent") is not None
        assert ("acme", "recent") in storage.all_assessments()


def test_delete_closed_jobs_is_a_no_op_when_nothing_qualifies(tmp_path):
    now = datetime.now(UTC)
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job())
        deleted = storage.delete_closed_jobs(now - timedelta(days=10))
        assert deleted == {"jobs": [], "assessments": [], "job_feedback": []}
        assert storage.stats()["active"] == 1


def test_vacuum_runs_without_error_after_delete(tmp_path):
    """Regression guard for the exact footgun this feature exists to avoid: VACUUM must be
    callable right after delete_closed_jobs's own commit, with no lingering transaction."""
    now = datetime.now(UTC)
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(job_id="stale", last_seen_at=now - timedelta(days=30)))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='stale'")
        storage.connection.commit()
        storage.delete_closed_jobs(now - timedelta(days=10))
        storage.vacuum()  # must not raise
        assert storage.stats()["closed"] == 0


# --- schema-version tracking (docs/agent-runtime-audit.md's "no explicit schema-version marker"
# finding) -- PRAGMA user_version, not a separate table.


def test_fresh_database_ends_up_at_the_current_schema_version(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        version = storage.connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(storage_module._MIGRATIONS)


def test_migrate_adds_missing_columns_to_a_pre_migration_database(tmp_path):
    """A database created before visa_sponsorship/sponsorship_evidence/salary_evidence existed
    (no PRAGMA user_version ever set -- the SQLite default is 0) must still end up with those
    columns and the current schema version after Storage opens it, exactly as the pre-refactor
    ad-hoc column-presence checks already guaranteed -- this confirms the refactor preserved that
    behavior, not just the new version-tracking mechanism."""
    db_path = tmp_path / "jobs.sqlite3"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE jobs (
            source_key TEXT NOT NULL, company TEXT NOT NULL, job_id TEXT NOT NULL,
            source_platform TEXT NOT NULL, title TEXT NOT NULL, canonical_url TEXT NOT NULL,
            us_eligible INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', missing_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(source_key, job_id)
        );
        """
    )
    raw.commit()
    raw.close()

    with Storage(db_path) as storage:
        columns = {row["name"] for row in storage.connection.execute("PRAGMA table_info(jobs)")}
        assert {"visa_sponsorship", "sponsorship_evidence", "salary_evidence"} <= columns
        version = storage.connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(storage_module._MIGRATIONS)


def test_migrate_skips_already_applied_migrations_on_a_second_open(tmp_path, monkeypatch):
    """The whole point of tracking PRAGMA user_version: a migration a database has already been
    upgraded past must not run again on a later open -- confirmed here by making the first
    migration blow up if it's ever invoked more than once, rather than only checking the end
    state looks right (which the old, purely column-presence-guarded checks would already satisfy
    even with no real version tracking behind them)."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path):
        pass  # first open: runs every migration, advances user_version

    calls: list[int] = []

    def _spy(connection):
        calls.append(1)

    monkeypatch.setattr(
        storage_module, "_MIGRATIONS", [_spy, storage_module._migrate_v2_add_salary_evidence_column]
    )

    with Storage(db_path):
        pass  # second open: must not re-run migration 1

    assert calls == [], "migration 1 ran again on an already-migrated database"
