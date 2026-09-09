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

# Reused from the sibling script rather than reimplemented: same hybrid-beats-remote text
# precedence and same "only the two actionable states get a tag" rule as the radar report uses,
# so a job tagged Hybrid/No Sponsorship here and in job-radar's report never disagrees.
from render_radar import _arrangement_tag, _sponsorship_tag  # noqa: E402

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

# Report filenames — and every other human-facing timestamp this tool prints or renders — are
# shown in the device's local timezone (not UTC, which `evaluated_at`/`last_seen_at` themselves
# stay in internally for storage/comparison) purely for human readability. This used to be
# hardcoded to America/New_York for the filename only, which was wrong for anyone running this
# on a machine set to a different timezone; `.astimezone()` with no argument resolves whatever
# the real system-local timezone is, correctly reflecting DST too, and needs no `tzdata` package.
def _local(moment: datetime) -> datetime:
    """Convert an internally-UTC instant to the device's local timezone for display. Never use
    this on a `posted_at` — many of those are date-only values normalized to UTC midnight, and
    converting them to local time would shift the displayed calendar day backward."""
    return moment.astimezone()


def _report_timestamp(moment: datetime) -> str:
    """`YYYY-MM-DD-T-HH-MM-SS` in the device's local timezone, 24-hour clock — e.g.
    `2026-09-06-T-21-36-05`. Distinct from `evaluated_at`'s own ISO-8601 UTC timestamp
    internally; this (and every other human-facing display of it) converts to local time."""
    return _local(moment).strftime("%Y-%m-%d-T-%H-%M-%S")


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

# Same default freshness window as render_radar.py's --new-days, so a job tagged [New] in one
# report is tagged [New] in the other.
_NEW_DAYS = 10


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
    undated_new_days: int
    undated_stale_days: int
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

    def assessment_score(self, job: Job) -> int | None:
        """Raw score for the feedback-button's data-score attribute — mirrors what a radar
        report's row carries, or None for a job never scored (the button still works; export
        just carries a null score, same as apply_radar_feedback.py already tolerates)."""
        assessment = self.assessments.get((job.source_key, job.job_id))
        return assessment.score if assessment else None


