import sys
import time
from pathlib import Path

from job_hunter.storage import Storage

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from apply_radar_feedback import ingest, resolve_feedback_file  # noqa: E402


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
