from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunter.cli import _stealth_browser_check, archive_path, parser
from job_hunter.config import CompanyConfig


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


def test_archive_path_uses_the_calendar_date_of_whatever_tzinfo_now_carries():
    """A run late in the evening in a US timezone is already past midnight UTC — the archive
    date must reflect the day it was actually run on, not tomorrow's UTC date. `archive_path`
    achieves this by formatting `now` using whatever tzinfo it's given rather than forcing a
    UTC conversion; production passes a real system-local `now` (see search_archive.py), and
    this test proves the formatting is correct given an explicitly non-UTC one."""
    from zoneinfo import ZoneInfo

    late_eastern = datetime(2026, 9, 8, 22, 30, tzinfo=ZoneInfo("America/New_York"))
    assert late_eastern.astimezone(UTC).date() == datetime(2026, 9, 9).date()
    assert archive_path("ADAS", now=late_eastern) == Path("data/searches/adas_2026-09-08.json")


def _company(key: str, adapter: str, *, enabled: bool = True) -> CompanyConfig:
    return CompanyConfig(
        key=key,
        company=key,
        adapter=adapter,
        enabled=enabled,
        unsupported_reason="test" if adapter == "unsupported" else None,
    )


def test_stealth_browser_check_not_needed_without_any_stealth_company():
    name, ok, detail = _stealth_browser_check([_company("lever_co", "lever")])
    assert (name, ok) == ("stealth browser", True)
    assert "not needed" in detail


def test_stealth_browser_check_not_needed_when_companies_unknown():
    """Config failed to load entirely (doctor()'s earlier try/except caught it) — must not
    crash or false-negative just because there's no company list to inspect."""
    name, ok, detail = _stealth_browser_check(None)
    assert (name, ok) == ("stealth browser", True)


def test_stealth_browser_check_ignores_disabled_stealth_company():
    name, ok, detail = _stealth_browser_check(
        [_company("astemo", "stealth_html", enabled=False)]
    )
    assert ok is True
    assert "not needed" in detail


def test_stealth_browser_check_fails_when_needed_but_not_installed(monkeypatch):
    import job_hunter.cli as cli_module

    monkeypatch.setattr(
        cli_module.importlib.util, "find_spec", lambda name: None if name == "scrapling" else object()
    )
    name, ok, detail = _stealth_browser_check([_company("astemo", "stealth_html")])
    assert (name, ok) == ("stealth browser", False)
    assert "astemo" in detail and "stealth" in detail


def test_stealth_browser_check_ok_when_needed_and_installed(monkeypatch):
    import job_hunter.cli as cli_module

    monkeypatch.setattr(cli_module.importlib.util, "find_spec", lambda name: object())
    name, ok, detail = _stealth_browser_check(
        [_company("astemo", "stealth_html"), _company("google", "stealth_html")]
    )
    assert (name, ok) == ("stealth browser", True)
    assert "astemo" in detail and "google" in detail


def test_cleanup_defaults_to_dry_run():
    """--apply is required to actually delete anything — the default must be safe."""
    args = parser().parse_args(["cleanup"])
    assert args.apply is False
    assert args.no_vacuum is False
    assert args.jobs_only is False
    assert args.reports_only is False
    assert args.no_export is False


def test_cleanup_jobs_only_and_reports_only_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parser().parse_args(["cleanup", "--jobs-only", "--reports-only"])
