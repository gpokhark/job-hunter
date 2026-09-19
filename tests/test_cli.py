import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunter.cli import _stealth_browser_check, archive_path, main, parser
from job_hunter.config import CompanyConfig
from job_hunter.models import PipelineManifest, PipelineStatus
from job_hunter.pipeline import write_manifest
from job_hunter.search_archive import resolve_search_path


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


# --- archive_path + --companies: a company-scoped run must never collide with the full/default
# run's filename — confirmed live the hard way (see search_archive.py's archive_path docstring:
# a --companies-scoped job-hunter pipeline run silently overwrote a same-day 65-source archive
# and its radar report before this scoping existed). ---


def test_archive_path_without_companies_is_unchanged():
    now = datetime(2026, 9, 17, tzinfo=UTC)
    assert archive_path(None, now=now) == Path("data/searches/default_2026-09-17.json")
    assert archive_path(None, companies=None, now=now) == archive_path(None, now=now)


def test_archive_path_with_companies_never_collides_with_the_unscoped_path():
    now = datetime(2026, 9, 17, tzinfo=UTC)
    scoped = archive_path(None, companies="openai", now=now)
    unscoped = archive_path(None, now=now)
    assert scoped != unscoped
    assert scoped == Path("data/searches/default__companies-openai_2026-09-17.json")


def test_archive_path_companies_scope_is_order_independent():
    now = datetime(2026, 9, 17, tzinfo=UTC)
    assert archive_path("ADAS", companies="honda,toyota", now=now) == archive_path(
        "ADAS", companies="toyota,honda", now=now
    )


def test_archive_path_different_companies_scopes_are_distinct():
    now = datetime(2026, 9, 17, tzinfo=UTC)
    assert archive_path(None, companies="openai", now=now) != archive_path(
        None, companies="honda", now=now
    )


def test_resolve_search_path_without_companies_ignores_a_scoped_archive_sharing_the_keyword(
    tmp_path, monkeypatch
):
    """Regression test for docs/agent-runtime-audit.md's "archive resolution is not
    company-scope aware" finding: before the fix, `{slug}_*.json`'s own glob incidentally matched
    a `{slug}__companies-{scope}_*.json` filename too (the scope suffix falls inside the
    wildcard), so a standalone review/radar/resolve-search call with no --companies could
    silently resolve to a company-scoped archive instead of the unscoped one it actually meant."""
    monkeypatch.chdir(tmp_path)
    search_dir = tmp_path / "data" / "searches"
    search_dir.mkdir(parents=True)
    unscoped = search_dir / "adas_2026-09-17.json"
    scoped = search_dir / "adas__companies-honda_2026-09-17.json"
    unscoped.write_text("{}")
    scoped.write_text("{}")
    # Make the scoped archive the newer file by mtime -- if the old bug were still present,
    # "newest mtime wins" would incorrectly select it.
    os.utime(unscoped, (1, 1))
    os.utime(scoped, (2, 2))

    resolved = resolve_search_path(keyword="adas")

    assert resolved.resolve() == unscoped.resolve()


def test_resolve_search_path_with_companies_resolves_the_scoped_archive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    search_dir = tmp_path / "data" / "searches"
    search_dir.mkdir(parents=True)
    unscoped = search_dir / "adas_2026-09-17.json"
    scoped = search_dir / "adas__companies-honda_2026-09-17.json"
    unscoped.write_text("{}")
    scoped.write_text("{}")

    resolved = resolve_search_path(keyword="adas", companies="honda")

    assert resolved.resolve() == scoped.resolve()


def test_resolve_search_path_companies_scope_is_order_independent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    search_dir = tmp_path / "data" / "searches"
    search_dir.mkdir(parents=True)
    scoped = search_dir / "adas__companies-honda-toyota_2026-09-17.json"
    scoped.write_text("{}")

    assert resolve_search_path(keyword="adas", companies="toyota,honda").resolve() == scoped.resolve()


def test_resolve_search_path_missing_companies_scope_raises_with_scope_in_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    search_dir = tmp_path / "data" / "searches"
    search_dir.mkdir(parents=True)
    (search_dir / "adas_2026-09-17.json").write_text("{}")

    with pytest.raises(FileNotFoundError, match="honda"):
        resolve_search_path(keyword="adas", companies="honda")


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


# --- section 4.2: `pipeline --no-scrape [--review]` ---


