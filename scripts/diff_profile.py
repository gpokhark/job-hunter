#!/usr/bin/env python3
"""Preview the effect of editing `candidate_profile.yaml`'s filtering fields — before you
actually save the change. See docs/profile-diff-plan.md for the full design.

Compares two profiles (either two saved YAML files, the real on-disk profile plus a hypothetical
in-memory `--add`/`--remove` patch never written back, or — with no flags at all — the real
on-disk profile against a tracked baseline snapshot, see "Check mode" below) against every stored,
`us_eligible`, recency-passing job in SQLite, using the exact same `evaluate_prefilter` the real
pipeline uses. Reports which postings would newly become candidates ("gained"), which would newly
stop being candidates ("lost"), and how many are unaffected in each direction ("retained" /
"still excluded").

This proves candidate *eligibility against stored postings as of this evaluation* — it does not
guarantee the next live search returns the same jobs (one may have closed, or its stored
description may be stale). Read-only with respect to candidate_profile.yaml and the job database —
never writes to either. No --apply in this version — see docs/profile-diff-plan.md section 7 for
why (that reasoning is specifically about writing *edits* into the file; check mode below only
ever copies the file's current content verbatim into its own tracked snapshot, never parses and
re-serializes it, so none of that reasoning applies there).

Check mode (no --before/--after/--add/--remove given): compares the current on-disk profile
against data/candidate_profile.snapshot.yaml — a plain-copy record of "the profile as of the last
time a baseline was accepted." This is what makes the tool answer "what changed since I last
looked," regardless of *how* the file changed — a manual edit in your editor and a change applied
by a skill both flow through the exact same on-disk file, so this mode can't tell (and doesn't
need to) which one happened. Check mode only ever *shows* the diff — it never advances the
baseline itself. Advancing is a separate, explicit --accept-baseline run, so nothing is ever
silently treated as reviewed; the snapshot it replaces is kept at
data/candidate_profile.snapshot.prev.yaml — one level of rollback via --rollback-baseline. The
very first check-mode run has no snapshot yet, so it bootstraps one from the current profile and
reports nothing to compare (there's nothing to have shown a diff against yet, so this one step
doesn't need a separate confirmation).

Usage:
    uv run python scripts/diff_profile.py                                   # check mode: show only
    uv run python scripts/diff_profile.py --accept-baseline                 # confirm what check mode showed
    uv run python scripts/diff_profile.py --rollback-baseline
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

_PROFILE_PATH = Path("config/candidate_profile.yaml")
# Check mode's tracked "last looked at this" state — a verbatim copy of the profile file's
# text, not a re-serialized/parsed round-trip, so it can never touch the real file's comments
# or formatting. Lives under data/ (already entirely git-ignored, see .gitignore) alongside
# every other run-generated artifact — never committed, same as candidate_profile.yaml itself.
_SNAPSHOT_PATH = Path("data/candidate_profile.snapshot.yaml")
_SNAPSHOT_PREV_PATH = Path("data/candidate_profile.snapshot.prev.yaml")


def _advance_baseline(profile_path: Path) -> None:
    """Record profile_path's current content as the new check-mode baseline, keeping exactly
    one prior generation for --rollback-baseline. A plain text copy, never a YAML parse/dump —
    see this module's docstring for why that distinction matters."""
    _SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _SNAPSHOT_PATH.exists():
        _SNAPSHOT_PREV_PATH.write_text(_SNAPSHOT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    _SNAPSHOT_PATH.write_text(profile_path.read_text(encoding="utf-8"), encoding="utf-8")


def _rollback_baseline() -> bool:
    """Swap the current baseline snapshot with the one it most recently replaced. A true swap
    (not a one-way restore) so running this twice in a row is a no-op, not a second undo.
    Returns False if there's nothing to roll back to."""
    if not _SNAPSHOT_PREV_PATH.exists():
        return False
    prev_content = _SNAPSHOT_PREV_PATH.read_text(encoding="utf-8")
    current_content = _SNAPSHOT_PATH.read_text(encoding="utf-8") if _SNAPSHOT_PATH.exists() else None
    _SNAPSHOT_PATH.write_text(prev_content, encoding="utf-8")
    if current_content is not None:
        _SNAPSHOT_PREV_PATH.write_text(current_content, encoding="utf-8")
    return True

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


@dataclass
class FieldTermDiff:
    """Which specific terms in one filtering field changed between the two profiles being
    compared — the actual words from candidate_profile.yaml, not just the field name, so a
    reviewer can see what's driving a Lost/Gained verdict without opening the YAML file
    separately. `unchanged` still carries the field's other terms for context (a gained/lost
    job may have matched on one of those instead of the actual edit)."""

    field: str
    added: list[str]
    removed: list[str]
    unchanged: list[str]


def _field_term_diffs(before: CandidateProfile, after: CandidateProfile) -> list[FieldTermDiff]:
    """Per-field term diff for every filtering field, in the same fixed order used
    everywhere else in this tool (`_FILTER_FIELDS`) — case-insensitive comparison (matching
    `apply_edits`/`evaluate_prefilter`'s own case-insensitive term matching), but the
    originally-cased term is what's shown."""
    diffs = []
    for field in _FILTER_FIELDS:
        before_by_lower = {term.lower(): term for term in getattr(before, field)}
        after_by_lower = {term.lower(): term for term in getattr(after, field)}
        diffs.append(
            FieldTermDiff(
                field=field,
                added=[
                    after_by_lower[key] for key in after_by_lower if key not in before_by_lower
                ],
                removed=[
                    before_by_lower[key] for key in before_by_lower if key not in after_by_lower
                ],
                unchanged=[
                    after_by_lower[key] for key in after_by_lower if key in before_by_lower
                ],
            )
        )
    return diffs


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
    field_term_diffs: list[FieldTermDiff]
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
        field_term_diffs=_field_term_diffs(before, after),
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


def _field_term_diff_str(diff: FieldTermDiff) -> str:
    parts = [*sorted(diff.unchanged), *(f"+{term}" for term in sorted(diff.added)), *(f"-{term}" for term in sorted(diff.removed))]
    return ", ".join(parts) if parts else "(empty)"


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
    print("\nProfile terms (+added, -removed by this comparison; the rest unchanged):")
    for diff in result.field_term_diffs:
        print(f"  {diff.field}: {_field_term_diff_str(diff)}")
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


_HTML_TEMPLATE = """<!DOCTYPE html>
<meta charset="utf-8">
<title>__TITLE__</title>
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
  .terms-field { margin: 14px 0; }
  .terms-field-name { font-weight: 600; font-size: 13px; font-family: ui-monospace, monospace; color: #6B7480; margin-bottom: 4px; }
  .term-tag { display: inline-block; padding: 2px 9px; border-radius: 12px; font-size: 12.5px; margin: 2px 4px 2px 0; background: #EEF1F3; color: #14191F; }
  .term-tag-added { background: #E6F4EA; color: #1A7F37; font-weight: 600; }
  .term-tag-removed { background: #FBEAE8; color: #C1443A; text-decoration: line-through; }
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
    <h2>Profile terms</h2>
    <p class="empty">The actual candidate_profile.yaml words behind this comparison — green
      = added, red/struck-through = removed, plain = unchanged (still in effect either way).</p>
    __PROFILE_TERMS__
  </section>
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


def _render_field_terms(diffs: list[FieldTermDiff]) -> str:
    if not any(diff.added or diff.removed or diff.unchanged for diff in diffs):
        return '<p class="empty">No filtering terms configured in either profile.</p>'
    parts = []
    for diff in diffs:
        if not (diff.added or diff.removed or diff.unchanged):
            continue
        tags = "".join(
            f'<span class="term-tag">{_e(term)}</span>' for term in sorted(diff.unchanged)
        )
        tags += "".join(
            f'<span class="term-tag term-tag-added">+ {_e(term)}</span>'
            for term in sorted(diff.added)
        )
        tags += "".join(
            f'<span class="term-tag term-tag-removed">{_e(term)}</span>'
            for term in sorted(diff.removed)
        )
        parts.append(
            f'<div class="terms-field"><div class="terms-field-name">{_e(diff.field)}</div>'
            f"<div>{tags}</div></div>"
        )
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
        .replace("__PROFILE_TERMS__", _render_field_terms(result.field_term_diffs))
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
    parser.add_argument(
        "--accept-baseline", action="store_true",
        help=(
            "commit the current on-disk profile as the new check-mode baseline — the only way "
            "the baseline ever advances; check mode itself only ever shows the diff and never "
            "advances it on its own. Standalone action: takes no other flags, runs no "
            "comparison, keeps the previous baseline at "
            "data/candidate_profile.snapshot.prev.yaml (--rollback-baseline to undo)."
        ),
    )
    parser.add_argument(
        "--rollback-baseline", action="store_true",
        help=(
            "undo the most recent --accept-baseline — swaps "
            "data/candidate_profile.snapshot.yaml with data/candidate_profile.snapshot.prev.yaml. "
            "Standalone action: takes no other flags, runs no comparison."
        ),
    )
    args = parser.parse_args()

    file_pair_mode = args.before is not None or args.after is not None
    convenience_mode = bool(args.add) or bool(args.remove)
    active_modes = [
        name
        for name, flag in (
            ("--before/--after", file_pair_mode),
            ("--add/--remove", convenience_mode),
            ("--accept-baseline", args.accept_baseline),
            ("--rollback-baseline", args.rollback_baseline),
        )
        if flag
    ]
    if len(active_modes) > 1:
        print(f"job-hunter: {' and '.join(active_modes)} are mutually exclusive", file=sys.stderr)
        return 2

    if args.rollback_baseline:
        if _rollback_baseline():
            print(
                f"Rolled back: {_SNAPSHOT_PATH} now holds what was previously at "
                f"{_SNAPSHOT_PREV_PATH} (and vice versa — run again to undo this rollback)."
            )
            return 0
        print(f"job-hunter: nothing to roll back — {_SNAPSHOT_PREV_PATH} does not exist", file=sys.stderr)
        return 2

    if args.accept_baseline:
        if not _PROFILE_PATH.exists():
            print(f"job-hunter: {_PROFILE_PATH} does not exist", file=sys.stderr)
            return 2
        _advance_baseline(_PROFILE_PATH)
        print(
            f"Baseline updated: {_SNAPSHOT_PATH} now matches the current profile. "
            f"Previous baseline kept at {_SNAPSHOT_PREV_PATH} (--rollback-baseline to undo)."
        )
        return 0

    try:
        if file_pair_mode:
            if args.before is None or args.after is None:
                raise ProfileDiffError("both --before and --after are required in file-pair mode")
            before = _load_profile_strict(args.before)
            after = _load_profile_strict(args.after)
        elif convenience_mode:
            before = _load_profile_strict(_PROFILE_PATH)
            after = apply_edits(before, args.add, args.remove)
        else:
            after = _load_profile_strict(_PROFILE_PATH)
            if not _SNAPSHOT_PATH.exists():
                # Nothing to confirm yet — there's no prior state to have shown a diff against,
                # so establishing the very first baseline isn't something to gate on
                # confirmation the way advancing an *existing* one is.
                _advance_baseline(_PROFILE_PATH)
                print(
                    f"No prior baseline found — recording the current profile at "
                    f"{_SNAPSHOT_PATH} as the starting point. Nothing to compare yet; "
                    "future changes (manual edits or skill-applied ones alike) will be "
                    "detected from here on."
                )
                return 0
            before = _load_profile_strict(_SNAPSHOT_PATH)
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

    if not file_pair_mode and not convenience_mode:
        print(
            "\nBaseline NOT updated — this only shows the diff. Run with --accept-baseline to "
            "commit the current profile as the new baseline once you've confirmed this is what "
            "you meant to change."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
