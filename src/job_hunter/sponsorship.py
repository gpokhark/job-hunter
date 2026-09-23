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

Two further not-available categories cover postings that never say the word "sponsorship"
is unavailable but state something that means the same thing in practice — confirmed live
on MBRDNA (Lever) and Daimler Truck North America (Workday), and written generically since
this boilerplate recurs across unrelated companies/roles, not just these two:

- **Export-control licensing.** ITAR/EAR boilerplate ("subject to the International Traffic
  in Arms Regulations (ITAR)... may be required to obtain an export license or authorization
  in accordance with United States law") is the standard way employers disclose that a role
  is restricted to "U.S. persons" (citizens/permanent residents/protected individuals) absent
  extra licensing — in practice a no-sponsorship role even though "sponsorship" never
  appears. Matched narrowly: the ITAR/EAR citation *and* a nearby "an export license/
  authorization is/may be required" clause must both be present, not just any ITAR/EAR
  mention (plenty of postings disclose export-control exposure without restricting hiring on
  it).
- **Sponsorship restricted to existing visa holders.** "Visa sponsorship will only be open to
  current [Company] employees working under an existing U.S. [Company] Visa" reads as
  available at a glance but is only a same-employer visa *transfer* for people already
  sponsored — not available to a new external applicant, which is what this field describes.
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
        # Export-control licensing implies a "U.S. persons only" restriction in practice —
        # see module docstring. Requires both the regulation citation *and* a nearby
        # "export license/authorization required" clause, not a bare ITAR/EAR mention.
        r"(?:international traffic in arms regulations|export administration regulations)"
        r"\s*\((?:itar|ear)\).{0,400}?(?:may be required to obtain|must obtain|require[sd]?)"
        r".{0,20}?(?:an )?export (?:licen[sc]e|authorization)",
        # Sponsorship offered only to existing employees already holding a company visa —
        # not available to an external applicant. See module docstring.
        r"sponsorship (?:will only|is only) be (?:open|available|offered) to "
        r"(?:current|existing|internal)\b.{0,60}\bemployees\b",
        r"employees?\s+(?:currently\s+)?working under an existing\b.{0,40}\bvisa\b",
        r"sponsorship (?:is|will be)?\s*(?:limited|restricted) to (?:current|existing|internal)"
        r"\b.{0,60}\bemployees\b",
    ]
]

_AVAILABLE = [
    re.compile(pattern, re.I)
    for pattern in [
        r"visa sponsorship is available",
        r"visa sponsorship available",
        r"sponsorship may be available",
        r"sponsorship\s*:\s*yes\b",
        r"will sponsor (?:a )?(?:visas?|applicants|candidates|employees)",
        r"do sponsor (?:a )?(?:visas?|applicants|candidates|employees)",
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
