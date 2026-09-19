from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .adapters import adapter_class
from .atomic import atomic_write_text
from .cleanup import CleanupResult, run_cleanup
from .collector import Collector, select_companies
from .config import load_companies, load_profile, load_settings
from .logging_config import configure_logging
from .models import PIPELINE_NON_SUCCESS_STATUSES, Assessment, PipelineStatus
from .pipeline import latest_run_id, read_manifest, run_pipeline
from .rootutil import add_project_argument, chdir_to_project_root, nonneg_int
from .runlock import RunLockHeld, pid_alive, process_start_time
from .search_archive import archive_path, resolve_search_path
from .storage import Storage


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="job-hunter")
    add_project_argument(root)
    sub = root.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search")
    search.add_argument("--companies")
    search.add_argument("--all-companies", action="store_true")
    seen = search.add_mutually_exclusive_group()
    seen.add_argument("--include-seen", action="store_true")
    seen.add_argument("--new-only", action="store_true")
    search.add_argument("--refresh-details", action="store_true")
    search.add_argument(
        "--max-candidates",
        type=nonneg_int,
        help="optional cap on candidates returned; none by default — every prefilter match is kept",
    )
    search.add_argument(
        "--keyword",
        help=(
            "comma-separated keyword(s) (e.g. 'ADAS,Robotics,Product Technical Leader'); "
            "when set, replaces target_title_terms/target_domains as the prefilter's "
            "positive-match set for this run only (the profile file itself is untouched)"
        ),
    )
    search.add_argument("--json", action="store_true")
    output_group = search.add_mutually_exclusive_group()
    output_group.add_argument("--output", type=Path)
    output_group.add_argument(
        "--archive",
        action="store_true",
        help=(
            "write to an auto-named data/searches/{keyword-or-default}_{date}.json instead "
            "of choosing a path yourself with --output. The same keyword (and --companies "
            "scope, if given) on the same day overwrites (today's answer refreshing); a new "
            "day, a different keyword, or a different --companies scope gets its own file, "
            "so an earlier run's candidate snapshot is never silently lost. Mutually "
            "exclusive with --output."
        ),
    )
    search.add_argument("--verbose", action="store_true")
    search.add_argument("--debug", action="store_true")
    sub.add_parser("doctor")
    sub.add_parser("source-status")
    test = sub.add_parser("source-test")
    test.add_argument("company")
    sub.add_parser("db-stats")
    export = sub.add_parser("export")
    export.add_argument("--format", choices=["json"], default="json")
    record = sub.add_parser("record-assessment")
    record.add_argument(
        "--payload",
        required=True,
        help=(
            "JSON object: source_key, job_id, company, title, url, score (0-100), "
            "recommended (bool), matches (list[str]), gaps (list[str]), "
            "optional resume_path"
        ),
    )
    sub.add_parser("export-assessments")
    sub.add_parser("export-feedback")
    sub.add_parser(
        "reevaluate-sponsorship",
        help=(
            "re-run visa-sponsorship detection against every stored job's existing "
            "description (no network) — use after sponsorship.py's phrase list changes, "
            "or to backfill jobs collected before a source (e.g. a rate-limited one) has "
            "successfully re-fetched since visa_sponsorship was added"
        ),
    )
    sub.add_parser(
        "reevaluate-salary",
        help=(
            "re-run salary detection against every stored job's existing description "
            "(no network) — use after salary.py's pattern changes, or to backfill jobs "
            "collected before salary_evidence existed"
        ),
    )
    resolve = sub.add_parser(
        "resolve-search",
        help=(
            "resolve which data/searches/*.json archive a downstream stage (review, radar) "
            "should use, without running a new search — prints the path or exits non-zero "
            "with what's actually available. See docs/skill-split-plan.md section 4."
        ),
    )
    resolve_group = resolve.add_mutually_exclusive_group()
    resolve_group.add_argument("--search", type=Path, help="use this exact archive path")
    resolve_group.add_argument(
        "--keyword", help="resolve to the newest archive for this keyword's slug"
    )
    resolve.add_argument(
        "--companies",
        help=(
            "disambiguate among archives sharing --keyword by --companies scope (same value "
            "originally passed to job-hunter search/pipeline --companies) — omit to resolve "
            "only among unscoped archives, the default and overwhelming majority of real runs. "
            "Ignored together with --search, which always resolves to exactly that path."
        ),
    )
    cleanup = sub.add_parser(
        "cleanup",
        help=(
            "delete jobs closed past retention.closed_job_after_days, and generated "
            "profile-diff/radar reports older than retention.report_after_days (keeping the "
            "latest retention.keep_latest_reports_per_slug per slug regardless of age) — "
            "see config/settings.yaml and docs/retention-cleanup-plan.md. Dry-run by default; "
            "--apply is required to actually delete anything, and writes a pre-delete export "
            "to data/cleanup-exports/ first."
        ),
    )
    cleanup.add_argument(
        "--apply", action="store_true", help="actually delete; without this, only reports what would be deleted"
    )
    cleanup.add_argument(
        "--no-vacuum",
        action="store_true",
        help="skip VACUUM after deleting jobs (only relevant with --apply) — DELETE alone does not shrink the file",
    )
    scope = cleanup.add_mutually_exclusive_group()
    scope.add_argument("--jobs-only", action="store_true", help="only clean the database, not report files")
    scope.add_argument("--reports-only", action="store_true", help="only clean report files, not the database")
    cleanup.add_argument(
        "--no-export",
        action="store_true",
        help="skip writing the pre-delete export record (only relevant with --apply)",
    )
    pipeline = sub.add_parser(
        "pipeline",
        help=(
            "run search -> local-LLM review -> radar report end to end as one Python-owned "
            "command, writing a durable data/runs/<run_id>/manifest.json at every stage — the "
            "same three commands the job-hunter skill documents, sequenced deterministically "
            "instead of by agent prose. See `pipeline-status` to poll a run afterward."
        ),
    )
    pipeline.add_argument("--keyword", help="same as job-hunter search --keyword")
    pipeline.add_argument("--companies", help="same as job-hunter search --companies")
    pipeline.add_argument(
        "--limit", type=nonneg_int, help="cap on NEW reviews this run (passed through to the review stage)"
    )
    pipeline.add_argument("--new-only", action="store_true", help="same as job-hunter search --new-only")
    pipeline.add_argument("--refresh-details", action="store_true", help="same as job-hunter search --refresh-details")
    pipeline.add_argument(
        "--max-candidates", type=nonneg_int, help="same as job-hunter search --max-candidates"
    )
    pipeline.add_argument("--skip-review", action="store_true", help="search only; leave review/radar for later")
    pipeline.add_argument(
        "--skip-radar", action="store_true", help="search + review only; skip rendering the HTML report"
    )
    pipeline.add_argument(
        "--no-scrape",
        action="store_true",
        help=(
            "skip the live search stage and re-run scripts/refilter_archive.py against an "
            "already-resolved archive instead (the 'I edited candidate_profile.yaml, show me "
            "the report reflecting that, without a new scrape' workflow) — rejected together "
            "with --companies, since refiltering re-evaluates an archive's own already-"
            "attempted source scope, not a fresh company selection; see "
            "docs/pipeline-refilter-stale-source-plan.md section 4.2"
        ),
    )
    pipeline.add_argument(
        "--review",
        action="store_true",
        help=(
            "with --no-scrape only: also run the local-LLM review stage against whatever the "
            "refilter surfaced as new/changed — review defaults OFF in --no-scrape mode "
            "(opt in with this flag), the opposite default from normal pipeline mode's "
            "--skip-review opt-out"
        ),
    )
    status = sub.add_parser(
        "pipeline-status",
        help="print a pipeline run's manifest — the newest run by default, or --run <id>",
    )
    status.add_argument("--run", help="a specific run_id (default: the newest run overall)")
    # --project is registered on the root parser above so `job-hunter --project X <command>`
    # works, but argparse subparsers only see arguments that appear *after* the command token —
    # `job-hunter <command> --project X` would otherwise be rejected as unrecognized. Registering
    # it again on every subparser here (rather than only documenting "put it before the
    # subcommand") makes both positions work. This used to re-register with the same `default=None`
    # as the root parser, which was a real bug: argparse's subparser pass runs *after* the root
    # pass and unconditionally re-applies its own default whenever the subcommand's own args don't
    # repeat the flag, so `job-hunter --project X doctor` silently lost `X` the moment `doctor`'s
    # subparser re-defaulted it to `None` (confirmed live, and with a standalone argparse repro).
    # `suppress_default=True` fixes this: a subparser with `default=argparse.SUPPRESS` leaves
    # `args.project` untouched when the flag wasn't given at that position, so whichever parser
    # actually saw it wins either way. Confirmed live: every skills/*/SKILL.md example shows
    # `<command> --project ...` (after the subcommand) — that shape must work, not just be
    # documented as the "wrong" order to avoid.
    for subparser in sub.choices.values():
        add_project_argument(subparser, suppress_default=True)
    return root


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)


