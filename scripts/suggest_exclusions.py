#!/usr/bin/env python3
"""Suggest `candidate_profile.yaml` changes from the `job_feedback` table (see
docs/feedback-exclusion-plan.md for the original design, and its §13 for this script's
extension beyond `soft_exclude_terms`). Feedback can come from any report that writes the same
radar-feedback-*.json shape — job-radar's report or the profile-diff report's own feedback
buttons, ingested identically by `apply_radar_feedback.py`. This script never knows or cares
which report a label came from.

Every job with feedback is re-evaluated against the CURRENT on-disk profile via the exact same
`evaluate_prefilter` the real pipeline uses, to find out precisely *why* it currently passes or
fails — not by re-deriving a guess from title text. That decision (`rule`/`term`/`rescued_by`)
is what decides which field a suggestion targets:

  - irrelevant, currently a plain positive match         -> soft_exclude_terms   (add)
  - irrelevant, currently rescued by strong_relevance     -> flagged for manual review (no
    a term                                                   auto-suggestion — removing/narrowing
                                                              a strong_relevance_terms word is too
                                                              blunt an instrument to propose blind)
  - relevant/okay, currently soft-excluded (not rescued)  -> strong_relevance_terms (add)
  - relevant/okay, currently no positive match            -> target_domains / target_title_terms
                                                              (add)
  - relevant/okay, currently hard-excluded                -> exclude_title_terms / exclude_terms
                                                              (remove) — high severity: these
                                                              fields have no rescue mechanism at
                                                              all, so a false positive here is
                                                              exactly the failure mode
                                                              docs/CLAUDE.md's filtering
                                                              philosophy warns hardest against.
  - relevant/okay, not us_eligible                        -> not fixable via profile terms
                                                              (location.py), just reported

Every `soft_exclude_terms`/`strong_relevance_terms`/`target_domains`/`target_title_terms`
candidate still carries the same two safety properties as before: it must repeat across
`--min-support` (default 2) distinct titles in its own bucket, and it must collide with *zero*
titles in the opposing bucket (an add-candidate must never also appear in an irrelevant-tagged
title, and vice versa for the historical soft-exclude check). A single-occurrence phrase is shown
separately, explicitly labeled below-confidence, never silently promoted.

Every proposed add/remove is also previewed live against every stored, `us_eligible`,
recency-passing job (the exact same `evaluate_prefilter` sweep `diff_profile.py`'s check mode
runs) — not a bespoke reimplementation of that logic here, the real thing, imported directly.

Never writes to candidate_profile.yaml — suggestions only, you approve each one and make the
edit yourself (or via the job-feedback skill, which does this conversationally).

Usage:
    uv run python scripts/suggest_exclusions.py
    uv run python scripts/suggest_exclusions.py --min-support 3
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Reused rather than reimplemented — same canonical_url->url column mapping and the same real
# evaluate_prefilter sweep diff_profile.py's own check mode runs, not a parallel reimplementation
# of either.
from diff_profile import (  # noqa: E402
    DiffResult,
    _row_to_job,
    apply_edits,
    compute_diff,
)

from job_hunter.config import CandidateProfile, load_profile, load_settings
from job_hunter.models import Job, PrefilterRule
from job_hunter.prefilter import PrefilterDecision, evaluate_prefilter
from job_hunter.storage import Storage

_WORD = re.compile(r"[a-z]+")
_STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "but", "if", "then", "else", "for", "of", "to", "in", "on", "at", "by", "with", "without", "from", "as", "is", "are"]
)

_PROTECTED_LABELS = ("relevant", "okay")


def _tokenize(title: str) -> list[str]:
    return [w for w in _WORD.findall(title.lower()) if w not in _STOPWORDS]


def _ngrams(title: str, sizes: tuple[int, ...] = (1, 2, 3)) -> set[str]:
    words = _tokenize(title)
    grams: set[str] = set()
    for n in sizes:
        for i in range(len(words) - n + 1):
            grams.add(" ".join(words[i : i + n]))
    return grams


def _collect_candidates(
    target_titles: list[str], protected_titles: list[str], *, min_support: int
) -> tuple[list[tuple[str, int]], list[tuple[str, int]], dict[str, list[str]]]:
    """Returns (high_confidence, below_threshold, examples) — the first two are lists of
    (term, support) pairs, already filtered to zero collisions with protected_titles and
    ranked by support descending; examples maps each surviving term to up to 3 target titles
    it matched, for display. Symmetric by design: used both for soft_exclude_terms candidates
    (target=irrelevant titles, protected=good titles) and, in reverse, for
    strong_relevance_terms/target_domains candidates (target=wrongly-excluded good titles,
    protected=irrelevant titles) — same safety shape either direction."""
    doc_freq: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for title in target_titles:
        grams = _ngrams(title)
        doc_freq.update(grams)
        for gram in grams:
            if len(examples[gram]) < 3:
                examples[gram].append(title)

    protected_lower = [t.lower() for t in protected_titles]

    def is_safe(term: str) -> bool:
        return not any(term in protected_title for protected_title in protected_lower)

    high_confidence = []
    below_threshold = []
    for term, support in doc_freq.most_common():
        if not is_safe(term):
            continue
        if support >= min_support:
            high_confidence.append((term, support))
        elif support == 1:
            below_threshold.append((term, support))
    return high_confidence, below_threshold, examples


def _build_title_sets(
    feedback_rows: list[dict[str, Any]], assessment_rows: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Returns (irrelevant_titles, protected_titles) for the soft_exclude_terms bucket only —
    unchanged from the original design. A job with no job_feedback row and a score below 50
    contributes to neither list — absence of feedback is never itself a signal, in either
    direction. See docs/feedback-exclusion-plan.md section 5.1's untagged-job guarantee, which
    this mirrors on the analysis side.

    An explicit "irrelevant" feedback label always overrides that same job's own assessment
    score for this purpose — otherwise a job tagged irrelevant but still sitting at its old
    >=50 score would count as its own protected collision, since the label and the stale
    score disagree about the same job. The whole point of tagging it is to correct the score,
    not compete with it. Confirmed as a real bug against live data: without this, "platform
    architecture" could never be suggested, because every job containing that phrase was
    simultaneously in irrelevant_titles (via feedback) and protected_titles (via its own
    pre-correction score)."""
    label_by_key = {(r["source_key"], r["job_id"]): r["label"] for r in feedback_rows}
    irrelevant_titles = [r["title"] for r in feedback_rows if r["label"] == "irrelevant"]
    protected_titles = [r["title"] for r in feedback_rows if r["label"] in {"relevant", "okay"}]
    protected_titles += [
        r["title"]
        for r in assessment_rows
        if r["score"] >= 50
        and label_by_key.get((r["source_key"], r["job_id"])) != "irrelevant"
    ]
    return irrelevant_titles, protected_titles


