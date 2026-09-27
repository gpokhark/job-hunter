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


def test_real_salary_config_is_ignored():
    assert _ignored("config/salary_config.json")


@pytest.mark.parametrize(
    "path",
    [
        "data/output/Acme/offer_Acme_SWE_2026-09-27.json",
        "data/output/Acme/comparison_2026-09-27.md",
        "data/output/Acme/negotiation-plan_2026-09-27.md",
    ],
)
def test_offer_and_report_files_are_ignored_under_data_output(path):
    assert _ignored(path)


@pytest.mark.parametrize("path", ["config/salary_config.example.json", "config/offer.example.json"])
def test_tracked_salary_templates_are_not_ignored(path):
    assert not _ignored(path)
