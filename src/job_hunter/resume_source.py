"""Which files feed the resume/outreach skills, resolved deterministically (spec section 6.1).

The master resume is the newest `data/resume/main_resume_<YYYY-MM-DD>.md`, chosen by the date in
the *filename* — never by mtime, so touching or re-saving an older file can't silently promote it.
The profile's `resume_path` stays only as a fallback so an existing setup keeps working. The same
function serves the local-LLM reviewer, so scoring and resume generation always agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from .config import CandidateProfile

RESUME_DIR = Path("data/resume")
PERSONALIZATION_NAME = "personalization.md"

_MASTER = re.compile(r"^main_resume_(\d{4})-(\d{2})-(\d{2})\.md$")
_COVER = re.compile(r"^cover_letter_(\d{4})-(\d{2})-(\d{2})\.md$")
_EVIDENCE = re.compile(r"^Review_Evidence_(\d{4})-(\d{2})-(\d{2})\.md$")

NO_RESUME_MESSAGE = (
    "No master resume found. Add data/resume/main_resume_<YYYY-MM-DD>.md "
    "(the newest date wins) or pass --resume PATH."
)


@dataclass(frozen=True)
class ResolvedResume:
    path: Path
    source: Literal["explicit", "dated", "profile"]


def _newest_dated(directory: Path, pattern: re.Pattern[str]) -> Path | None:
    if not directory.is_dir():
        return None
    best_key: tuple[date, str] | None = None
    best_path: Path | None = None
    for entry in directory.iterdir():
        match = pattern.match(entry.name)
        if not match or not entry.is_file():
            continue
        try:
            when = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:  # e.g. 2026-13-45 matches the shape but is not a date
            continue
        key = (when, entry.name)
        if best_key is None or key > best_key:
            best_key, best_path = key, entry
    return best_path


def resolve_master_resume(
    root: Path, *, explicit: Path | None = None, profile: CandidateProfile | None = None
) -> ResolvedResume:
    """Precedence: explicit path > newest dated file in data/resume/ > profile.resume_path."""
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"--resume file not found: {path}")
        return ResolvedResume(path.resolve(), "explicit")
    found = _newest_dated(root / RESUME_DIR, _MASTER)
    if found is not None:
        return ResolvedResume(found.resolve(), "dated")
    if profile is not None and profile.resume_path is not None:
        fallback = profile.resume_path if profile.resume_path.is_absolute() else root / profile.resume_path
        if fallback.is_file():
            return ResolvedResume(fallback.resolve(), "profile")
    raise FileNotFoundError(NO_RESUME_MESSAGE)


def is_example(path: Path) -> bool:
    """The repo ships `config/resume.example.md`; generation must never use placeholder text."""
    return ".example." in path.name


def find_personalization(root: Path) -> Path | None:
    path = root / RESUME_DIR / PERSONALIZATION_NAME
    return path.resolve() if path.is_file() else None


def find_cover_sample(root: Path) -> Path | None:
    found = _newest_dated(root / RESUME_DIR, _COVER)
    return found.resolve() if found else None


def find_review_evidence(root: Path) -> Path | None:
    found = _newest_dated(root / RESUME_DIR, _EVIDENCE)
    return found.resolve() if found else None
