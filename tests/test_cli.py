import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunter.cli import _hermes_hook_check, _stealth_browser_check, archive_path, main, parser
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


def _write_archive_with_source_health(path: Path, source_health: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"candidates": [], "source_health": source_health}))


def test_resolve_search_cli_prints_only_the_path_on_stdout_and_scope_on_stderr(tmp_path, monkeypatch, capsys):
    """Regression test for docs/agent-runtime-audit.md's "archive scope is invisible" gap: the
    scope summary must never leak into stdout, since scripted callers ($(job-hunter resolve-search
    ...)) treat stdout as the bare resolved path and nothing else."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("{}\n")
    archive = tmp_path / "data" / "searches" / "default_2026-09-17.json"
    _write_archive_with_source_health(
        archive, [{"source_key": "openai", "company": "OpenAI", "status": "ok", "job_count": 20}]
    )

    exit_code = main(["resolve-search", "--keyword", "default"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "data/searches/default_2026-09-17.json"
    assert "scope: 1 source attempted (openai)" in captured.err


def test_resolve_search_cli_scope_summary_orders_by_job_count_and_truncates(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("{}\n")
    archive = tmp_path / "data" / "searches" / "default_2026-09-15.json"
    source_health = [
        {"source_key": f"source-{n}", "company": f"Source {n}", "status": "ok", "job_count": n}
        for n in [5, 40, 1, 82, 20, 10, 3]
    ]
    _write_archive_with_source_health(archive, source_health)

    exit_code = main(["resolve-search", "--keyword", "default"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "data/searches/default_2026-09-15.json"
    # Descending by job_count: source-82(82), source-40(40), source-20(20), source-10(10), source-5(5),
    # then "+2 more" (source-3, source-1).
    assert "scope: 7 sources attempted (source-82, source-40, source-20, source-10, source-5, ... +2 more)" in captured.err


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


def test_hermes_hook_check_ok_when_no_hermes_home_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-such-hermes-home"))
    name, ok, detail = _hermes_hook_check()
    assert (name, ok) == ("hermes hook", True)
    assert "not configured" in detail


def test_hermes_hook_check_ok_when_hermes_configured_but_hook_not_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model: example\n")
    name, ok, detail = _hermes_hook_check()
    assert (name, ok) == ("hermes hook", True)
    assert "not registered" in detail


def test_hermes_hook_check_ok_when_registered_and_script_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    hook_script = tmp_path / "agent-hooks" / "job-hunter-profile.py"
    hook_script.parent.mkdir(parents=True)
    hook_script.write_text("# hook\n")
    (tmp_path / "config.yaml").write_text(
        "hooks:\n  post_tool_call:\n"
        f"    - command: python3 {hook_script} /some/repo\n      matcher: x\n"
    )
    name, ok, detail = _hermes_hook_check()
    assert (name, ok) == ("hermes hook", True)
    assert str(hook_script) in detail


def test_hermes_hook_check_fails_when_registered_but_script_missing(tmp_path, monkeypatch):
    """docs/agent-runtime-audit.md's "Hermes registration is not relocatable" finding, point 4:
    a checkout moved/deleted without ever re-running the installer from its new location leaves a
    registration pointing at nothing -- must be a diagnosable warning, not silent."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    missing_hook_script = tmp_path / "agent-hooks" / "job-hunter-profile.py"
    (tmp_path / "config.yaml").write_text(
        "hooks:\n  post_tool_call:\n"
        f"    - command: python3 {missing_hook_script} /some/repo\n      matcher: x\n"
    )
    name, ok, detail = _hermes_hook_check()
    assert (name, ok) == ("hermes hook", False)
    assert "does not exist" in detail


def test_hermes_hook_check_fails_on_unparseable_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(":\n  - not: [valid\n")
    name, ok, detail = _hermes_hook_check()
    assert (name, ok) == ("hermes hook", False)
    assert "did not parse" in detail


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


def test_pipeline_search_without_no_scrape_is_rejected_before_run_pipeline_is_ever_called(
    monkeypatch, capsys
):
    """--search only makes sense with --no-scrape (a live search always writes a fresh archive
    rather than resolving an existing one) — same short-circuit-before-run_pipeline discipline as
    the --companies+--no-scrape guard just above."""
    import job_hunter.cli as cli_module

    def _unexpected_run_pipeline(*args, **kwargs):
        raise AssertionError("run_pipeline must not be called when --search without --no-scrape is rejected")

    monkeypatch.setattr(cli_module, "run_pipeline", _unexpected_run_pipeline)

    exit_code = cli_module.main(["pipeline", "--search", "data/searches/default_2026-09-15.json"])
    assert exit_code == 2
    assert "--search" in capsys.readouterr().err


def test_pipeline_no_scrape_search_reaches_run_pipeline_with_the_exact_path(monkeypatch):
    """The plumbing half of the --search fix: once past cli.py's own guard, run_pipeline must
    receive the exact path given, not None (which would fall back to keyword/mtime resolution —
    the whole thing this flag exists to bypass)."""
    import job_hunter.cli as cli_module

    captured = {}

    async def _fake_run_pipeline(settings, project_root, **kwargs):
        captured.update(kwargs)
        return PipelineManifest(run_id="x", project_root=str(project_root), status=PipelineStatus.PARTIAL)

    monkeypatch.setattr(cli_module, "run_pipeline", _fake_run_pipeline)

    exit_code = cli_module.main(
        ["pipeline", "--no-scrape", "--search", "data/searches/default_2026-09-15.json"]
    )
    assert exit_code == 0
    assert captured["search"] == Path("data/searches/default_2026-09-15.json")


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


# --- reject negative values on numeric CLI options (docs/agent-runtime-audit.md's "input
# validation" finding) -- a bare `type=int` silently accepted a negative --limit/--max-candidates,
# which then flowed into a downstream Python slice (`to_review[:args.limit]`) as a *valid* but
# surprising "all but the last N" instead of a clear command-line error.


@pytest.mark.parametrize(
    "argv",
    [
        ["search", "--max-candidates", "-1"],
        ["pipeline", "--limit", "-1"],
        ["pipeline", "--max-candidates", "-1"],
    ],
)
def test_negative_numeric_options_are_rejected_at_parse_time(argv, capsys):
    with pytest.raises(SystemExit) as exc_info:
        parser().parse_args(argv)
    assert exc_info.value.code == 2
    assert "non-negative" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["search", "--max-candidates", "0"],
        ["pipeline", "--limit", "0"],
        ["pipeline", "--max-candidates", "0"],
    ],
)
def test_zero_is_still_accepted_for_numeric_options(argv):
    """The fix rejects *negative* values specifically -- zero is a legitimate (if degenerate)
    value for a cap/limit and must keep parsing cleanly."""
    args = parser().parse_args(argv)
    value = args.max_candidates if "--max-candidates" in argv else args.limit
    assert value == 0


def test_all_companies_flag_was_removed():
    """docs/agent-runtime-audit.md's "--all-companies is dead" finding: it parsed but did
    nothing -- omitting --companies already means every enabled company, `search`'s unconditional
    default. Regression test for the removal, not the flag itself."""
    with pytest.raises(SystemExit):
        parser().parse_args(["search", "--all-companies"])
