"""`job-hunter cleanup` — deletes closed jobs past their retention window and old generated
profile-diff/radar reports. See docs/retention-cleanup-plan.md for the full design rationale;
this module is the resolved implementation of that plan.

Two independent halves, either can be skipped (`--jobs-only`/`--reports-only`):
- **Jobs**: `Storage.delete_closed_jobs()` (storage.py) — a job closed longer than
  `retention.closed_job_after_days` ago is deleted, cascading to its own assessments/feedback.
- **Reports**: this module's own file-scanning — a generated profile-diff/radar report older
  than `retention.report_after_days` is deleted, *unless* it's among the
  `retention.keep_latest_reports_per_slug` most recently generated in its own group (see
  `classify_report` for what "group" means per report shape).

Both halves are dry-run by default (report what *would* be deleted, delete nothing) and, when
actually applying, write a pre-delete export of everything about to be removed first — since a
deleted row/file cannot be re-queried afterward to build that record retroactively.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .storage import Storage

PROFILE_DIFF_DIR = Path("data/profile-diff")
RADAR_DIR = Path("data/radar")
CLEANUP_EXPORT_DIR = Path("data/cleanup-exports")

# Two generated timestamp shapes coexist on disk: the current local-timezone-readable one
# (diff_profile.py's `_report_timestamp`) and the earlier bare-UTC one it replaced. Both are
# genuinely script-generated, neither is hand-named, so both are recognized here — see
# docs/retention-cleanup-plan.md section 3.5/Q6.
_TIMESTAMP = r"(?:\d{4}-\d{2}-\d{2}-T-\d{2}-\d{2}-\d{2}|\d{8}T\d{6})"
_DATE = r"\d{4}-\d{2}-\d{2}"

_PROFILE_DIFF_RE = re.compile(rf"^{_TIMESTAMP}\.html$")
_ARCHIVE_DIFF_RE = re.compile(rf"^archive-(?P<slug>.+)_{_DATE}-{_TIMESTAMP}\.html$")
_RADAR_RE = re.compile(rf"^(?P<slug>.+)_{_DATE}\.html$")

# The group key for a plain diff_profile.py report, which has no slug of its own (it diffs the
# profile against a baseline, not any one archive) — confirmed in scope for the same "keep
# latest N" floor as slugged reports (docs/retention-cleanup-plan.md Q2).
_NO_SLUG_GROUP = "(profile-diff)"


@dataclass
class ReportFile:
    path: Path
    kind: str  # "profile_diff" | "archive_diff" | "radar"
    group: str  # a slug, or _NO_SLUG_GROUP for a plain profile_diff report
    mtime: float


def classify_report(path: Path, *, role: str) -> ReportFile | None:
    """Matches `path` against exactly the filename shapes `diff_profile.py`/
    `refilter_archive.py`/`render_radar.py` themselves generate for the given `role`
    (`"profile-diff"` or `"radar"` — the directory's purpose, not a specific location, so this
    is testable against any tmp directory rather than only the real `data/` paths). Returns
    None for anything else — including a hand-named file sitting in the same directory
    (confirmed live: `data/profile-diff/soft_exclude_terms_removed_2026-09-05.html` predates
    this feature and must never be swept up by an age-based glob, see
    docs/retention-cleanup-plan.md section 3.5). Never guess a file's provenance from its age or
    location alone."""
    name = path.name
    if role == "profile-diff":
        archive_match = _ARCHIVE_DIFF_RE.match(name)
        if archive_match:
            return ReportFile(path, "archive_diff", archive_match.group("slug"), path.stat().st_mtime)
        if _PROFILE_DIFF_RE.match(name):
            return ReportFile(path, "profile_diff", _NO_SLUG_GROUP, path.stat().st_mtime)
        return None
    if role == "radar":
        radar_match = _RADAR_RE.match(name)
        if radar_match:
            return ReportFile(path, "radar", radar_match.group("slug"), path.stat().st_mtime)
        return None
    raise ValueError(f"unknown role: {role!r}")


def scan_reports(
    *, profile_diff_dir: Path = PROFILE_DIFF_DIR, radar_dir: Path = RADAR_DIR
) -> list[ReportFile]:
    files: list[ReportFile] = []
    for directory, role in ((profile_diff_dir, "profile-diff"), (radar_dir, "radar")):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file():
                continue
            classified = classify_report(path, role=role)
            if classified:
                files.append(classified)
    return files


def select_reports_to_delete(
    files: list[ReportFile], *, cutoff: datetime, keep_latest_per_group: int
) -> list[ReportFile]:
    """Groups by (kind, group) — a plain profile_diff report, an archive_diff slug, and a radar
    slug are each their own independent series (docs/retention-cleanup-plan.md section 3.4) —
    sorts each newest-first by mtime, protects the first `keep_latest_per_group` unconditionally,
    and only deletes the remainder if it's also older than `cutoff`. A file protected by the
    keep-latest floor is never deleted regardless of age; a file within the age cutoff but
    outside the floor's window is never deleted either — both conditions gate independently."""
    groups: dict[tuple[str, str], list[ReportFile]] = defaultdict(list)
    for report in files:
        groups[(report.kind, report.group)].append(report)
    cutoff_ts = cutoff.timestamp()
    to_delete: list[ReportFile] = []
    for group_files in groups.values():
        group_files.sort(key=lambda r: r.mtime, reverse=True)
        candidates = group_files[keep_latest_per_group:]
        to_delete.extend(r for r in candidates if r.mtime < cutoff_ts)
    return to_delete