def _print_cleanup_result(result: CleanupResult) -> None:
    verb = "Deleted" if result.applied else "Eligible for deletion (dry run — nothing deleted)"
    jobs_count = result.closed_jobs_deleted if result.applied else result.closed_jobs_eligible
    print(f"{verb}: {jobs_count} closed job(s)")
    if result.applied and jobs_count:
        print(
            f"  cascaded: {result.assessments_deleted} assessment(s), "
            f"{result.job_feedback_deleted} job_feedback label(s)"
        )
        if result.db_size_before is not None:
            print(
                f"  database: {result.db_size_before:,} -> {result.db_size_after:,} bytes"
            )
    reports = result.reports_deleted if result.applied else result.reports_eligible
    print(f"{verb}: {len(reports)} report file(s)")
    for path in reports:
        print(f"  {path}")
    if result.applied:
        if result.export_path:
            print(f"Wrote pre-delete export: {result.export_path}")
    elif jobs_count or reports:
        print("Re-run with --apply to actually delete these (writes an export first).")


def _write_assessments_export(settings, rows: list[dict[str, Any]]) -> Path:
    path = settings.database_path.parent / "assessments.json"
    atomic_write_text(path, _json(rows) + "\n")
    return path


def _write_feedback_export(settings, rows: list[dict[str, Any]]) -> Path:
    path = settings.database_path.parent / "job_feedback.json"
    atomic_write_text(path, _json(rows) + "\n")
    return path