def compute_diff(
    *,
    before: CandidateProfile,
    after: CandidateProfile,
    database_path: Path,
    max_age_days: int,
    keywords: list[str] | None,
    undated_new_days: int = 15,
    undated_stale_days: int = 45,
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
        undated_new_days=undated_new_days,
        undated_stale_days=undated_stale_days,
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
        f"Evaluated at {_local(result.evaluated_at).isoformat()} | recency cutoff: {result.max_age_days} days"
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
            print(f"    {result.assessment_note(item.job)} | last seen {_local(item.job.last_seen_at)}")
    if result.gained:
        print("\n=== GAINED (would newly become candidates) ===")
        for item in result.gained:
            print(f"  {item.job.company}: {item.job.title}")
            print(f"    before: {_decision_str(item.before)}")
            print(f"    after:  {_decision_str(item.after)}")
            print(f"    {result.assessment_note(item.job)} | last seen {_local(item.job.last_seen_at)}")


_HTML_TEMPLATE = """<!DOCTYPE html>
<meta charset="utf-8">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --paper: #F3F6F7;
    --ink: #14191F;
    --ink-soft: #4B5560;
    --surface: #FFFFFF;
    --line: #DCE3E7;
    --accent: #0C7F91;
    --accent-soft: #E4F1F3;
    --tier-exceptional: #1D9A66;
    --tier-exceptional-soft: #E4F5EC;
    --danger: #C1443A;
    --danger-soft: #FBEAE8;
    --arrangement-remote: #2D6FB0;
    --arrangement-remote-soft: #E4EEF8;
    --arrangement-hybrid: #7B5CAE;
    --arrangement-hybrid-soft: #EFE8F7;
    --muted: #6B7480;
    --shadow: 0 1px 2px rgba(20, 25, 31, 0.06);
  }

  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --paper: #10151A;
      --ink: #E9EDEF;
      --ink-soft: #A6B0B8;
      --surface: #171E24;
      --line: #2A333A;
      --accent: #3FC1D4;
      --accent-soft: #17323A;
      --tier-exceptional: #3FCC8C;
      --tier-exceptional-soft: #163829;
      --danger: #E2695E;
      --danger-soft: #3A1F1C;
      --arrangement-remote: #6FB1EE;
      --arrangement-remote-soft: #17293A;
      --arrangement-hybrid: #C0A3EA;
      --arrangement-hybrid-soft: #2A2038;
      --muted: #8A95A0;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.4);
    }
  }

  :root[data-theme="dark"] {
    --paper: #10151A;
    --ink: #E9EDEF;
    --ink-soft: #A6B0B8;
    --surface: #171E24;
    --line: #2A333A;
    --accent: #3FC1D4;
    --accent-soft: #17323A;
    --tier-exceptional: #3FCC8C;
    --tier-exceptional-soft: #163829;
    --danger: #E2695E;
    --danger-soft: #3A1F1C;
    --arrangement-remote: #6FB1EE;
    --arrangement-remote-soft: #17293A;
    --arrangement-hybrid: #C0A3EA;
    --arrangement-hybrid-soft: #2A2038;
    --muted: #8A95A0;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.4);
  }

  * { box-sizing: border-box; }

  body { font-family: "IBM Plex Sans", system-ui, sans-serif; background: var(--paper); color: var(--ink); margin: 0; line-height: 1.5; }
  main { max-width: 900px; margin: 0 auto; padding: 40px 24px 80px; }
  h1 { font-family: "Big Shoulders Display", system-ui, sans-serif; font-weight: 700; font-size: 32px; letter-spacing: -0.005em; margin: 0 0 4px; }
  h2 { font-family: "Big Shoulders Display", system-ui, sans-serif; font-weight: 700; font-size: 24px; margin: 0 0 6px; }
  main > p { color: var(--muted); font-size: 13px; }
  .stats { display: flex; gap: 12px; margin: 20px 0; flex-wrap: wrap; }
  .stat { background: var(--surface); border: 1px solid var(--line); border-radius: 4px; padding: 12px 18px; box-shadow: var(--shadow); }
  .stat-value { font-family: "IBM Plex Mono", monospace; font-size: 22px; font-weight: 700; display: block; font-variant-numeric: tabular-nums; }
  .stat-label { font-size: 11px; letter-spacing: 0.04em; color: var(--muted); text-transform: uppercase; }
  .warning { background: var(--danger-soft); border: 1px solid var(--danger); color: var(--danger); padding: 12px 16px; border-radius: 4px; margin: 16px 0; font-size: 13.5px; }
  .flag { background: var(--danger-soft); border: 1px solid var(--danger); color: var(--danger); padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; margin-left: 8px; vertical-align: middle; }
  section { margin: 32px 0; }
  .rows { display: flex; flex-direction: column; gap: 8px; }
  .row { background: var(--surface); border: 1px solid var(--line); border-radius: 3px; box-shadow: var(--shadow); padding: 12px 16px; }
  .row-top { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  .job { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1 1 auto; }
  .job-title { font-weight: 600; font-size: 15px; }
  .job-company { font-size: 13px; color: var(--muted); }
  .job-date { font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--muted); white-space: nowrap; }
  .tags { display: flex; gap: 6px; flex-wrap: wrap; }
  .tag { font-family: "IBM Plex Mono", monospace; font-size: 10px; font-weight: 600; letter-spacing: 0.03em; padding: 3px 7px; border-radius: 2px; white-space: nowrap; }
  .tag-sponsor-yes { background: var(--tier-exceptional-soft); color: var(--tier-exceptional); }
  .tag-sponsor-no { background: var(--danger-soft); color: var(--danger); }
  .tag-long-standing { background: var(--line); color: var(--muted); }
  .tag-remote { background: var(--arrangement-remote-soft); color: var(--arrangement-remote); }
  .tag-hybrid { background: var(--arrangement-hybrid-soft); color: var(--arrangement-hybrid); }
  .tag-new { display: inline-block; background: var(--accent); color: var(--surface); font-weight: 700; }
  @media (prefers-reduced-motion: no-preference) {
    .tag-new { animation: pulse-new 1.4s ease-in-out infinite; }
  }
  @keyframes pulse-new { 0%, 100% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.12); opacity: 0.72; } }
  .apply-link { font-family: "IBM Plex Mono", monospace; font-size: 12.5px; font-weight: 500; color: var(--accent); text-decoration: none; border-bottom: 1px solid transparent; white-space: nowrap; }
  .apply-link:hover, .apply-link:focus-visible { border-bottom-color: var(--accent); }
  .row-meta { font-size: 12.5px; color: var(--muted); margin-top: 8px; }
  .empty { color: var(--muted); font-style: italic; }
  .terms-field { margin: 14px 0; }
  .terms-field-name { font-weight: 600; font-size: 13px; font-family: "IBM Plex Mono", monospace; color: var(--muted); margin-bottom: 4px; }
  .term-tag { display: inline-block; padding: 2px 9px; border-radius: 12px; font-size: 12.5px; margin: 2px 4px 2px 0; background: var(--accent-soft); color: var(--ink); }
  .term-tag-added { background: var(--tier-exceptional-soft); color: var(--tier-exceptional); font-weight: 600; }
  .term-tag-removed { background: var(--danger-soft); color: var(--danger); text-decoration: line-through; }

  /* --- Feedback capture (same mechanism/schema as job-radar's report — see
     docs/feedback-exclusion-plan.md) --- */
  .row-feedback { margin-top: 8px; }
  .feedback-buttons { display: flex; gap: 4px; align-items: center; }
  .fb-btn { font-size: 13px; line-height: 1; padding: 4px 6px; border-radius: 3px; border: 1px solid var(--line); background: var(--surface); cursor: pointer; }
  .fb-btn:hover { border-color: var(--accent); }
  .fb-btn.fb-active { border-color: var(--accent); background: var(--accent-soft); }
  .fb-btn.fb-irrelevant.fb-active { border-color: var(--danger); background: var(--danger-soft); }
  .row.fb-tagged { border-left: 3px solid var(--accent); }
  .feedback-export { position: fixed; bottom: 24px; right: 24px; z-index: 10; }
  #feedback-export-btn {
    font-family: "IBM Plex Mono", monospace; font-size: 13px; font-weight: 600; padding: 12px 20px;
    border-radius: 999px; border: 1px solid var(--line); background: var(--accent); color: var(--surface);
    cursor: pointer; box-shadow: 0 2px 10px rgba(20, 25, 31, 0.2);
  }
  #feedback-export-btn:disabled { background: var(--surface); color: var(--muted); cursor: not-allowed; box-shadow: var(--shadow); opacity: 0.7; }
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
    <div class="rows">__LOST_ROWS__</div>
  </section>
  <section>
    <h2>Gained</h2>
    <div class="rows">__GAINED_ROWS__</div>
  </section>
</main>

<div class="feedback-export">
  <button type="button" id="feedback-export-btn" disabled>Export Feedback (0)</button>
</div>

<script>
(function () {
  // Same click-to-tag / export-on-demand mechanism as job-radar's report (see
  // docs/feedback-exclusion-plan.md) — exports to the identical radar-feedback-*.json
  // filename shape so scripts/apply_radar_feedback.py ingests it with no changes, and
  // scripts/suggest_exclusions.py can then draw on it regardless of which report a job's
  // feedback came from.
  var STORAGE_KEY = 'job-hunter-feedback:__DIFF_STEM__';
  var feedback = {};

  function loadPersisted() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (e) {
      return {};
    }
  }

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(feedback));
    } catch (e) {
      // Private browsing / storage disabled / quota exceeded — feedback still works for
      // this page load via the in-memory object and the Export button.
    }
  }

  function exportButton() {
    return document.getElementById('feedback-export-btn');
  }

  function refreshExportButton() {
    var count = Object.keys(feedback).length;
    var btn = exportButton();
    btn.textContent = 'Export Feedback (' + count + ')';
    btn.disabled = count === 0;
  }

  feedback = loadPersisted();

  document.querySelectorAll('.feedback-buttons').forEach(function (group) {
    var buttons = group.querySelectorAll('.fb-btn');
    var key = group.dataset.sourceKey + '|' + group.dataset.jobId;
    var row = group.closest('.row');

    // A job already labeled in the database (from a prior export+apply cycle, possibly via
    // a different report entirely) shows that label by default — localStorage still wins
    // if this browser already has a newer, not-yet-exported choice for the same job.
    if (!feedback[key] && group.dataset.dbLabel) {
      feedback[key] = {
        source_key: group.dataset.sourceKey,
        job_id: group.dataset.jobId,
        company: group.dataset.company,
        title: group.dataset.title,
        department: group.dataset.department || null,
        score: group.dataset.score ? parseInt(group.dataset.score, 10) : null,
        label: group.dataset.dbLabel
      };
    }

    var restored = feedback[key];
    if (restored) {
      buttons.forEach(function (b) {
        if (b.dataset.label === restored.label) b.classList.add('fb-active');
      });
      if (row) row.classList.add('fb-tagged');
    }

    buttons.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var alreadyActive = btn.classList.contains('fb-active');
        buttons.forEach(function (b) { b.classList.remove('fb-active'); });

        if (alreadyActive) {
          delete feedback[key];
          if (row) row.classList.remove('fb-tagged');
        } else {
          btn.classList.add('fb-active');
          feedback[key] = {
            source_key: group.dataset.sourceKey,
            job_id: group.dataset.jobId,
            company: group.dataset.company,
            title: group.dataset.title,
            department: group.dataset.department || null,
            score: group.dataset.score ? parseInt(group.dataset.score, 10) : null,
            label: btn.dataset.label
          };
          if (row) row.classList.add('fb-tagged');
        }
        persist();
        refreshExportButton();
      });
    });
  });

  refreshExportButton();

  exportButton().addEventListener('click', function () {
    var rows = Object.keys(feedback).map(function (k) { return feedback[k]; });
    if (rows.length === 0) {
      return;
    }
    var blob = new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = 'radar-feedback-__DIFF_STEM__.json';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  });
})();
</script>
"""


def _e(text: str) -> str:
    return html.escape(text or "")


def _fmt_posted_date(posted_at: datetime | None, first_seen_at: datetime | None = None) -> str:
    """A real posted_at always wins. Absent one, falls back to "First seen {date}" using
    first_seen_at (when job-hunter's own collector first observed the job) rather than the
    bare "Date unknown" this used to always show — clearly labeled so it's never mistaken for
    the job's actual posting date. first_seen_at is a genuine instant, so it's converted to
    local time for display like every other one (see `_local`)."""
    if posted_at:
        return posted_at.strftime("%b %-d, %Y")
    if first_seen_at:
        return f"First seen {_local(first_seen_at).strftime('%b %-d, %Y')}"
    return "Date unknown"


def _job_tags(job: Job, *, now: datetime, undated_new_days: int, undated_stale_days: int) -> str:
    """[New]/sponsorship/work-arrangement tags — deliberately the same three tags and the
    same rules job-radar's report uses (see _arrangement_tag/_sponsorship_tag), so a job never
    looks tagged differently in the two reports. This report never scores anything, so there's
    no score tier tag ([90+]/[80+]) to show here.

    A job with no posted_at falls back to first_seen_at as a display-only proxy for age —
    never a filter, see config.py's SearchConfig.undated_new_days/undated_stale_days: [New]
    while freshly first-seen, "Long-standing" once first seen a long time ago. This can be
    wrong (a job onboarded from a brand-new source looks "new" regardless of how long it's
    actually been posted, and an evergreen undated listing will eventually get tagged
    long-standing even though it's still genuinely open) — it's a hint for a human reviewer,
    not a claim about the job's real age, which is why it never removes anything from the
    report the way max_posting_age_days can for a job with a real posted_at."""
    tags = ""
    if job.posted_at:
        posted = job.posted_at if job.posted_at.tzinfo else job.posted_at.replace(tzinfo=UTC)
        if (now - posted).days <= _NEW_DAYS:
            tags += '<span class="tag tag-new">New</span>'
    elif job.first_seen_at:
        first_seen = job.first_seen_at if job.first_seen_at.tzinfo else job.first_seen_at.replace(tzinfo=UTC)
        age_days = (now - first_seen).days
        if age_days <= undated_new_days:
            tags += '<span class="tag tag-new">New</span>'
        elif age_days > undated_stale_days:
            tags += '<span class="tag tag-long-standing">Long-standing</span>'
    tags += _sponsorship_tag(job.visa_sponsorship)
    tags += _arrangement_tag(job.work_arrangement)
    return tags


def _render_rows(items: list[ChangedJob], result: DiffResult, *, empty_message: str) -> str:
    if not items:
        return f'<p class="empty">{_e(empty_message)}</p>'
    parts = []
    for item in items:
        job = item.job
        label = result.feedback_label(job)
        flag = f'<span class="flag">TAGGED {_e(label.upper())}</span>' if label in ("relevant", "okay") else ""
        tags = _job_tags(
            job, now=result.evaluated_at,
            undated_new_days=result.undated_new_days, undated_stale_days=result.undated_stale_days,
        )
        date_display = _fmt_posted_date(job.posted_at, job.first_seen_at)
        score = result.assessment_score(job)
        feedback_buttons = f'''<span class="feedback-buttons"
              data-source-key="{_e(job.source_key)}" data-job-id="{_e(job.job_id)}"
              data-company="{_e(job.company)}" data-title="{_e(job.title)}"
              data-department="{_e(job.department)}" data-score="{score if score is not None else ''}"
              data-db-label="{_e(label or '')}">
              <button type="button" class="fb-btn fb-relevant" data-label="relevant" title="Relevant">&#128077;</button>
              <button type="button" class="fb-btn fb-okay" data-label="okay" title="Okay">&#128994;</button>
              <button type="button" class="fb-btn fb-irrelevant" data-label="irrelevant" title="Irrelevant">&#128078;</button>
            </span>'''
        parts.append(f"""
        <div class="row">
          <div class="row-top">
            <span class="tags">{tags}</span>
            <span class="job">
              <span class="job-title">{_e(job.title)}{flag}</span>
              <span class="job-company">{_e(job.company)}</span>
            </span>
            <span class="job-date">{_e(date_display)}</span>
            <a class="apply-link" href="{html.escape(job.url, quote=True)}" target="_blank" rel="noopener">View posting &#8599;</a>
          </div>
          <div class="row-meta">before: {_e(_decision_str(item.before))} &middot; after: {_e(_decision_str(item.after))}</div>
          <div class="row-meta">{_e(result.assessment_note(job))} &middot; last seen {_e(str(_local(job.last_seen_at)))}</div>
          <div class="row-feedback">{feedback_buttons}</div>
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
        .replace("__DIFF_STEM__", _e(output_path.stem))
        .replace("__EVALUATED_AT__", _e(_local(result.evaluated_at).isoformat()))
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
        undated_new_days=settings.search.undated_new_days,
        undated_stale_days=settings.search.undated_stale_days,
        keywords=keywords,
    )
    print_summary(result, keywords=keywords)

    output_path = args.output or Path("data/profile-diff") / f"{_report_timestamp(result.evaluated_at)}.html"
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
