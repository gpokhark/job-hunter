"""Shared helpers for the missed-job diagnostics (`why_missed.py`) and the periodic near-miss scan
(`near_miss.py`): what a job title can contribute as a candidate profile term, and how many
already-rejected jobs a term would admit.

Gains are computed with the real `evaluate_prefilter` on a widened copy of the profile, so the
substring-on-title+department semantics and the soft-exclude rule cannot drift from production.
Nothing here writes anything."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .active_pool import raw_active_jobs
from .config import CandidateProfile
from .models import Job, PrefilterRule
from .prefilter import evaluate_prefilter, passes_recency

#: Tokens too common to justify a profile term on their own (a candidate phrase must contain at
#: least one token that is not in this set and has >= 3 characters).
GENERIC_TITLE_TOKENS = frozenset(
    {
        "senior", "sr", "staff", "principal", "lead", "manager", "engineer", "engineering",
        "specialist", "director", "associate", "analyst", "jr", "junior", "head", "vp",
        "contract", "temporary", "ii", "iii", "iv", "i", "of", "and", "the", "for", "to",
        "in", "at", "with", "a", "an",
    }
)

#: A suggested term admitting more than this many already-rejected jobs is flagged "broad" (shared
#: by `why-missed` suggestions and `near-misses` vocabulary hints).
BROAD_TERM_THRESHOLD = 40

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#./-]*")


def title_phrases(title: str, max_words: int = 3) -> list[str]:
    """Contiguous 1..max_words-word windows of the lower-cased title (ampersands dropped), skipping
    windows made only of generic/short tokens, unique, ordered by window size then position."""
    # trailing "."/"-"/"/" is dropped so "Sr." is the generic "sr", not a distinct 3-char token
    tokens = [t for t in (t.rstrip(".-/") for t in _TOKEN.findall(title.lower().replace("&", " "))) if t]
    seen: set[str] = set()
    phrases: list[str] = []
    for size in range(1, max_words + 1):
        for start in range(len(tokens) - size + 1):
            window = tokens[start : start + size]
            if not any(len(t) >= 3 and t not in GENERIC_TITLE_TOKENS for t in window):
                continue
            phrase = " ".join(window)
            if phrase not in seen:
                seen.add(phrase)
                phrases.append(phrase)
    return phrases


def rejected_pool(
    database_path: Path,
    profile: CandidateProfile,
    max_age_days: int,
    *,
    now: datetime | None = None,
) -> list[Job]:
    """Active, U.S.-eligible, recency-passing jobs the positive-term gate rejected
    (`no_positive_match`) — deliberate rejections (exclude/soft-exclude rules) are not included."""
    return [
        job
        for job in raw_active_jobs(database_path, readonly=True)
        if passes_recency(job, max_age_days, now=now)
        and evaluate_prefilter(job, profile).rule is PrefilterRule.NO_POSITIVE_MATCH
    ]


@dataclass(frozen=True)
class PhraseGain:
    count: int
    samples: tuple[str, ...]


def _gate_text(job: Job) -> str:
    return f"{job.title} {job.department or ''}".lower()


def phrase_gain(
    profile: CandidateProfile, phrase: str, pool: list[Job], *, sample_size: int = 3
) -> PhraseGain:
    """How many jobs in `pool` pass the real gate once `phrase` is added to `target_title_terms`.
    `pool` must be `rejected_pool` output (jobs rejected with `no_positive_match`). Widening only
    adds a positive term, so such a job lacking the phrase in title+department cannot flip to
    admitted and is skipped without evaluating (identical result, just faster). That shortcut is
    exact only for a rejected pool: a job that already passes via an existing term but lacks the
    phrase would be skipped here though a full evaluation would count it."""
    widened = profile.model_copy(
        update={"target_title_terms": [*profile.target_title_terms, phrase]}
    )
    needle = phrase.lower()
    admitted = [
        job for job in pool if needle in _gate_text(job) and evaluate_prefilter(job, widened).passes
    ]
    samples = tuple(f"{job.source_key}: {job.title}" for job in admitted[:sample_size])
    return PhraseGain(len(admitted), samples)
