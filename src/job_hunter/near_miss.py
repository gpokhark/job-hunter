"""Periodic near-miss discovery: jobs the positive-term gate rejected (`no_positive_match`) whose
descriptions still contain several of the profile's strong-relevance terms, plus vocabulary hints
and a department-coverage table.

This is a *human-only scouting aid* in the spirit of docs/broad-match-plan.md (which concluded
description similarity is unreliable as a filter but good for keyword discovery). Near-misses are
never LLM-scored, never added to `candidates`, never merged into the radar, and nothing here
changes the gate. Pure functions over `raw_active_jobs()`; report writing lives in Task 6."""

from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .active_pool import raw_active_jobs
from .config import CandidateProfile
from .models import Job, PrefilterRule
from .prefilter import evaluate_prefilter, passes_recency
from .vocabulary import phrase_gain, title_phrases

#: Strong terms too generic to count toward a near-miss (kept in the profile for the soft-exclude
#: rescue, but "vehicle"/"driving" alone say nothing about a role). Override with --ignore-term.
GENERIC_STRONG_TERMS = frozenset(
    {"vehicle", "behavior", "behaviour", "driving", "chassis", "camera", "radar"}
)


def html_to_text(text: str) -> str:
    text = re.sub(r"(?i)<\s*(?:br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _paragraphs(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", p).strip() for p in html_to_text(text).split("\n") if p.strip()]


def strip_boilerplate(
    jobs: list[Job], *, threshold: float = 0.3, min_postings: int = 5, min_chars: int = 40
) -> dict[tuple[str, str], str]:
    """Description text per `(source_key, job_id)` with per-source boilerplate paragraphs removed:
    a paragraph of >= `min_chars` characters recurring in at least `threshold` of that source's
    postings (and at least twice) is company copy, not role content. Sources with fewer than
    `min_postings` jobs are left untouched (too little evidence to call anything boilerplate)."""
    by_source: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        by_source[job.source_key].append(job)
    cleaned: dict[tuple[str, str], str] = {}
    for source, group in by_source.items():
        paragraphs = {job.job_id: _paragraphs(job.description or "") for job in group}
        counts: Counter[str] = Counter()
        for items in paragraphs.values():
            counts.update({p.lower() for p in items if len(p) >= min_chars})
        boiler: set[str] = set()
        if len(group) >= min_postings:
            cutoff = threshold * len(group)
            boiler = {p for p, n in counts.items() if n >= 2 and n >= cutoff}
        for job in group:
            keep = [p for p in paragraphs[job.job_id] if p.lower() not in boiler]
            cleaned[(source, job.job_id)] = "\n".join(keep)
    return cleaned


def _term_regex(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])")


def matched_terms(text: str, terms: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in terms if _term_regex(t).search(low)]


@dataclass(frozen=True)
class NearMiss:
    source_key: str
    job_id: str
    company: str
    title: str
    url: str
    department: str | None
    posted_at: datetime | None
    first_seen_at: datetime
    terms: tuple[str, ...]
    occurrences: int

    @property
    def score(self) -> int:
        return len(self.terms)


@dataclass(frozen=True)
class SourceCoverage:
    source_key: str
    total: int
    empty_department: int


@dataclass(frozen=True)
class VocabularyHint:
    term: str
    near_miss_jobs: int
    gain: int
    samples: tuple[str, ...]


@dataclass
class ScanResult:
    rows: list[NearMiss]
    pool_size: int
    eligible_recent: int
    empty_department: int
    coverage: list[SourceCoverage] = field(default_factory=list)
    hints: list[VocabularyHint] = field(default_factory=list)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _vocabulary_hints(
    rows: list[NearMiss], profile: CandidateProfile, pool: list[Job], *, top: int = 15, min_jobs: int = 2
) -> list[VocabularyHint]:
    frequency: Counter[str] = Counter()
    for row in rows:
        frequency.update(set(title_phrases(row.title)))
    hints: list[VocabularyHint] = []
    for phrase, count in frequency.items():
        if count < min_jobs:
            continue
        # shown only when adding it would admit at least one currently-rejected job (a phrase the
        # profile already covers, or a soft-exclude still rejects, has gain 0)
        gain = phrase_gain(profile, phrase, pool)
        if gain.count == 0:
            continue
        hints.append(VocabularyHint(phrase, count, gain.count, gain.samples))
    hints.sort(key=lambda h: (-h.near_miss_jobs, h.gain, -len(h.term), h.term))
    return hints[:top]


def scan(
    database_path: Path,
    profile: CandidateProfile,
    max_age_days: int,
    *,
    ignore_terms: frozenset[str] = GENERIC_STRONG_TERMS,
    min_terms: int = 3,
    since: datetime | None = None,
    limit: int | None = None,
    now: datetime | None = None,
) -> ScanResult:
    eligible = [j for j in raw_active_jobs(database_path) if passes_recency(j, max_age_days, now=now)]
    pool = [j for j in eligible if evaluate_prefilter(j, profile).rule is PrefilterRule.NO_POSITIVE_MATCH]

    ignored = {t.lower() for t in ignore_terms}
    terms = [t for t in profile.strong_relevance_terms if t.lower() not in ignored]
    cleaned = strip_boilerplate(pool)
    rows: list[NearMiss] = []
    for job in pool:
        text = cleaned[(job.source_key, job.job_id)]
        hits = matched_terms(text, terms)
        if len(hits) < min_terms:
            continue
        if since is not None and _aware(job.first_seen_at) <= _aware(since):
            continue
        low = text.lower()
        occurrences = sum(len(_term_regex(t).findall(low)) for t in hits)
        rows.append(
            NearMiss(
                job.source_key, job.job_id, job.company, job.title, job.url, job.department,
                job.posted_at, job.first_seen_at, tuple(hits), occurrences,
            )
        )
    rows.sort(key=lambda r: (-r.score, -r.occurrences, r.source_key, r.title))
    if limit is not None:
        rows = rows[:limit]

    totals: Counter[str] = Counter(j.source_key for j in eligible)
    empties: Counter[str] = Counter(j.source_key for j in eligible if not (j.department or "").strip())
    coverage = sorted(
        (SourceCoverage(k, totals[k], empties[k]) for k in totals if empties[k]),
        key=lambda c: (-c.empty_department, c.source_key),
    )
    return ScanResult(
        rows=rows,
        pool_size=len(pool),
        eligible_recent=len(eligible),
        empty_department=sum(empties.values()),
        coverage=coverage,
        hints=_vocabulary_hints(rows, profile, pool),
    )