@dataclass
class FeedbackDecision:
    """One job_feedback row, joined against its current SQLite record and re-evaluated against
    the current on-disk profile — the fresh `evaluate_prefilter` decision, not a stale one."""

    job: Job
    label: str
    decision: PrefilterDecision


def _load_all_jobs(database_path: Path) -> dict[tuple[str, str], Job]:
    """Every job ever stored, regardless of status/us_eligible/recency — a feedback-tagged job
    may have since closed or aged out, but its profile-decision provenance is still worth
    knowing. Read-only, same connection pattern as diff_profile.py's own storage access."""
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM jobs").fetchall()
    finally:
        conn.close()
    return {(job.source_key, job.job_id): job for row in rows if (job := _row_to_job(row))}


def _feedback_decisions(
    feedback_rows: list[dict[str, Any]],
    jobs_by_key: dict[tuple[str, str], Job],
    profile: CandidateProfile,
) -> list[FeedbackDecision]:
    decisions = []
    for row in feedback_rows:
        job = jobs_by_key.get((row["source_key"], row["job_id"]))
        if job is None:
            continue  # purged from storage entirely — nothing to recompute against
        decisions.append(
            FeedbackDecision(job=job, label=row["label"], decision=evaluate_prefilter(job, profile))
        )
    return decisions