def test_pipeline_no_scrape_and_review_default_off():
    """Normal (live-search) pipeline mode is the default — no-scrape/review are both opt-in
    flags, not the other way around."""
    args = parser().parse_args(["pipeline"])
    assert args.no_scrape is False
    assert args.review is False


def test_pipeline_no_scrape_and_review_flags_parse():
    args = parser().parse_args(["pipeline", "--no-scrape", "--review"])
    assert args.no_scrape is True
    assert args.review is True


def test_pipeline_no_scrape_with_companies_is_rejected_before_run_pipeline_is_ever_called(
    monkeypatch, capsys
):
    """cli.py's own guard (independent of run_pipeline's matching ValueError guard, covered in
    tests/test_pipeline.py) — this must short-circuit with a clear stderr message and exit code
    2 without ever invoking run_pipeline, since --companies silently doing nothing here would be
    worse than refusing outright."""
    import job_hunter.cli as cli_module

    def _unexpected_run_pipeline(*args, **kwargs):
        raise AssertionError("run_pipeline must not be called when --no-scrape+--companies is rejected")

    monkeypatch.setattr(cli_module, "run_pipeline", _unexpected_run_pipeline)

    exit_code = cli_module.main(["pipeline", "--no-scrape", "--companies", "honda"])
    assert exit_code == 2
    assert "--no-scrape" in capsys.readouterr().err


def test_pipeline_status_reports_abandoned_for_a_dead_pid(tmp_path, monkeypatch, capsys):
    """A manifest can be stuck at RUNNING forever if its process died without updating it
    (killed, crashed, machine restarted) — `pipeline-status` must say so rather than reporting a
    dead run as still in progress (docs/agent-runtime-audit.md's "abandoned vs. running" finding).
    The manifest file on disk is never rewritten; this is a read-time, presentation-only verdict."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("{}\n")

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid

    manifest = PipelineManifest(
        run_id="abandoned-run", project_root=str(tmp_path), pid=dead_pid, status=PipelineStatus.RUNNING,
    )
    write_manifest(manifest)

    exit_code = main(["pipeline-status", "--run", "abandoned-run"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "abandoned"
    assert payload["last_written_status"] == "running"


def test_pipeline_status_reports_running_for_a_live_pid(tmp_path, monkeypatch, capsys):
    """The counterpart to the abandoned case: a RUNNING manifest whose pid is genuinely still
    alive (this test process itself) must be reported as running, not misdiagnosed as abandoned."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("{}\n")

    manifest = PipelineManifest(
        run_id="live-run", project_root=str(tmp_path), pid=os.getpid(), status=PipelineStatus.RUNNING,
    )
    write_manifest(manifest)

    exit_code = main(["pipeline-status", "--run", "live-run"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "running"
    assert "last_written_status" not in payload


def test_pipeline_status_reports_abandoned_when_pid_was_reused(tmp_path, monkeypatch, capsys):
    """docs/agent-runtime-audit.md's "PID reuse" finding: a live pid alone isn't proof it's the
    *same* process the manifest was written for -- the OS can hand a dead process's pid number to
    an unrelated later process. A manifest whose recorded `pid_start_time` doesn't match the pid's
    real current start time must be reported as abandoned even though `pid_alive()` alone would
    say "alive" (this test process's own pid genuinely is alive)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("{}\n")

    manifest = PipelineManifest(
        run_id="reused-pid-run",
        project_root=str(tmp_path),
        pid=os.getpid(),
        pid_start_time="Thu Jan  1 00:00:00 1970",  # never this test process's real start time
        status=PipelineStatus.RUNNING,
    )
    write_manifest(manifest)

    exit_code = main(["pipeline-status", "--run", "reused-pid-run"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "abandoned"
    assert payload["last_written_status"] == "running"


def test_pipeline_status_with_no_pid_start_time_falls_back_to_pid_only_liveness(
    tmp_path, monkeypatch, capsys
):
    """A manifest written before `pid_start_time` existed (or where `ps` wasn't available at
    record time) must keep working exactly as before -- an absent recorded value is never treated
    as evidence of anything, only a genuine mismatch is."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("{}\n")

    manifest = PipelineManifest(
        run_id="no-start-time-run",
        project_root=str(tmp_path),
        pid=os.getpid(),
        pid_start_time=None,
        status=PipelineStatus.RUNNING,
    )
    write_manifest(manifest)

    exit_code = main(["pipeline-status", "--run", "no-start-time-run"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "running"
