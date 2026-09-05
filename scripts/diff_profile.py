#!/usr/bin/env python3
"""Preview the effect of editing `candidate_profile.yaml`'s filtering fields — before you
actually save the change. See docs/profile-diff-plan.md for the full design.

Compares two profiles (either two saved YAML files, or the real on-disk profile plus a
hypothetical in-memory `--add`/`--remove` patch — never written back) against every stored,
`us_eligible`, recency-passing job in SQLite, using the exact same `evaluate_prefilter` the real
pipeline uses. Reports which postings would newly become candidates ("gained"), which would newly
stop being candidates ("lost"), and how many are unaffected in each direction ("retained" /
"still excluded").

This proves candidate *eligibility against stored postings as of this evaluation* — it does not
guarantee the next live search returns the same jobs (one may have closed, or its stored
description may be stale). Read-only: never writes to candidate_profile.yaml, never writes to the
database. No --apply in this version — see docs/profile-diff-plan.md section 7 for why.

Usage:
    uv run python scripts/diff_profile.py --add soft_exclude_terms:"post silicon"
    uv run python scripts/diff_profile.py --remove target_domains:"validation"
    uv run python scripts/diff_profile.py --add exclude_terms:"cybersecurity" --remove exclude_terms:"fullstack"
    uv run python scripts/diff_profile.py --before /tmp/profile_v1.yaml --after config/candidate_profile.yaml
    uv run python scripts/diff_profile.py --add strong_relevance_terms:"perception" --keyword ADAS
"""

from __future__ import annotations

import argparse
import html
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from job_hunter.config import CandidateProfile, load_settings
from job_hunter.models import Assessment, Job
from job_hunter.prefilter import PrefilterDecision, evaluate_prefilter, passes_recency
from job_hunter.storage import Storage

_FILTER_FIELDS = (
    "target_domains",
    "target_title_terms",
    "exclude_title_terms",
    "exclude_terms",
    "soft_exclude_terms",
    "strong_relevance_terms",
)

# jobs.canonical_url -> Job.url is the one required rename; every other column already lines
# up with a Job field by name. Explicit allowlist, not **row, so a schema column that isn't a
# Job field (status, missing_count) can never silently leak in or cause a validation surprise.
_JOB_COLUMNS = (
    "source_key", "company", "job_id", "source_platform", "title",
    "location_raw", "city", "state", "country", "work_arrangement",
    "us_eligible", "location_confidence", "location_evidence",
    "visa_sponsorship", "sponsorship_evidence", "department",
    "employment_type", "posted_at", "description", "salary_min",
    "salary_max", "salary_currency", "content_hash", "first_seen_at",
    "last_seen_at",
)


class ProfileDiffError(Exception):
    """Any user-facing configuration/input problem — caught once in main(), never a traceback."""


def _load_profile_strict(path: Path) -> CandidateProfile:
    """Unlike config.py's load_profile(), never falls back to the example profile — a typo'd
    path here must fail loudly, not silently compare against the wrong file."""
    if not path.exists():
        raise ProfileDiffError(f"{path} does not exist (no fallback to the example profile for this tool)")
    content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return CandidateProfile.model_validate(content)


def _parse_field_term(spec: str, *, flag: str) -> tuple[str, str]:
    if ":" not in spec:
        raise ProfileDiffError(f"{flag} {spec!r} must be formatted as field:term")
    field, term = spec.split(":", 1)
    field, term = field.strip(), term.strip()
    if field not in _FILTER_FIELDS:
        raise ProfileDiffError(
            f"{flag} {spec!r}: {field!r} is not a patchable field "
            f"(allowed: {', '.join(_FILTER_FIELDS)})"
        )
    if not term:
        raise ProfileDiffError(f"{flag} {spec!r}: term must not be blank")
    return field, term


def apply_edits(before: CandidateProfile, adds: list[str], removes: list[str]) -> CandidateProfile:
    """Builds the hypothetical "after" profile in memory — before is never mutated, and nothing
    here is ever written back to disk."""
    add_specs = [_parse_field_term(spec, flag="--add") for spec in adds]
    remove_specs = [_parse_field_term(spec, flag="--remove") for spec in removes]

    add_keys = {(field, term.lower()) for field, term in add_specs}
    remove_keys = {(field, term.lower()) for field, term in remove_specs}
    conflicts = add_keys & remove_keys
    if conflicts:
        raise ProfileDiffError(f"--add and --remove target the same field+term: {sorted(conflicts)}")

    after_data = before.model_dump()
    for field, term in add_specs:
        current = after_data[field]
        if any(existing.lower() == term.lower() for existing in current):
            print(f"job-hunter: --add {field}:{term!r} already present, no change", file=sys.stderr)
            continue
        after_data[field] = [*current, term]
    for field, term in remove_specs:
        current = after_data[field]
        if not any(existing.lower() == term.lower() for existing in current):
            raise ProfileDiffError(f"--remove {field}:{term!r}: term not found in {field}")
        after_data[field] = [existing for existing in current if existing.lower() != term.lower()]
    return CandidateProfile.model_validate(after_data)


