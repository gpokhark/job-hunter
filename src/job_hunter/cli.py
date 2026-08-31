from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .adapters import adapter_class
from .collector import Collector, select_companies
from .config import load_companies, load_profile, load_settings
from .logging_config import configure_logging
from .models import Assessment
from .storage import Storage


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="job-hunter")
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
        type=int,
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
            "of choosing a path yourself with --output. The same keyword on the same day "
            "overwrites (today's answer refreshing); a new day or a different keyword gets "
            "its own file, so an earlier run's candidate snapshot is never silently lost. "
            "Mutually exclusive with --output."
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
    sub.add_parser(
        "reevaluate-sponsorship",
        help=(
            "re-run visa-sponsorship detection against every stored job's existing "
            "description (no network) — use after sponsorship.py's phrase list changes, "
            "or to backfill jobs collected before a source (e.g. a rate-limited one) has "
            "successfully re-fetched since visa_sponsorship was added"
        ),
    )
    return root


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"


def archive_path(keyword: str | None, *, now: datetime | None = None) -> Path:
    """The deterministic data/searches/{slug}_{date}.json path for --archive, computed
    from the same --keyword string passed to `search` (or "default" without one) and
    today's UTC date — same inputs always produce the same path, so a caller (the
    job-hunter skill, or any other agent runtime) can predict it without parsing stdout,
    and a same-day rerun with the same keyword deliberately overwrites rather than
    accumulating duplicates."""
    slug = _slugify(keyword) if keyword else "default"
    date_str = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    return Path("data/searches") / f"{slug}_{date_str}.json"


def _write_assessments_export(settings, rows: list[dict[str, Any]]) -> Path:
    path = settings.database_path.parent / "assessments.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json(rows) + "\n", encoding="utf-8")
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
    checks.append(
        (
            "headless",
            True,
            "no browser/DISPLAY dependency, except the opt-in stealth_html adapter "
            "(astemo, google) which runs a headless browser "
            "and needs `--extra stealth`",
        )
    )
    for name, ok, detail in checks:
        print(f"{'OK' if ok else 'FAIL':4} {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor()
        settings = load_settings()
        if args.command in {"source-status", "db-stats", "export", "export-assessments"}:
            with Storage(settings.database_path) as storage:
                value = (
                    storage.health_rows()
                    if args.command == "source-status"
                    else storage.stats()
                    if args.command == "db-stats"
                    else storage.export_active()
                    if args.command == "export"
                    else storage.export_assessments()
                )
            if args.command == "export-assessments":
                _write_assessments_export(settings, value)
            print(_json(value))
            return 0
        if args.command == "reevaluate-sponsorship":
            with Storage(settings.database_path) as storage:
                changed = storage.reevaluate_sponsorship()
            print(f"Re-evaluated visa sponsorship for every stored job; {changed} changed.")
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
                include_seen=args.include_seen or not args.new_only,
                new_only=args.new_only,
                refresh_details=args.refresh_details,
                max_candidates=args.max_candidates,
                keywords=keywords,
            )
        )
        rendered = _json(result.model_dump(mode="json"))
        output_path = archive_path(args.keyword) if args.archive else args.output
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered + "\n", encoding="utf-8")
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
    except (FileNotFoundError, ValueError, ValidationError) as exc:
        print(f"job-hunter: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
