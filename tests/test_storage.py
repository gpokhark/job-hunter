import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from job_hunter import storage as storage_module
from job_hunter.models import (
    Application,
    ApplicationStatus,
    Assessment,
    Job,
    JobFeedback,
    LocationConfidence,
)
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


def test_partition_candidates_for_review_splits_cached_from_needed(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_assessment(make_assessment(source_key="acme", job_id="42", content_hash="hash-1"))
        candidates = [
            {"source_key": "acme", "job_id": "42", "content_hash": "hash-1"},  # cached, unchanged
            {"source_key": "acme", "job_id": "42", "content_hash": "hash-2"},  # cached but content changed
            {"source_key": "acme", "job_id": "99", "content_hash": "hash-3"},  # never assessed
        ]
        to_review, skipped_cached = storage.partition_candidates_for_review(candidates)

    assert skipped_cached == 1
    assert [c["job_id"] for c in to_review] == ["42", "99"]
    assert to_review[0]["content_hash"] == "hash-2"


def test_partition_candidates_for_review_force_ignores_cache(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_assessment(make_assessment(source_key="acme", job_id="42", content_hash="hash-1"))
        candidates = [{"source_key": "acme", "job_id": "42", "content_hash": "hash-1"}]
        to_review, skipped_cached = storage.partition_candidates_for_review(candidates, force=True)

    assert skipped_cached == 0
    assert len(to_review) == 1


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


def test_reevaluate_location_fixes_a_self_contained_ambiguous_code(tmp_path):
    """Real live bug: Intuitive's listing renders an Indian posting's location as
    'Chennai, TN, India' — an older location.py let the ambiguous 'TN' (Tennessee/Tunisia)
    match as a U.S. state even though 'India' is spelled out in the very same string,
    misclassifying it us_eligible=True. Once location.py's disambiguation is fixed,
    reevaluate_location must backfill this from the already-stored location_raw alone, no
    re-fetch required."""
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(
            make_job(
                location_raw="Chennai, TN, India",
                state="TN",
                country="US",
                us_eligible=True,
                location_confidence=LocationConfidence.HIGH,
                location_evidence="recognized U.S. state: TN",
            )
        )

        changed = storage.reevaluate_location()

        assert changed == 1
        row = storage.get_job("acme", "42")
        assert not row["us_eligible"]
        assert row["location_evidence"] == "recognized non-U.S. location"

        # Idempotent: running it again with nothing changed reports zero.
        assert storage.reevaluate_location() == 0


def test_reevaluate_location_skips_a_code_not_present_in_its_own_location_raw(tmp_path):
    """Safety guard, confirmed against a real production false-positive risk: some Workday
    tenants (Johnson & Johnson, NVIDIA) store a generic 'N Locations' placeholder in
    location_raw while a genuine U.S. state ('CA', an ambiguous code) was resolved from
    richer detail-fetch text that was never persisted verbatim. Re-deriving purely from the
    stored (placeholder) location_raw would wrongly flip these to non-U.S. — this job must
    be left untouched since there's no local evidence to safely re-derive it from."""
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(
            make_job(
                location_raw="2 Locations",
                state="CA",
                country="US",
                us_eligible=True,
                location_confidence=LocationConfidence.HIGH,
                location_evidence="recognized U.S. state: CA",
            )
        )

        assert storage.reevaluate_location() == 0
        row = storage.get_job("acme", "42")
        assert row["us_eligible"]
        assert row["state"] == "CA"


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


# --- live-radar feedback: tombstones + event-time (last-writer-wins) writes ---


def _t(minutes: int) -> datetime:
    return datetime(2026, 9, 26, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes)


def test_feedback_label_is_restricted_to_the_three_known_values():
    with pytest.raises(ValidationError):
        make_feedback(label="maybe")


def test_pre_v3_database_gains_the_tombstone_table_on_open(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.connection.execute("DROP TABLE feedback_tombstones")
        storage.connection.execute("PRAGMA user_version = 2")
        storage.connection.commit()
    with Storage(db_path) as storage:
        count = storage.connection.execute("SELECT COUNT(*) FROM feedback_tombstones").fetchone()[0]
        version = storage.connection.execute("PRAGMA user_version").fetchone()[0]
    assert count == 0
    assert version == len(storage_module._MIGRATIONS)


def test_apply_feedback_requires_a_strictly_newer_event(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.apply_feedback(make_feedback(label="okay"), event_at=_t(0)) == "applied"
        # Equal timestamp: rejected (strictly newer required).
        assert storage.apply_feedback(make_feedback(label="relevant"), event_at=_t(0)) == "stale"
        # Older: rejected.
        assert storage.apply_feedback(make_feedback(label="relevant"), event_at=_t(-5)) == "stale"
        row = storage.get_job_feedback("apple", "99")
        assert row["label"] == "okay"
        assert row["recorded_at"] == _t(0).isoformat()
        # Newer: applied, recorded_at is the event time (not "now").
        assert storage.apply_feedback(make_feedback(label="irrelevant"), event_at=_t(5)) == "applied"
        row = storage.get_job_feedback("apple", "99")
        assert row["label"] == "irrelevant"
        assert row["recorded_at"] == _t(5).isoformat()


def test_apply_feedback_rejects_a_naive_event_time(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage, pytest.raises(ValueError):
        storage.apply_feedback(make_feedback(), event_at=datetime(2026, 9, 26, 12, 0))


def test_apply_feedback_force_bypasses_the_staleness_check(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.apply_feedback(make_feedback(label="okay"), event_at=_t(10))
        assert (
            storage.apply_feedback(make_feedback(label="relevant"), event_at=_t(0), force=True)
            == "applied"
        )
        assert storage.get_job_feedback("apple", "99")["label"] == "relevant"


def test_delete_feedback_leaves_a_tombstone_that_blocks_older_labels(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.apply_feedback(make_feedback(label="okay"), event_at=_t(0))
        assert storage.delete_feedback("apple", "99", event_at=_t(10)) == "applied"
        assert storage.get_job_feedback("apple", "99") is None
        # An older label (e.g. a stale export) must not resurrect the job.
        assert storage.apply_feedback(make_feedback(label="relevant"), event_at=_t(5)) == "stale"
        assert storage.get_job_feedback("apple", "99") is None
        # A newer label clears the tombstone and lands.
        assert storage.apply_feedback(make_feedback(label="relevant"), event_at=_t(20)) == "applied"
        assert storage.get_job_feedback("apple", "99")["label"] == "relevant"
        tombstones = storage.connection.execute("SELECT * FROM feedback_tombstones").fetchall()
        assert tombstones == []


def test_delete_feedback_is_itself_subject_to_the_staleness_check(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.apply_feedback(make_feedback(label="okay"), event_at=_t(10))
        assert storage.delete_feedback("apple", "99", event_at=_t(0)) == "stale"
        assert storage.get_job_feedback("apple", "99")["label"] == "okay"


def test_delete_feedback_with_no_prior_row_still_records_a_tombstone(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.delete_feedback("apple", "99", event_at=_t(10)) == "applied"
        assert storage.apply_feedback(make_feedback(label="okay"), event_at=_t(0)) == "stale"


def test_feedback_map_is_keyed_by_source_and_job(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.apply_feedback(make_feedback(source_key="a", job_id="1"), event_at=_t(0))
        storage.apply_feedback(make_feedback(source_key="b", job_id="2", label="okay"), event_at=_t(0))
        mapping = storage.feedback_map()
    assert set(mapping) == {"a|1", "b|2"}
    assert mapping["b|2"]["label"] == "okay"


def test_get_job_snapshot_and_assessment_score_never_expose_the_description(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.get_job_snapshot("acme", "42") is None
        assert storage.get_assessment_score("acme", "42") is None
        storage.upsert_job(make_job(description="secret body", posted_at=None))
        storage.upsert_assessment(make_assessment(score=77))
        snapshot = storage.get_job_snapshot("acme", "42")
        assert snapshot["company"] == "Acme"
        assert snapshot["url"] == "https://example.com/42"
        assert snapshot["status"] == "active"
        assert "description" not in snapshot
        assert storage.get_assessment_score("acme", "42") == 77


def test_delete_closed_jobs_leaves_feedback_tombstones_alone(tmp_path):
    now = datetime.now(UTC)
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job(job_id="stale", last_seen_at=now - timedelta(days=30)))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='stale'")
        storage.connection.commit()
        storage.delete_feedback("acme", "stale", event_at=_t(0))
        storage.delete_closed_jobs(now - timedelta(days=7))
        remaining = storage.connection.execute(
            "SELECT job_id FROM feedback_tombstones"
        ).fetchall()
    assert [row["job_id"] for row in remaining] == ["stale"]


# --- live-radar application tracking (Phase B) ---

TODAY = date(2026, 9, 26)


def _snap(**over):
    values = {
        "company": "Acme", "title": "Engineer", "url": "https://example.com/42",
        "location_raw": "Detroit, MI", "posted_at": "2026-09-20T00:00:00Z", "score": 82,
        "salary_evidence": "$100,000 - $120,000",
    }
    values.update(over)
    return values


def _apply(storage, minutes, changes, snapshot="default", key=("acme", "42")):
    return storage.apply_application(
        *key, event_at=_t(minutes), changes=changes,
        snapshot=_snap() if snapshot == "default" else snapshot, today=TODAY,
    )


def test_application_model_enforces_invariants():
    base = dict(
        source_key="a", job_id="1", status="saved", company="Acme", title="T", url="https://x",
        created_at=_t(0), updated_at=_t(0),
    )
    assert Application(**base).applied_at is None
    with pytest.raises(ValidationError):
        Application(**{**base, "applied_at": date(2026, 9, 1)})  # saved cannot have a date
    with pytest.raises(ValidationError):
        Application(**{**base, "status": "hired"})
    with pytest.raises(ValidationError):
        Application(**{**base, "notes": "x" * 4001})
    with pytest.raises(ValidationError):
        Application(**{**base, "title": "  "})


def test_pre_v4_database_gains_the_application_tables_on_open(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.connection.execute("DROP TABLE applications")
        storage.connection.execute("DROP TABLE application_tombstones")
        storage.connection.execute("PRAGMA user_version = 3")
        storage.connection.commit()
    with Storage(db_path) as storage:
        for table in ("applications", "application_tombstones"):
            assert storage.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert storage.connection.execute("PRAGMA user_version").fetchone()[0] == len(
            storage_module._MIGRATIONS
        )


def test_first_write_creates_a_row_with_a_server_snapshot(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        outcome, row = _apply(storage, 0, {"status": ApplicationStatus.SAVED})
        assert outcome == "applied"
        assert (row["status"], row["applied_at"], row["notes"]) == ("saved", None, None)
        assert (row["company"], row["title"], row["url"], row["score"]) == (
            "Acme", "Engineer", "https://example.com/42", 82,
        )
        assert row["location"] == "Detroit, MI" and row["salary_evidence"] == "$100,000 - $120,000"
        assert row["created_at"] == row["updated_at"] == _t(0).isoformat()
        assert storage.get_application("acme", "42") == row


def test_creating_requires_a_status_and_a_known_job(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        with pytest.raises(ValueError, match="status"):
            _apply(storage, 0, {"notes": "hello"})
        with pytest.raises(ValueError, match="unknown job"):
            _apply(storage, 0, {"status": ApplicationStatus.SAVED}, snapshot=None)


def test_applied_date_rules(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        _apply(storage, 0, {"status": ApplicationStatus.SAVED})
        # first move to a non-saved status defaults to the caller's local today
        _, row = _apply(storage, 1, {"status": ApplicationStatus.APPLIED})
        assert row["applied_at"] == "2026-09-26"
        # a later status keeps the existing date
        _, row = _apply(storage, 2, {"status": ApplicationStatus.INTERVIEWING})
        assert row["applied_at"] == "2026-09-26"
        # an explicit date wins
        _, row = _apply(storage, 3, {"applied_at": date(2026, 9, 1)})
        assert (row["status"], row["applied_at"]) == ("interviewing", "2026-09-01")
        # back to saved clears the date
        _, row = _apply(storage, 4, {"status": ApplicationStatus.SAVED})
        assert row["applied_at"] is None
        # saved cannot take an explicit date, and a date can never be cleared explicitly
        with pytest.raises(ValueError, match="saved"):
            _apply(storage, 5, {"applied_at": date(2026, 9, 2)})
        _apply(storage, 6, {"status": ApplicationStatus.APPLIED})
        with pytest.raises(ValueError, match="cleared"):
            _apply(storage, 7, {"applied_at": None})


def test_partial_updates_change_only_the_named_fields_and_never_the_snapshot(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        _apply(storage, 0, {"status": ApplicationStatus.APPLIED, "notes": "first"})
        _, row = _apply(storage, 1, {"notes": "second"}, snapshot=_snap(company="Changed", score=1))
        assert (row["status"], row["notes"]) == ("applied", "second")
        assert (row["company"], row["score"]) == ("Acme", 82)  # snapshot is immutable
        assert row["created_at"] == _t(0).isoformat() and row["updated_at"] == _t(1).isoformat()
        _, row = _apply(storage, 2, {"notes": None})
        assert row["notes"] is None and row["status"] == "applied"


def test_notes_are_capped(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        with pytest.raises(ValueError):
            _apply(storage, 0, {"status": ApplicationStatus.SAVED, "notes": "x" * 4001})
        assert storage.get_application("acme", "42") is None  # nothing half-written


def test_stale_writes_are_rejected_and_return_the_current_row(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        _apply(storage, 10, {"status": ApplicationStatus.APPLIED})
        outcome, row = _apply(storage, 10, {"status": ApplicationStatus.OFFER})  # equal → stale
        assert outcome == "stale" and row["status"] == "applied"
        outcome, row = _apply(storage, 5, {"notes": "late"})  # older → stale
        assert outcome == "stale" and row["notes"] is None
        outcome, row = _apply(storage, 11, {"status": ApplicationStatus.OFFER})
        assert outcome == "applied" and row["status"] == "offer"


def test_delete_leaves_a_tombstone_that_blocks_older_writes(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        _apply(storage, 0, {"status": ApplicationStatus.APPLIED, "notes": "keep?"})
        outcome, row = _apply(storage, 10, {"status": None})
        assert (outcome, row) == ("deleted", None)
        assert storage.get_application("acme", "42") is None
        assert storage.get_application_tombstone("acme", "42") == _t(10).isoformat()
        outcome, row = _apply(storage, 5, {"status": ApplicationStatus.APPLIED})
        assert (outcome, row) == ("stale", None)
        # a newer write recreates it (fresh snapshot, fresh created_at) and clears the tombstone
        outcome, row = _apply(storage, 20, {"status": ApplicationStatus.SAVED})
        assert outcome == "applied" and row["created_at"] == _t(20).isoformat()
        assert storage.get_application_tombstone("acme", "42") is None


def test_deleting_something_that_was_never_tracked_still_records_a_tombstone(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert _apply(storage, 10, {"status": None}, snapshot=None) == ("deleted", None)
        assert _apply(storage, 5, {"status": ApplicationStatus.SAVED})[0] == "stale"


def test_naive_event_time_is_rejected(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage, pytest.raises(ValueError):
        storage.apply_application(
            "acme", "42", event_at=datetime(2026, 9, 26, 12, 0),
            changes={"status": ApplicationStatus.SAVED}, snapshot=_snap(), today=TODAY,
        )


def test_application_map_export_order_and_job_statuses(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job(job_id="a"))
        storage.upsert_job(make_job(job_id="b"))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='b'")
        storage.connection.commit()
        _apply(storage, 0, {"status": ApplicationStatus.SAVED}, key=("acme", "a"))
        _apply(storage, 5, {"status": ApplicationStatus.APPLIED}, key=("acme", "b"))
        _apply(storage, 9, {"status": ApplicationStatus.APPLIED}, key=("acme", "gone"))
        assert set(storage.application_map()) == {"acme|a", "acme|b", "acme|gone"}
        assert [r["job_id"] for r in storage.export_applications()] == ["gone", "b", "a"]
        statuses = storage.job_statuses([("acme", "a"), ("acme", "b"), ("acme", "gone")])
        assert statuses == {"acme|a": "active", "acme|b": "closed"}  # "gone" is absent


def test_feedback_tombstone_reader(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        assert storage.get_feedback_tombstone("apple", "99") is None
        storage.delete_feedback("apple", "99", event_at=_t(3))
        assert storage.get_feedback_tombstone("apple", "99") == _t(3).isoformat()


def test_cleanup_never_touches_applications_or_their_tombstones(tmp_path):
    now = datetime.now(UTC)
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job(job_id="stale", last_seen_at=now - timedelta(days=30)))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='stale'")
        storage.connection.commit()
        _apply(storage, 0, {"status": ApplicationStatus.APPLIED}, key=("acme", "stale"))
        _apply(storage, 1, {"status": None}, key=("acme", "other"), snapshot=None)
        deleted = storage.delete_closed_jobs(now - timedelta(days=7))
        assert [j["job_id"] for j in deleted["jobs"]] == ["stale"]
        assert set(deleted) == {"jobs", "assessments", "job_feedback"}  # return shape unchanged
        assert storage.get_application("acme", "stale")["status"] == "applied"
        assert storage.get_application_tombstone("acme", "other") is not None