def _non_filter_field_diffs(before: CandidateProfile, after: CandidateProfile) -> list[str]:
    notes = []
    for field in CandidateProfile.model_fields:
        if field in _FILTER_FIELDS:
            continue
        before_value, after_value = getattr(before, field), getattr(after, field)
        if before_value != after_value:
            notes.append(f"{field} {before_value!r} -> {after_value!r}")
    return notes


def _read_only_jobs(database_path: Path) -> list[sqlite3.Row]:
    """A genuinely read-only connection — Storage.__init__() unconditionally runs
    CREATE TABLE IF NOT EXISTS/_migrate()/commit() on open, which is idempotent and harmless to
    existing data but not an actual read-only guarantee, and inconsistent with a tool whose whole
    premise is "changes nothing." """
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM jobs WHERE status='active' AND us_eligible=1").fetchall()
    finally:
        conn.close()


def _row_to_job(row: sqlite3.Row) -> Job:
    data = {column: row[column] for column in _JOB_COLUMNS}
    data["url"] = row["canonical_url"]
    return Job(**data)


@dataclass
class ChangedJob:
    job: Job
    before: PrefilterDecision
    after: PrefilterDecision


@dataclass
class DiffResult:
    evaluated_at: datetime
    max_age_days: int
    source_scope: list[str]
    total_considered: int
    retained: int
    still_excluded: int
    gained: list[ChangedJob]
    lost: list[ChangedJob]
    positive_gate_unrestricted: bool
    non_filter_diffs: list[str]
    assessments: dict[tuple[str, str], Assessment]
    feedback_labels: dict[tuple[str, str], str]

    def assessment_note(self, job: Job) -> str:
        assessment = self.assessments.get((job.source_key, job.job_id))
        if assessment is None:
            return "no assessment on record"
        if assessment.content_hash != job.content_hash:
            return f"has a stale prior assessment (score {assessment.score}, no longer valid for current content)"
        return f"has a valid prior assessment (score {assessment.score})"

    def feedback_label(self, job: Job) -> str | None:
        return self.feedback_labels.get((job.source_key, job.job_id))


def compute_diff(
    *,
    before: CandidateProfile,
    after: CandidateProfile,
    database_path: Path,
    max_age_days: int,
    keywords: list[str] | None,
    now: datetime | None = None,
) -> DiffResult:
    now = now or datetime.now(UTC)
    rows = _read_only_jobs(database_path)
    jobs = [job for row in rows if passes_recency(job := _row_to_job(row), max_age_days, now=now)]

    retained = still_excluded = 0
    gained: list[ChangedJob] = []
    lost: list[ChangedJob] = []
    for job in jobs:
        before_decision = evaluate_prefilter(job, before, keywords=keywords)
        after_decision = evaluate_prefilter(job, after, keywords=keywords)
        if before_decision.passes and after_decision.passes:
            retained += 1
        elif not before_decision.passes and not after_decision.passes:
            still_excluded += 1
        elif after_decision.passes:
            gained.append(ChangedJob(job, before_decision, after_decision))
        else:
            lost.append(ChangedJob(job, before_decision, after_decision))

    with Storage(database_path) as storage:
        assessments = storage.all_assessments()
        feedback_labels = {
            (row["source_key"], row["job_id"]): row["label"] for row in storage.export_job_feedback()
        }

    positive_after = [*after.target_title_terms, *after.target_domains]
    positive_gate_unrestricted = not keywords and not positive_after

    return DiffResult(
        evaluated_at=now,
        max_age_days=max_age_days,
        source_scope=sorted({job.source_key for job in jobs}),
        total_considered=len(jobs),
        retained=retained,
        still_excluded=still_excluded,
        gained=gained,
        lost=lost,
        positive_gate_unrestricted=positive_gate_unrestricted,
        non_filter_diffs=_non_filter_field_diffs(before, after),
        assessments=assessments,
        feedback_labels=feedback_labels,
    )