async def _source_test(key: str) -> int:
    import httpx

    settings = load_settings()
    companies = select_companies(load_companies(), key)
    async with httpx.AsyncClient(
        timeout=settings.collection.timeout_seconds, follow_redirects=True
    ) as client:
        adapter = adapter_class(companies[0].adapter)(companies[0], client, settings.collection)
        health = await adapter.healthcheck()
    print(_json(health.model_dump(mode="json")))
    return 0 if health.status.value in {"ok", "warning"} else 1


def _stealth_browser_check(companies: list | None) -> tuple[str, bool, str]:
    """Only actually a check when a configured, enabled company needs it — the stealth
    headless browser is an opt-in dependency (`--extra stealth`), not a default one, so
    its absence is only a real failure for a source that would actually invoke it.
    A pure function of (companies, whether scrapling is importable) so `doctor()`'s
    control flow around config-loading failures stays simple and this stays unit-testable
    without touching the filesystem."""
    stealth_companies = (
        sorted(c.key for c in companies if c.enabled and c.adapter == "stealth_html")
        if companies is not None
        else []
    )
    if not stealth_companies:
        return (
            "stealth browser",
            True,
            "not needed — no enabled company config uses the stealth_html adapter",
        )
    installed = importlib.util.find_spec("scrapling") is not None
    detail = (
        f"required by: {', '.join(stealth_companies)}"
        if installed
        else f"required by: {', '.join(stealth_companies)}, but the 'stealth' dependency "
        "group is not installed (uv sync --extra stealth && uv run scrapling install)"
    )
    return "stealth browser", installed, detail


def _hermes_hook_check() -> tuple[str, bool, str]:
    """Confirms a registered Hermes profile-diff hook's own script still exists on disk —
    catches a stale registration left pointing at a moved/deleted checkout (the relocatable-
    registration fix in `scripts/install_hermes_hook.py` stops a *reinstall* from leaving a stale
    entry behind, but a checkout that's moved without ever re-running `install_skill.sh --hermes`
    from the new location is still silently broken until something surfaces it —
    docs/agent-runtime-audit.md's "Hermes registration is not relocatable" finding, point 4). A
    machine with no Hermes install at all (the common case) is reported OK/not-applicable, not a
    failure — same shape as `_stealth_browser_check`'s "not needed" branch."""
    import yaml

    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    config_path = hermes_home / "config.yaml"
    if not config_path.exists():
        return "hermes hook", True, "not configured — no Hermes config.yaml found"
    try:
        config = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as exc:
        return "hermes hook", False, f"{config_path} did not parse: {exc}"
    hooks = config.get("hooks") if isinstance(config, dict) else None
    entries = hooks.get("post_tool_call") if isinstance(hooks, dict) else None
    hook_script = hermes_home / "agent-hooks" / "job-hunter-profile.py"
    registered = isinstance(entries, list) and any(
        isinstance(e, dict) and isinstance(e.get("command"), str) and str(hook_script) in e["command"]
        for e in entries
    )
    if not registered:
        return "hermes hook", True, "not registered"
    if hook_script.exists():
        return "hermes hook", True, str(hook_script)
    return (
        "hermes hook",
        False,
        f"registered in {config_path} but {hook_script} does not exist — re-run "
        "`scripts/install_skill.sh --hermes --update` from the current checkout",
    )


