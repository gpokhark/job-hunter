from datetime import UTC, datetime
from pathlib import Path

from job_hunter.cli import archive_path


def test_archive_path_defaults_to_default_slug_without_keyword():
    now = datetime(2026, 8, 31, tzinfo=UTC)
    assert archive_path(None, now=now) == Path("data/searches/default_2026-08-31.json")


def test_archive_path_slugifies_keyword():
    now = datetime(2026, 8, 31, tzinfo=UTC)
    assert archive_path("Product Manager", now=now) == Path(
        "data/searches/product-manager_2026-08-31.json"
    )


def test_archive_path_slugifies_multi_keyword_and_punctuation():
    now = datetime(2026, 8, 31, tzinfo=UTC)
    assert archive_path("ADAS, Robotics!", now=now) == Path(
        "data/searches/adas-robotics_2026-08-31.json"
    )


def test_archive_path_same_keyword_same_day_is_stable():
    """A rerun of the same keyword on the same day must resolve to the exact same path —
    that's what makes it overwrite rather than accumulate duplicates."""
    now = datetime(2026, 8, 31, 23, 59, tzinfo=UTC)
    later_same_day = datetime(2026, 8, 31, 0, 1, tzinfo=UTC)
    assert archive_path("ADAS", now=now) == archive_path("ADAS", now=later_same_day)


def test_archive_path_changes_across_days():
    day1 = datetime(2026, 8, 31, tzinfo=UTC)
    day2 = datetime(2026, 9, 1, tzinfo=UTC)
    assert archive_path("ADAS", now=day1) != archive_path("ADAS", now=day2)