def _decision_str(decision: PrefilterDecision) -> str:
    text = decision.rule.value
    if decision.term:
        text += f" ({decision.term!r})"
    if decision.rescued_by:
        text += f", rescued_by={decision.rescued_by!r}"
    return text


def print_summary(result: DiffResult, *, keywords: list[str] | None) -> None:
    print(
        f"Evaluated at {result.evaluated_at.isoformat()} | recency cutoff: {result.max_age_days} days"
        f" | {result.total_considered} stored postings considered"
    )
    print(f"Source scope: {', '.join(result.source_scope) or '(none)'}")
    if keywords:
        print(
            f"--keyword {','.join(keywords)} given — target_domains/target_title_terms are NOT "
            "evaluated in keyword mode (same as job-hunter search --keyword)"
        )
    for note in result.non_filter_diffs:
        print(f"Also differs (not part of this comparison): {note}")
    if result.positive_gate_unrestricted:
        print(
            "WARNING: the positive gate is unrestricted after this change — no "
            "target_domains/target_title_terms remain. Every other check still applies, but "
            "nothing gates on domain relevance."
        )
    print()
    print(
        f"Retained: {result.retained} | Still excluded: {result.still_excluded} | "
        f"Gained: {len(result.gained)} | Lost: {len(result.lost)}"
    )
    if result.lost:
        print("\n=== LOST (would newly stop being candidates) ===")
        for item in result.lost:
            label = result.feedback_label(item.job)
            flag = f"  *** TAGGED {label.upper()} — CHECK THIS ***" if label in ("relevant", "okay") else ""
            print(f"  {item.job.company}: {item.job.title}{flag}")
            print(f"    before: {_decision_str(item.before)}")
            print(f"    after:  {_decision_str(item.after)}")
            print(f"    {result.assessment_note(item.job)} | last seen {item.job.last_seen_at}")
    if result.gained:
        print("\n=== GAINED (would newly become candidates) ===")
        for item in result.gained:
            print(f"  {item.job.company}: {item.job.title}")
            print(f"    before: {_decision_str(item.before)}")
            print(f"    after:  {_decision_str(item.after)}")
            print(f"    {result.assessment_note(item.job)} | last seen {item.job.last_seen_at}")


_HTML_TEMPLATE = """<title>__TITLE__</title>
<style>
  body { font-family: system-ui, sans-serif; background: #F3F6F7; color: #14191F; margin: 0; }
  main { max-width: 900px; margin: 0 auto; padding: 40px 24px 80px; }
  h1 { font-size: 28px; }
  .stats { display: flex; gap: 12px; margin: 20px 0; flex-wrap: wrap; }
  .stat { background: #fff; border: 1px solid #DCE3E7; border-radius: 4px; padding: 12px 18px; }
  .stat-value { font-size: 22px; font-weight: 700; display: block; }
  .stat-label { font-size: 12px; color: #6B7480; text-transform: uppercase; }
  .warning { background: #FBEAE8; border: 1px solid #C1443A; color: #C1443A; padding: 12px 16px; border-radius: 4px; margin: 16px 0; }
  .flag { background: #FBEAE8; border: 1px solid #C1443A; color: #C1443A; padding: 2px 8px; border-radius: 3px; font-size: 12px; margin-left: 8px; }
  section { margin: 32px 0; }
  .row { background: #fff; border: 1px solid #DCE3E7; border-radius: 4px; padding: 12px 16px; margin-bottom: 8px; }
  .row-title { font-weight: 600; }
  .row-company { color: #6B7480; font-size: 13px; }
  .row-meta { font-size: 12.5px; color: #6B7480; margin-top: 6px; }
  .empty { color: #6B7480; font-style: italic; }
</style>
<main>
  <h1>__TITLE__</h1>
  <p>Evaluated __EVALUATED_AT__ &middot; recency cutoff __MAX_AGE_DAYS__ days &middot; __TOTAL_CONSIDERED__ stored postings considered</p>
  __WARNING__
  <div class="stats">
    <div class="stat"><span class="stat-value">__RETAINED__</span><span class="stat-label">Retained</span></div>
    <div class="stat"><span class="stat-value">__STILL_EXCLUDED__</span><span class="stat-label">Still excluded</span></div>
    <div class="stat"><span class="stat-value">__GAINED_COUNT__</span><span class="stat-label">Gained</span></div>
    <div class="stat"><span class="stat-value">__LOST_COUNT__</span><span class="stat-label">Lost</span></div>
  </div>
  <section>
    <h2>Lost</h2>
    __LOST_ROWS__
  </section>
  <section>
    <h2>Gained</h2>
    __GAINED_ROWS__
  </section>
</main>
"""


