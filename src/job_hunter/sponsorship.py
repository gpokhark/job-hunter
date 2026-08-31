"""Deterministic, evidence-based detection of a posting's explicit stance on visa
sponsorship — purely informational, never a filter (see `models.SponsorshipStatus`'s
docstring). This module never excludes or ranks a job; `passes_prefilter` never calls it.

A bare "sponsor" substring match is not good enough — confirmed directly against live
postings, "sponsor" routinely appears in unrelated contexts: PACCAR's "sponsor Key-Op
program participants" (an internal mentorship program), Hyundai's "liaison... and
sponsors for training initiatives" (event sponsors), Valeo's "access to our sponsored
sports hall" (an employee perk) and "Sponsorowane prywatne ubezpieczenie zdrowotne"
(Polish for "sponsored private health insurance"). So this matches a curated list of
specific, high-confidence phrases instead — the same real, boilerplate legal language
companies actually paste into postings (Toyota's "does not offer support or sponsorship
of job applicants for employment-based visas", Honda's "Sponsorship for employment visa
status for these positions is unavailable", Nissan's structured "Sponsorship: No" field,
Valeo's "not eligible for visa sponsorship") — and defaults to UNMENTIONED whenever none
of them match, exactly like `location.py`'s "never guess" philosophy for ambiguous cases.

Not-available phrases are checked first: they're the dominant, most consistent signal in
practice, and checking them first means a sentence like "does not offer... sponsorship"
can never be miscounted by a looser "offer sponsorship" positive pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import SponsorshipStatus

_NOT_AVAILABLE = [
    re.compile(pattern, re.I)
    for pattern in [
        r"will not be sponsored for work authorization",
        r"does not offer support or sponsorship",
        r"not (?:be )?eligible for.{0,40}sponsorship",
        r"sponsorship for employment visa status.{0,60}unavailable",
        r"sponsorship\s*:\s*no\b",
        r"without (?:the need for )?(?:a )?(?:work )?(?:visa )?sponsorship",
        r"will not sponsor",
        r"does not sponsor",
        r"we do not sponsor",
        r"no visa sponsorship",
        r"not (?:currently )?(?:provide|offer).{0,40}sponsorship",
        r"unable to sponsor",
        r"sponsorship.{0,40}is not available",
        r"not authorized to sponsor",
        r"not require.{0,40}sponsorship",
        r"not need.{0,30}sponsorship",
    ]
]

_AVAILABLE = [
    re.compile(pattern, re.I)
    for pattern in [
        r"visa sponsorship is available",
        r"visa sponsorship available",
        r"sponsorship may be available",
        r"sponsorship\s*:\s*yes\b",
        r"will sponsor (?:visas|applicants|candidates|employees)",
        r"eligible for (?:visa )?sponsorship",
        r"(?:offers?|provides?) (?:visa )?sponsorship",
    ]
]


_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class SponsorshipDecision:
    status: SponsorshipStatus
    evidence: str | None = None


def _first_match(text: str, patterns: list[re.Pattern[str]]) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return " ".join(match.group(0).split())
    return None


def evaluate_sponsorship(description: str | None) -> SponsorshipDecision:
    # Descriptions are raw HTML — a tag sitting between two words of a phrase (e.g.
    # Nissan's "<b>Sponsorship:</b> No") would otherwise silently defeat a plain-text
    # pattern; strip tags first so phrase matching sees the same text a human reader does.
    # Stripping a tag that sat *inside* a phrase (e.g. "is <strong>not</strong>
    # available") leaves double spaces behind — confirmed as a real live miss on Ford's
    # own "Visa sponsorship is <strong>not</strong> available" wording — so whitespace
    # is also collapsed, since every pattern below assumes single-space-separated words.
    text = " ".join(_TAG.sub(" ", description or "").split())
    not_available = _first_match(text, _NOT_AVAILABLE)
    if not_available:
        return SponsorshipDecision(SponsorshipStatus.NOT_AVAILABLE, not_available)
    available = _first_match(text, _AVAILABLE)
    if available:
        return SponsorshipDecision(SponsorshipStatus.AVAILABLE, available)
    return SponsorshipDecision(SponsorshipStatus.UNMENTIONED)
