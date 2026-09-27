import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT, capture_output=True
    )
    return result.returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        "config/resume/main_resume_2026-01-01.md",
        "config/resume/cover_letter_2026-01-01.md",
        "config/resume/personalization.md",
        "config/resume/stray_notes.txt",
        "config/main_resume_2026-01-01.md",
    ],
)
def test_personal_resume_files_are_ignored(path):
    assert _ignored(path)


@pytest.mark.parametrize("path", ["config/resume/README.md", "config/resume.example.md"])
def test_tracked_resume_files_are_not_ignored(path):
    assert not _ignored(path)
