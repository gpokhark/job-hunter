#!/usr/bin/env python3
"""Suggest safe `soft_exclude_terms` candidates from the `job_feedback` table (see
docs/feedback-exclusion-plan.md for the full design). A candidate phrase must repeat across
`--min-support` (default 2) distinct irrelevant-tagged titles before it's shown with confidence;
anything repeating only once is shown separately, explicitly labeled below-confidence — the tool
never overclaims a pattern it hasn't actually observed twice.

Safety property (checked against every candidate, high- or low-confidence): a phrase is only ever
shown if it appears in *zero* titles from the protected set — every job already scored >=50 by
the local LLM, plus every job explicitly labeled "relevant"/"okay" via radar feedback. This is
empirical safety (no collision seen so far), not the structural guarantee strong_relevance_terms
provides at match time (see prefilter.py) — but it's still a real, useful first filter before you
manually decide whether to add anything.

Never writes to candidate_profile.yaml — suggestions only, you approve each one.

Usage:
    uv run python scripts/suggest_exclusions.py
    uv run python scripts/suggest_exclusions.py --min-support 3
    uv run python scripts/suggest_exclusions.py --search data/searches/default_2026-09-05.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from job_hunter.config import load_profile, load_settings
from job_hunter.search_archive import resolve_search_path
from job_hunter.storage import Storage

_WORD = re.compile(r"[a-z]+")
_STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "but", "if", "then", "else", "for", "of", "to", "in", "on", "at", "by", "with", "without", "from", "as", "is", "are"]
)


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
    irrelevant_titles: list[str], protected_titles: list[str], *, min_support: int
) -> tuple[list[tuple[str, int]], list[tuple[str, int]], dict[str, list[str]]]:
    """Returns (high_confidence, below_threshold, examples) — the first two are lists of
    (term, support) pairs, already filtered to zero collisions with protected_titles and
    ranked by support descending; examples maps each surviving term to up to 3 irrelevant
    titles it matched, for display."""
    doc_freq: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for title in irrelevant_titles:
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


def _diff_preview(term: str, *, search_path: Path, strong_relevance_terms: list[str]) -> None:
    search = json.loads(search_path.read_text(encoding="utf-8"))
    excluded: list[dict[str, Any]] = []
    rescued: list[dict[str, Any]] = []
    for candidate in search.get("candidates", []):
        # soft_exclude_terms matches title+department only, not the description — see
        # prefilter.py's passes_prefilter for why (confirmed false positives against real
        # description text otherwise).
        gate_text = f"{candidate.get('title', '')} {candidate.get('department') or ''}".lower()
        if term not in gate_text:
            continue
        if any(t.lower() in gate_text for t in strong_relevance_terms):
            rescued.append(candidate)
        else:
            excluded.append(candidate)

    print(f"    Against {search_path.name}:")
    print(f"      Would exclude {len(excluded)} current candidate(s):")
    for c in excluded:
        print(f"        - {c.get('company', '?')}: {c.get('title', '?')}")
    print(f"      Rescued by strong_relevance_terms ({len(rescued)}):")
    for c in rescued:
        print(f"        - {c.get('company', '?')}: {c.get('title', '?')}")


def _build_title_sets(
    feedback_rows: list[dict[str, Any]], assessment_rows: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Returns (irrelevant_titles, protected_titles). A job with no job_feedback row and a
    score below 50 contributes to neither list — absence of feedback is never itself a
    signal, in either direction. See docs/feedback-exclusion-plan.md section 5.1's untagged-
    job guarantee, which this mirrors on the analysis side.

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-support", type=int, default=2, help="min distinct irrelevant titles a phrase must repeat across (default 2)")
    parser.add_argument("--search", type=Path, default=None, help="archive to diff-preview against (default: newest overall)")
    parser.add_argument("--keyword", default=None, help="resolve --search by keyword instead of a path")
    args = parser.parse_args()

    settings = load_settings()
    profile = load_profile()
    search_path = resolve_search_path(search=args.search, keyword=args.keyword)

    with Storage(settings.database_path) as storage:
        feedback_rows = storage.export_job_feedback()
        assessment_rows = storage.export_assessments()

    irrelevant_titles, protected_titles = _build_title_sets(feedback_rows, assessment_rows)

    if not irrelevant_titles:
        print("No irrelevant-labeled jobs in job_feedback yet — nothing to suggest. "
              "Tag some jobs in a radar report and run scripts/apply_radar_feedback.py first.")
        return 0

    high_confidence, below_threshold, examples = _collect_candidates(
        irrelevant_titles, protected_titles, min_support=args.min_support
    )

    print(
        f"{len(irrelevant_titles)} irrelevant-tagged job(s), "
        f"{len(protected_titles)} protected title(s) (score>=50 or explicitly relevant/okay).\n"
    )

    print(f"=== High-confidence candidates (repeat across >= {args.min_support} irrelevant titles, zero protected collisions) ===")
    if not high_confidence:
        print("  (none yet)")
    for term, support in high_confidence:
        print(f'\n  "{term}" — matches {support} irrelevant title(s):')
        for example in examples[term][:3]:
            print(f"    e.g. {example}")
        _diff_preview(term, search_path=search_path, strong_relevance_terms=profile.strong_relevance_terms)

    print("\n=== Below confidence threshold (single occurrence, zero protected collisions — your manual call) ===")
    if not below_threshold:
        print("  (none)")
    for term, _support in below_threshold:
        print(f'  "{term}"')

    print(
        "\nNothing above was written to candidate_profile.yaml — review and add to "
        "soft_exclude_terms yourself."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