def doctor() -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("python", sys.version_info >= (3, 11), sys.version.split()[0]))
    checks.append(
        (
            "uv environment",
            bool(os.environ.get("VIRTUAL_ENV")),
            os.environ.get("VIRTUAL_ENV", "not active"),
        )
    )
    companies = None
    try:
        settings, companies, profile = load_settings(), load_companies(), load_profile()
        checks.append(("configuration", True, f"{len(companies)} companies"))
        with Storage(settings.database_path):
            pass
        checks.append(("database", True, str(settings.database_path)))
        if profile.resume_path:
            checks.append(("resume", profile.resume_path.exists(), str(profile.resume_path)))
        else:
            checks.append(("resume", True, "not configured; profile-only ranking"))
    except Exception as exc:
        checks.append(("configuration/database", False, str(exc)))
    packages = all(
        importlib.util.find_spec(name) for name in ("httpx", "pydantic", "yaml", "selectolax")
    )
    checks.append(("dependencies", packages, "required imports"))
    try:
        socket.getaddrinfo("example.com", 443)
        checks.append(("DNS", True, "available"))
    except OSError as exc:
        checks.append(("DNS", False, str(exc)))
    checks.append(_stealth_browser_check(companies))
    checks.append(_hermes_hook_check())
    for name, ok, detail in checks:
        print(f"{'OK' if ok else 'FAIL':4} {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        chdir_to_project_root(args.project)
        if args.command == "doctor":
            return doctor()
        settings = load_settings()
        if args.command in {
            "source-status", "db-stats", "export", "export-assessments", "export-feedback",
        }:
            with Storage(settings.database_path) as storage:
                value = (
                    storage.health_rows()
                    if args.command == "source-status"
                    else storage.stats()
                    if args.command == "db-stats"
                    else storage.export_active()
                    if args.command == "export"
                    else storage.export_assessments()
                    if args.command == "export-assessments"
                    else storage.export_job_feedback()
                )
            if args.command == "export-assessments":
                _write_assessments_export(settings, value)
            elif args.command == "export-feedback":
                _write_feedback_export(settings, value)
            print(_json(value))
            return 0
        if args.command == "reevaluate-sponsorship":
            with Storage(settings.database_path) as storage:
                changed = storage.reevaluate_sponsorship()
            print(f"Re-evaluated visa sponsorship for every stored job; {changed} changed.")
            return 0
        if args.command == "reevaluate-salary":
            with Storage(settings.database_path) as storage:
                changed = storage.reevaluate_salary()
            print(f"Re-evaluated salary for every stored job; {changed} changed.")
            return 0
        if args.command == "resolve-search":
            resolved = resolve_search_path(search=args.search, keyword=args.keyword, companies=args.companies)
            print(resolved)
            return 0
        if args.command == "pipeline-status":
            run_id = args.run or latest_run_id()
            if run_id is None:
                print("job-hunter: no pipeline runs found (data/runs is empty)", file=sys.stderr)
                return 2
            manifest = read_manifest(run_id)
            # A manifest can be stuck at RUNNING forever if its process died without updating it
            # (killed, crashed, machine restarted) — the manifest file itself never lies about
            # what it last wrote, so this is a read-time, presentation-only verdict computed here,
            # never persisted back to the manifest (see docs/agent-runtime-audit.md's "abandoned
            # vs. running" finding). `pid_alive` alone isn't sufficient: the OS can reuse a dead
            # process's pid for an unrelated later process, which would make a genuinely-abandoned
            # manifest look live for the wrong reason ("PID reuse" finding). When the manifest
            # recorded `pid_start_time` (process identity, not just a number) and the pid's
            # *current* start time can be determined right now, a mismatch means this isn't the
            # same process anymore — treated as abandoned even though `pid_alive` alone would say
            # "alive". Either side being unavailable (`None`) means "can't verify" and falls back
            # to the original PID-only liveness check, never treated as evidence of reuse.
            current_start_time = (
                process_start_time(manifest.pid)
                if manifest.pid is not None and manifest.pid_start_time is not None
                else None
            )
            pid_reused = (
                manifest.pid_start_time is not None
                and current_start_time is not None
                and current_start_time != manifest.pid_start_time
            )
            abandoned = manifest.status == PipelineStatus.RUNNING and manifest.pid is not None and (
                not pid_alive(manifest.pid) or pid_reused
            )
            payload = manifest.model_dump(mode="json")
            if abandoned:
                payload["last_written_status"] = payload["status"]
                payload["status"] = "abandoned"
            print(_json(payload))
            if abandoned:
                return 2
            return 0 if manifest.status not in PIPELINE_NON_SUCCESS_STATUSES else 2
        if args.command == "pipeline":
            if args.no_scrape and args.companies:
                print(
                    "job-hunter: --companies has no effect with --no-scrape (refiltering "
                    "re-evaluates an archive's own already-attempted source scope, not a "
                    "fresh company selection) — drop one of the two flags",
                    file=sys.stderr,
                )
                return 2
            manifest = asyncio.run(
                run_pipeline(
                    settings,
                    Path.cwd(),
                    keyword=args.keyword,
                    companies_filter=args.companies,
                    limit=args.limit,
                    new_only=args.new_only,
                    refresh_details=args.refresh_details,
                    max_candidates=args.max_candidates,
                    skip_review=args.skip_review,
                    skip_radar=args.skip_radar,
                    no_scrape=args.no_scrape,
                    review=args.review,
                )
            )
            print(_json(manifest.model_dump(mode="json")))
            return 0 if manifest.status not in PIPELINE_NON_SUCCESS_STATUSES else 2
        if args.command == "cleanup":
            result = run_cleanup(
                settings,
                apply=args.apply,
                jobs_only=args.jobs_only,
                reports_only=args.reports_only,
                vacuum=not args.no_vacuum,
                write_export=not args.no_export,
            )
            _print_cleanup_result(result)
            return 0
        if args.command == "record-assessment":
            payload = json.loads(args.payload)
            payload.pop("content_hash", None)  # always server-derived, never caller-supplied
            with Storage(settings.database_path) as storage:
                prior_job = storage.get_job(
                    payload.get("source_key", ""), payload.get("job_id", "")
                )
                assessment = Assessment(
                    **payload, content_hash=prior_job["content_hash"] if prior_job else None
                )
                storage.upsert_assessment(assessment)
                rows = storage.export_assessments()
            _write_assessments_export(settings, rows)
            print(_json(assessment.model_dump(mode="json")))
            return 0
        if args.command == "source-test":
            return asyncio.run(_source_test(args.company))
        configure_logging(verbose=args.verbose, debug=args.debug)
        companies = select_companies(load_companies(), args.companies)
        keywords = (
            [term.strip() for term in args.keyword.split(",") if term.strip()]
            if args.keyword
            else None
        )
        result = asyncio.run(
            Collector(settings, companies, load_profile()).search(
                include_seen=not args.new_only,
                new_only=args.new_only,
                refresh_details=args.refresh_details,
                max_candidates=args.max_candidates,
                keywords=keywords,
            )
        )
        rendered = _json(result.model_dump(mode="json"))
        output_path = archive_path(args.keyword, companies=args.companies) if args.archive else args.output
        if output_path:
            atomic_write_text(output_path, rendered + "\n")
            print(f"Archived to: {output_path}")
        if args.json:
            print(rendered)
        else:
            summary = result.summary
            print(
                f"Sources: {summary.sources_attempted} attempted / {summary.sources_succeeded} succeeded / {summary.sources_failed} failed"
            )
            print(
                f"Jobs: {summary.jobs_observed} observed / {summary.us_eligible} U.S.-eligible / "
                f"{summary.stale_excluded} excluded as stale / {summary.prefilter_candidates} candidates"
            )
            for health in result.source_health:
                print(
                    f"{health.source_key}: {health.status.value} ({health.job_count}){': ' + health.message if health.message else ''}"
                )
        return 0 if result.summary.sources_succeeded else 2
    except (FileNotFoundError, ValueError, ValidationError, RunLockHeld) as exc:
        print(f"job-hunter: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