def _preview(
    profile: CandidateProfile, *, add: str | None, remove: str | None, database_path: Path, max_age_days: int
) -> DiffResult:
    after = apply_edits(profile, adds=[add] if add else [], removes=[remove] if remove else [])
    return compute_diff(
        before=profile, after=after, database_path=database_path, max_age_days=max_age_days, keywords=None
    )


def _drop_already_present(
    high_confidence: list[tuple[str, int]], below_threshold: list[tuple[str, int]], *, existing: list[str]
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """A candidate already sitting in the target field is neither a new suggestion nor
    something worth an --add preview (apply_edits would just no-op it with a stderr note) —
    filter it out before it's ever shown, rather than re-suggesting what's already applied."""
    existing_lower = {t.lower() for t in existing}
    return (
        [(t, s) for t, s in high_confidence if t not in existing_lower],
        [(t, s) for t, s in below_threshold if t not in existing_lower],
    )


def _print_preview(result: DiffResult, *, limit: int = 5) -> None:
    print(
        f"    Preview vs. every stored job: retained={result.retained} "
        f"still_excluded={result.still_excluded} gained={len(result.gained)} lost={len(result.lost)}"
    )
    for item in result.gained[:limit]:
        print(f"      + {item.job.company}: {item.job.title}")
    if len(result.gained) > limit:
        print(f"      ... and {len(result.gained) - limit} more gained")
    for item in result.lost[:limit]:
        print(f"      - {item.job.company}: {item.job.title}")
    if len(result.lost) > limit:
        print(f"      ... and {len(result.lost) - limit} more lost")


def _print_soft_exclude_bucket(
    irrelevant_titles: list[str], protected_titles: list[str], *,
    profile: CandidateProfile, database_path: Path, max_age_days: int, min_support: int,
) -> None:
    print(
        f"\n=== soft_exclude_terms candidates (repeat across >= {min_support} irrelevant "
        "titles, zero collisions with a good match) ==="
    )
    if not irrelevant_titles:
        print("  (no irrelevant-labeled feedback yet)")
        return
    high_confidence, below_threshold, examples = _collect_candidates(
        irrelevant_titles, protected_titles, min_support=min_support
    )
    high_confidence, below_threshold = _drop_already_present(
        high_confidence, below_threshold, existing=profile.soft_exclude_terms
    )
    if not high_confidence:
        print("  (none yet)")
    for term, support in high_confidence:
        print(f'\n  ADD "{term}" to soft_exclude_terms — matches {support} irrelevant title(s):')
        for example in examples[term][:3]:
            print(f"    e.g. {example}")
        _print_preview(_preview(profile, add=f"soft_exclude_terms:{term}", remove=None, database_path=database_path, max_age_days=max_age_days))
    if below_threshold:
        print(f"\n  Below confidence threshold (single occurrence, your manual call): {', '.join(t for t, _ in below_threshold)}")


def _print_positive_gate_bucket(
    decisions: list[FeedbackDecision], irrelevant_titles: list[str], *,
    profile: CandidateProfile, database_path: Path, max_age_days: int, min_support: int,
) -> None:
    titles = [
        d.job.title for d in decisions
        if d.label in _PROTECTED_LABELS and d.decision.rule == PrefilterRule.NO_POSITIVE_MATCH
    ]
    print(
        f"\n=== target_domains / target_title_terms candidates (tagged relevant/okay, currently "
        f"no positive match; repeat across >= {min_support} such titles, zero collisions with "
        "an irrelevant-tagged title) ==="
    )
    if not titles:
        print("  (no relevant/okay feedback on a currently-unmatched job yet)")
        return
    high_confidence, below_threshold, examples = _collect_candidates(titles, irrelevant_titles, min_support=min_support)
    high_confidence, below_threshold = _drop_already_present(
        high_confidence, below_threshold, existing=[*profile.target_domains, *profile.target_title_terms]
    )
    if not high_confidence:
        print("  (none yet)")
    for term, support in high_confidence:
        print(f'\n  ADD "{term}" to target_domains (or target_title_terms) — matches {support} title(s):')
        for example in examples[term][:3]:
            print(f"    e.g. {example}")
        _print_preview(_preview(profile, add=f"target_domains:{term}", remove=None, database_path=database_path, max_age_days=max_age_days))
    if below_threshold:
        print(f"\n  Below confidence threshold (single occurrence, your manual call): {', '.join(t for t, _ in below_threshold)}")


def _print_strong_relevance_bucket(
    decisions: list[FeedbackDecision], irrelevant_titles: list[str], *,
    profile: CandidateProfile, database_path: Path, max_age_days: int, min_support: int,
) -> None:
    by_soft_term: dict[str, list[str]] = defaultdict(list)
    for d in decisions:
        if d.label in _PROTECTED_LABELS and d.decision.rule == PrefilterRule.SOFT_EXCLUDED:
            by_soft_term[d.decision.term or "?"].append(d.job.title)

    print(
        f"\n=== strong_relevance_terms candidates (tagged relevant/okay, currently excluded by "
        f"an existing soft_exclude_terms entry with no rescue; repeat across >= {min_support} "
        "such titles, zero collisions with an irrelevant-tagged title) ==="
    )
    if not by_soft_term:
        print("  (no relevant/okay feedback on a currently soft-excluded job yet)")
        return
    for soft_term, titles in by_soft_term.items():
        high_confidence, below_threshold, examples = _collect_candidates(titles, irrelevant_titles, min_support=min_support)
        high_confidence, below_threshold = _drop_already_present(
            high_confidence, below_threshold, existing=profile.strong_relevance_terms
        )
        print(f'\n  Currently excluded via soft_exclude_terms "{soft_term}":')
        if not high_confidence:
            print("    (no safe rescue term found yet)")
        for term, support in high_confidence:
            print(f'\n    ADD "{term}" to strong_relevance_terms — matches {support} title(s):')
            for example in examples[term][:3]:
                print(f"      e.g. {example}")
            _print_preview(_preview(profile, add=f"strong_relevance_terms:{term}", remove=None, database_path=database_path, max_age_days=max_age_days))
        if below_threshold:
            print(f"    Below confidence threshold: {', '.join(t for t, _ in below_threshold)}")


_HARD_EXCLUDE_FIELDS = {
    PrefilterRule.EXCLUDE_TITLE_TERMS: "exclude_title_terms",
    PrefilterRule.EXCLUDE_TERMS: "exclude_terms",
}


def _print_hard_exclude_removal_bucket(
    decisions: list[FeedbackDecision], *, profile: CandidateProfile, database_path: Path, max_age_days: int,
) -> None:
    by_field_term: dict[tuple[str, str], list[str]] = defaultdict(list)
    for d in decisions:
        field = _HARD_EXCLUDE_FIELDS.get(d.decision.rule)
        if d.label in _PROTECTED_LABELS and field and d.decision.term:
            by_field_term[(field, d.decision.term)].append(d.job.title)

    print(
        "\n=== exclude_title_terms / exclude_terms — REMOVAL candidates (tagged relevant/okay, "
        "currently hard-blocked with no rescue mechanism at all — the highest-severity false "
        "positive this tool can find) ==="
    )
    if not by_field_term:
        print("  (none — no relevant/okay feedback on a hard-excluded job)")
        return
    for (field, term), titles in by_field_term.items():
        print(f'\n  *** REMOVE "{term}" from {field} — blocking {len(titles)} job(s) you tagged relevant/okay: ***')
        for title in titles[:5]:
            print(f"    e.g. {title}")
        _print_preview(_preview(profile, add=None, remove=f"{field}:{term}", database_path=database_path, max_age_days=max_age_days))


def _print_rescue_caution_bucket(decisions: list[FeedbackDecision]) -> None:
    by_rescue_term: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for d in decisions:
        if d.label == "irrelevant" and d.decision.passes and d.decision.rescued_by:
            by_rescue_term[d.decision.rescued_by].append((d.job.title, d.decision.term or "?"))

    print(
        "\n=== strong_relevance_terms CAUTION (tagged irrelevant, currently rescued from a "
        "soft_exclude_terms match — no auto-suggestion here, narrowing/removing a "
        "strong_relevance_terms word is too blunt a lever to propose blind; review manually) ==="
    )
    if not by_rescue_term:
        print("  (none)")
        return
    for rescue_term, pairs in by_rescue_term.items():
        print(f'\n  "{rescue_term}" rescued {len(pairs)} job(s) you tagged irrelevant:')
        for title, soft_term in pairs[:5]:
            print(f'    e.g. "{title}" (soft-excluded by "{soft_term}")')


def _print_not_fixable_bucket(decisions: list[FeedbackDecision]) -> None:
    titles = [
        d.job.title for d in decisions
        if d.label in _PROTECTED_LABELS and d.decision.rule == PrefilterRule.NOT_US_ELIGIBLE
    ]
    if not titles:
        return
    print(
        f"\n=== Not fixable via candidate_profile.yaml ({len(titles)} job(s) tagged "
        "relevant/okay but not us_eligible — check location.py's evaluate_location for these) ==="
    )
    for title in titles[:5]:
        print(f"  {title}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-support", type=int, default=2, help="min distinct titles a phrase must repeat across, per bucket (default 2)")
    args = parser.parse_args()

    settings = load_settings()
    profile = load_profile()

    with Storage(settings.database_path) as storage:
        feedback_rows = storage.export_job_feedback()
        assessment_rows = storage.export_assessments()

    if not feedback_rows:
        print("No job_feedback rows yet — nothing to suggest. Tag some jobs (radar report or "
              "profile-diff report) and run scripts/apply_radar_feedback.py first.")
        return 0

    jobs_by_key = _load_all_jobs(settings.database_path)
    decisions = _feedback_decisions(feedback_rows, jobs_by_key, profile)
    irrelevant_titles, protected_titles = _build_title_sets(feedback_rows, assessment_rows)

    n_relevant = sum(1 for r in feedback_rows if r["label"] in _PROTECTED_LABELS)
    n_irrelevant = sum(1 for r in feedback_rows if r["label"] == "irrelevant")
    print(f"{len(feedback_rows)} feedback row(s): {n_relevant} relevant/okay, {n_irrelevant} irrelevant.")
    if len(decisions) < len(feedback_rows):
        print(f"({len(feedback_rows) - len(decisions)} feedback row(s) skipped — job no longer in storage.)")

    max_age_days = settings.search.max_posting_age_days
    _print_soft_exclude_bucket(
        irrelevant_titles, protected_titles, profile=profile,
        database_path=settings.database_path, max_age_days=max_age_days, min_support=args.min_support,
    )
    _print_strong_relevance_bucket(
        decisions, irrelevant_titles, profile=profile,
        database_path=settings.database_path, max_age_days=max_age_days, min_support=args.min_support,
    )
    _print_positive_gate_bucket(
        decisions, irrelevant_titles, profile=profile,
        database_path=settings.database_path, max_age_days=max_age_days, min_support=args.min_support,
    )
    _print_hard_exclude_removal_bucket(
        decisions, profile=profile, database_path=settings.database_path, max_age_days=max_age_days,
    )
    _print_rescue_caution_bucket(decisions)
    _print_not_fixable_bucket(decisions)

    print(
        "\nNothing above was written to candidate_profile.yaml — review and edit it yourself "
        "(or via the job-feedback skill)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
