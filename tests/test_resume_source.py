import os
import time
from pathlib import Path

import pytest

from job_hunter.config import CandidateProfile
from job_hunter.resume_source import (
    find_cover_sample,
    find_personalization,
    find_review_evidence,
    is_example,
    resolve_master_resume,
)


def _resume_dir(root: Path) -> Path:
    directory = root / "data" / "resume"
    directory.mkdir(parents=True)
    return directory


def test_newest_dated_file_wins_by_filename_not_mtime(tmp_path):
    directory = _resume_dir(tmp_path)
    old = directory / "main_resume_2025-01-05.md"
    new = directory / "main_resume_2026-03-09.md"
    new.write_text("new")
    time.sleep(0.01)
    old.write_text("old")  # older date but *newer* mtime: must still lose
    os.utime(new, (1, 1))
    resolved = resolve_master_resume(tmp_path)
    assert resolved.path == new.resolve()
    assert resolved.source == "dated"


def test_names_that_do_not_match_or_are_not_real_dates_are_ignored(tmp_path):
    directory = _resume_dir(tmp_path)
    (directory / "main_resume_2026-13-45.md").write_text("bad month")
    (directory / "main_resume_latest.md").write_text("no date")
    (directory / "resume_2026-01-01.md").write_text("wrong prefix")
    (directory / "main_resume_2026-01-02.md.bak").write_text("wrong suffix")
    (directory / "main_resume_2026-02-02.md").mkdir()  # a directory, not a file
    good = directory / "main_resume_2024-06-01.md"
    good.write_text("ok")
    assert resolve_master_resume(tmp_path).path == good.resolve()


def test_explicit_path_beats_everything_and_must_exist(tmp_path):
    directory = _resume_dir(tmp_path)
    (directory / "main_resume_2026-03-09.md").write_text("dated")
    explicit = tmp_path / "elsewhere.md"
    explicit.write_text("explicit")
    resolved = resolve_master_resume(tmp_path, explicit=explicit)
    assert (resolved.path, resolved.source) == (explicit.resolve(), "explicit")
    with pytest.raises(FileNotFoundError, match="elsewhere-missing.md"):
        resolve_master_resume(tmp_path, explicit=tmp_path / "elsewhere-missing.md")


def test_profile_resume_path_is_only_a_fallback(tmp_path):
    fallback = tmp_path / "config" / "my_resume.md"
    fallback.parent.mkdir()
    fallback.write_text("profile")
    profile = CandidateProfile(resume_path=Path("config/my_resume.md"))  # relative to root
    resolved = resolve_master_resume(tmp_path, profile=profile)
    assert (resolved.path, resolved.source) == (fallback.resolve(), "profile")
    # a dated file appears -> it wins without touching the profile
    directory = _resume_dir(tmp_path)
    dated = directory / "main_resume_2026-01-01.md"
    dated.write_text("dated")
    assert resolve_master_resume(tmp_path, profile=profile).path == dated.resolve()


def test_nothing_found_explains_how_to_fix_it(tmp_path):
    with pytest.raises(FileNotFoundError, match=r"data/resume/main_resume_<YYYY-MM-DD>\.md"):
        resolve_master_resume(tmp_path)
    profile = CandidateProfile(resume_path=Path("config/missing.md"))
    with pytest.raises(FileNotFoundError):
        resolve_master_resume(tmp_path, profile=profile)


def test_is_example_flags_placeholder_filenames():
    assert is_example(Path("config/resume.example.md"))
    assert not is_example(Path("data/resume/main_resume_2026-01-01.md"))


def test_optional_files_are_found_or_none(tmp_path):
    assert find_personalization(tmp_path) is None
    assert find_cover_sample(tmp_path) is None
    assert find_review_evidence(tmp_path) is None
    directory = _resume_dir(tmp_path)
    (directory / "personalization.md").write_text("tone: plain")
    (directory / "cover_letter_2025-01-01.md").write_text("old")
    (directory / "cover_letter_2026-02-02.md").write_text("new")
    (directory / "Review_Evidence_2026-04-04.md").write_text("ieee")
    assert find_personalization(tmp_path) == (directory / "personalization.md").resolve()
    assert find_cover_sample(tmp_path) == (directory / "cover_letter_2026-02-02.md").resolve()
    assert find_review_evidence(tmp_path) == (directory / "Review_Evidence_2026-04-04.md").resolve()