@dataclass
class CleanupResult:
    closed_jobs_eligible: int = 0
    closed_jobs_deleted: int = 0
    assessments_deleted: int = 0
    job_feedback_deleted: int = 0
    reports_eligible: list[Path] = field(default_factory=list)
    reports_deleted: list[Path] = field(default_factory=list)
    export_path: Path | None = None
    db_size_before: int | None = None
    db_size_after: int | None = None
    applied: bool = False


def _write_export(payload: dict[str, Any], *, now: datetime, export_dir: Path) -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(
        json.dumps(payload, indent=2, default=str, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def run_cleanup(
    settings: Settings,
    *,
    apply: bool,
    jobs_only: bool = False,
    reports_only: bool = False,
    vacuum: bool = True,
    write_export: bool = True,
    now: datetime | None = None,
    profile_diff_dir: Path = PROFILE_DIFF_DIR,
    radar_dir: Path = RADAR_DIR,
    export_dir: Path = CLEANUP_EXPORT_DIR,
) -> CleanupResult:
    """Dry-run by default (`apply=False`): reports what qualifies, deletes nothing. With
    `apply=True`: writes a pre-delete export (unless `write_export=False`) of exactly what's
    about to be removed, deletes it, and VACUUMs (unless `vacuum=False`) if any jobs were
    deleted. `jobs_only`/`reports_only` are mutually exclusive scopes; passing neither runs
    both halves."""
    now = now or datetime.now(UTC)
    result = CleanupResult()
    do_jobs = not reports_only
    do_reports = not jobs_only

    export_payload: dict[str, Any] = {"cleaned_at": now.isoformat(), "applied": apply}

    if do_jobs:
        job_cutoff = now - _days(settings.retention.closed_job_after_days)
        with Storage(settings.database_path) as storage:
            eligible = storage.find_stale_closed_jobs(job_cutoff)
            result.closed_jobs_eligible = len(eligible)
            if apply and eligible:
                result.db_size_before = _db_size(settings.database_path)
                deleted = storage.delete_closed_jobs(job_cutoff)
                export_payload["deleted_jobs"] = deleted["jobs"]
                export_payload["deleted_assessments"] = deleted["assessments"]
                export_payload["deleted_job_feedback"] = deleted["job_feedback"]
                result.closed_jobs_deleted = len(deleted["jobs"])
                result.assessments_deleted = len(deleted["assessments"])
                result.job_feedback_deleted = len(deleted["job_feedback"])
                if vacuum:
                    storage.vacuum()
                result.db_size_after = _db_size(settings.database_path)

    if do_reports:
        report_cutoff = now - _days(settings.retention.report_after_days)
        eligible_reports = select_reports_to_delete(
            scan_reports(profile_diff_dir=profile_diff_dir, radar_dir=radar_dir),
            cutoff=report_cutoff,
            keep_latest_per_group=settings.retention.keep_latest_reports_per_slug,
        )
        result.reports_eligible = [r.path for r in eligible_reports]
        if apply and eligible_reports:
            export_payload["deleted_reports"] = [str(r.path) for r in eligible_reports]
            for report in eligible_reports:
                report.path.unlink(missing_ok=True)
            result.reports_deleted = result.reports_eligible

    something_deleted = (
        result.closed_jobs_deleted or result.assessments_deleted
        or result.job_feedback_deleted or result.reports_deleted
    )
    if apply and write_export and something_deleted:
        result.export_path = _write_export(export_payload, now=now, export_dir=export_dir)
    result.applied = apply
    return result


def _days(count: int) -> timedelta:
    return timedelta(days=count)


def _db_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0
