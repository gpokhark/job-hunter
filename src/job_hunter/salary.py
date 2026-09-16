"""Deterministic detection of an explicit salary/pay figure mentioned in a posting —
the same evidence-based pattern as `sponsorship.py`: purely informational, never a
filter (`passes_prefilter` never calls this), defaulting to no evidence whenever
nothing matches rather than guessing.

Unlike sponsorship's curated phrase list, a single regex anchored on a real range (two
numbers straddling a "-"/"–"/"—"/"to", at least the first one carrying a "$") is enough
here and was validated directly against live descriptions (confirmed against real GM,
Honda, Ford, and Torc Robotics postings, and re-checked across the full stored job pool
before shipping — ~6,700 of ~30,000 active jobs match, and a random sample turned up no
false positive that wasn't a genuine compensation range). A bare single dollar figure is
deliberately NOT matched on its own — confirmed live as a real false-positive risk the
same way bare "sponsor" was for sponsorship.py: Ford's own benefits boilerplate mentions
"Life Insurance of $3,000" and "Accidental Death and Dismemberment of $1,500", neither a
salary. Every live salary mention actually found (GM's "$76,100.00 to $114,300.00",
Honda's hourly "$37.13 - $42.43", Ford's "$72,480-121,440" — note the second number
carries no "$" at all, which is why it's optional on that side only — and Torc's
Greenhouse-templated "$177,300 — $212,800 USD") is already two-sided, so requiring a
second number costs no real recall while ruling out that whole class of false positive.

One more real trap found the same way: dozens of Caterpillar/Nissan Workday postings for
hourly/union roles carry an unfilled compensation-template field that renders as a
literal "$0.00 - $0.00" — not a real range, and never surfaced as one."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

_TAG = re.compile(r"<[^>]+>")

_RANGE = re.compile(
    r"\$[\d,]+(?:\.\d{2})?\s*(?:-|–|—|to)\s*\$?[\d,]+(?:\.\d{2})?"
    r"(?:\s*(?:/hour|/hr|per hour|hourly|/year|/yr|per year|annually|annual|USD))?",
    re.I,
)
_NUMBER = re.compile(r"[\d,]+(?:\.\d{2})?")


@dataclass(frozen=True)
class SalaryDecision:
    evidence: str | None = None
    min_value: float | None = None
    max_value: float | None = None


def evaluate_salary(description: str | None) -> SalaryDecision:
    # Descriptions are raw HTML, same reasoning as sponsorship.py: strip tags first so a
    # tag sitting between the two numbers (or before/after) can't defeat the pattern.
    # Also unescape entities — confirmed live on Torc Robotics' Greenhouse postings,
    # whose pay range renders as two <span> tags separated by a third holding a literal
    # "&mdash;" (not a real "—" character) — an em/en-dash entity is otherwise invisible
    # to the "-"/"–"/"—" separator alternatives below.
    text = " ".join(_TAG.sub(" ", html.unescape(description or "")).split())
    match = _RANGE.search(text)
    if not match:
        return SalaryDecision()
    numbers = _NUMBER.findall(match.group(0))
    if len(numbers) < 2:
        return SalaryDecision()
    low = float(numbers[0].replace(",", ""))
    high = float(numbers[1].replace(",", ""))
    if low == 0 and high == 0:
        return SalaryDecision()
    return SalaryDecision(" ".join(match.group(0).split()), low, high)
