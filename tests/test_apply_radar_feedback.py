import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from job_hunter.models import JobFeedback
from job_hunter.storage import Storage

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from apply_radar_feedback import export_event_time, ingest, resolve_feedback_file  # noqa: E402


def _entry(**updates):
    values = dict(
        source_key="apple", job_id="99", company="Apple", title="Some Role",
        department=None, score=68, label="irrelevant",
    )
    values.update(updates)
    return values


def test_omitting_a_previously_tagged_job_leaves_its_label_unchanged(tmp_path):
    """The exact guarantee this script exists to provide: a session that doesn't re-tag a
    job with an existing job_feedback row must not silently erase or reset it."""
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        ingest(storage, [_entry(job_id="1", label="okay")])
        ingest(storage, [_entry(job_id="2", label="irrelevant")])  # job "1" omitted here

        rows = {row["job_id"]: row["label"] for row in storage.export_job_feedback()}
        assert rows["1"] == "okay"
        assert rows["2"] == "irrelevant"


def test_relabeling_the_same_job_corrects_in_place(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        counts = ingest(storage, [_entry(job_id="5", label="okay")])
        assert counts == {"new": 1, "changed": 0, "unchanged": 0, "invalid": 0}

        counts = ingest(storage, [_entry(job_id="5", label="irrelevant")])
        assert counts == {"new": 0, "changed": 1, "unchanged": 0, "invalid": 0}

        rows = storage.export_job_feedback()
        assert len(rows) == 1
        assert rows[0]["label"] == "irrelevant"


def test_invalid_label_is_skipped_not_stored(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        counts = ingest(storage, [_entry(job_id="7", label="maybe")])
        assert counts["invalid"] == 1
        assert storage.export_job_feedback() == []


# --- resolve_feedback_file (auto-resolving the newest download) ---

def test_resolve_feedback_file_explicit_file_wins_even_if_missing(tmp_path):
    """An explicit --file is never second-guessed against the downloads directory — it's
    returned verbatim, existence-checking is main()'s job, not the resolver's."""
    explicit = tmp_path / "does-not-exist.json"
    assert resolve_feedback_file(explicit, tmp_path) == explicit


def test_resolve_feedback_file_picks_newest_by_mtime(tmp_path):
    older = tmp_path / "radar-feedback-default_2026-09-01.json"
    newer = tmp_path / "radar-feedback-adas_2026-09-05.json"
    older.write_text("[]")
    time.sleep(0.01)
    newer.write_text("[]")
    assert resolve_feedback_file(None, tmp_path) == newer


def test_resolve_feedback_file_returns_none_when_nothing_matches(tmp_path):
    (tmp_path / "unrelated.json").write_text("[]")
    assert resolve_feedback_file(None, tmp_path) is None


def test_resolve_feedback_file_returns_none_when_downloads_dir_missing(tmp_path):
    assert resolve_feedback_file(None, tmp_path / "does-not-exist") is None


# --- Guarded writes with event timestamps ---


def _t(minutes: int) -> datetime:
    return datetime(2026, 9, 26, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes)


def _live(label="irrelevant", **updates):
    values = dict(
        source_key="apple", job_id="99", company="Apple", title="Some Role",
        department=None, score=68, label=label,
    )
    values.update(updates)
    return JobFeedback(**values)


def test_stale_export_cannot_overwrite_a_newer_live_relabel(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.apply_feedback(_live("irrelevant"), event_at=_t(30))
        counts = ingest(storage, [_entry(label="relevant")], event_at=_t(0))
        assert counts == {"new": 0, "changed": 0, "unchanged": 0, "invalid": 0, "stale": 1}
        assert storage.get_job_feedback("apple", "99")["label"] == "irrelevant"


def test_stale_export_cannot_resurrect_an_untagged_job(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.delete_feedback("apple", "99", event_at=_t(30))
        counts = ingest(storage, [_entry(label="okay")], event_at=_t(0))
        assert counts["stale"] == 1 and counts["new"] == 0
        assert storage.get_job_feedback("apple", "99") is None


def test_newer_export_wins_and_clears_a_tombstone(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.delete_feedback("apple", "99", event_at=_t(0))
        counts = ingest(storage, [_entry(label="okay")], event_at=_t(30))
        assert counts == {"new": 1, "changed": 0, "unchanged": 0, "invalid": 0}
        assert storage.get_job_feedback("apple", "99")["label"] == "okay"


def test_older_reimport_of_the_same_label_counts_as_unchanged_not_stale(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        storage.apply_feedback(_live("okay"), event_at=_t(30))
        counts = ingest(storage, [_entry(label="okay")], event_at=_t(0))
        assert counts == {"new": 0, "changed": 0, "unchanged": 1, "invalid": 0}


def test_imported_rows_are_stamped_with_the_export_event_time(tmp_path):
    with Storage(tmp_path / "jobs.sqlite3") as storage:
        ingest(storage, [_entry(job_id="3", label="okay")], event_at=_t(7))
        assert storage.get_job_feedback("apple", "3")["recorded_at"] == _t(7).isoformat()


def test_export_event_time_is_the_files_mtime_in_utc(tmp_path):
    path = tmp_path / "radar-feedback-x.json"
    path.write_text("[]")
    event_time = export_event_time(path)
    assert event_time.tzinfo is not None
    assert abs(event_time.timestamp() - path.stat().st_mtime) < 1e-3