def _e(text: str) -> str:
    return html.escape(text or "")


def _render_rows(items: list[ChangedJob], result: DiffResult, *, empty_message: str) -> str:
    if not items:
        return f'<p class="empty">{_e(empty_message)}</p>'
    parts = []
    for item in items:
        label = result.feedback_label(item.job)
        flag = f'<span class="flag">TAGGED {_e(label.upper())}</span>' if label in ("relevant", "okay") else ""
        parts.append(f"""
        <div class="row">
          <div class="row-title">{_e(item.job.title)}{flag}</div>
          <div class="row-company">{_e(item.job.company)}</div>
          <div class="row-meta">before: {_e(_decision_str(item.before))} &middot; after: {_e(_decision_str(item.after))}</div>
          <div class="row-meta">{_e(result.assessment_note(item.job))} &middot; last seen {_e(str(item.job.last_seen_at))}</div>
        </div>""")
    return "".join(parts)


def render_html(result: DiffResult, output_path: Path, *, title: str) -> None:
    warning = (
        '<div class="warning">The positive gate is unrestricted after this change — no '
        "target_domains/target_title_terms remain.</div>"
        if result.positive_gate_unrestricted
        else ""
    )
    out = (
        _HTML_TEMPLATE.replace("__TITLE__", _e(title))
        .replace("__EVALUATED_AT__", _e(result.evaluated_at.isoformat()))
        .replace("__MAX_AGE_DAYS__", str(result.max_age_days))
        .replace("__TOTAL_CONSIDERED__", str(result.total_considered))
        .replace("__WARNING__", warning)
        .replace("__RETAINED__", str(result.retained))
        .replace("__STILL_EXCLUDED__", str(result.still_excluded))
        .replace("__GAINED_COUNT__", str(len(result.gained)))
        .replace("__LOST_COUNT__", str(len(result.lost)))
        .replace("__LOST_ROWS__", _render_rows(result.lost, result, empty_message="Nothing lost."))
        .replace("__GAINED_ROWS__", _render_rows(result.gained, result, empty_message="Nothing gained."))
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(out, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", type=Path, help="a saved profile YAML (file-pair mode)")
    parser.add_argument("--after", type=Path, help="a saved profile YAML (file-pair mode)")
    parser.add_argument(
        "--add", action="append", default=[], metavar="field:term",
        help="hypothetically add a term to a filtering field (repeatable)",
    )
    parser.add_argument(
        "--remove", action="append", default=[], metavar="field:term",
        help="hypothetically remove a term from a filtering field (repeatable)",
    )
    parser.add_argument(
        "--keyword", default=None,
        help="replaces target_domains/target_title_terms for this comparison, same as "
             "job-hunter search --keyword — NOT a narrowing of them",
    )
    parser.add_argument("--output", type=Path, default=None, help="HTML output path")
    args = parser.parse_args()

    file_pair_mode = args.before is not None or args.after is not None
    convenience_mode = bool(args.add) or bool(args.remove)
    if file_pair_mode and convenience_mode:
        print("job-hunter: --before/--after and --add/--remove are mutually exclusive", file=sys.stderr)
        return 2
    if not file_pair_mode and not convenience_mode:
        print("job-hunter: specify either --before/--after or --add/--remove", file=sys.stderr)
        return 2

    try:
        if file_pair_mode:
            if args.before is None or args.after is None:
                raise ProfileDiffError("both --before and --after are required in file-pair mode")
            before = _load_profile_strict(args.before)
            after = _load_profile_strict(args.after)
        else:
            before = _load_profile_strict(Path("config/candidate_profile.yaml"))
            after = apply_edits(before, args.add, args.remove)
    except ProfileDiffError as exc:
        print(f"job-hunter: {exc}", file=sys.stderr)
        return 2

    keywords = [term.strip() for term in args.keyword.split(",") if term.strip()] if args.keyword else None

    settings = load_settings()
    result = compute_diff(
        before=before,
        after=after,
        database_path=settings.database_path,
        max_age_days=settings.search.max_posting_age_days,
        keywords=keywords,
    )
    print_summary(result, keywords=keywords)

    output_path = args.output or Path("data/profile-diff") / f"{result.evaluated_at.strftime('%Y%m%dT%H%M%S')}.html"
    render_html(result, output_path, title="Candidate Profile Diff")
    print(f"\nWrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
