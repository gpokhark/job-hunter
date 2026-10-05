"""Which files feed the resume/outreach skills, resolved deterministically (spec section 6.1).

The master resume is the newest `config/resume/main_resume_<YYYY-MM-DD>.md`, chosen by the date in
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

RESUME_DIR = Path("config/resume")
PERSONALIZATION_NAME = "personalization.md"

_MASTER = re.compile(r"^main_resume_(\d{4})-(\d{2})-(\d{2})\.md$")
_COVER = re.compile(r"^cover_letter_(\d{4})-(\d{2})-(\d{2})\.md$")
_EVIDENCE = re.compile(r"^Review_Evidence_(\d{4})-(\d{2})-(\d{2})\.md$")

NO_RESUME_MESSAGE = (
    "No master resume found. Add config/resume/main_resume_<YYYY-MM-DD>.md "
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
    """Precedence: explicit path > newest dated file in config/resume/ > profile.resume_path."""
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


#: First token of one line in `config/resume/personalization.example.md`. Someone who copies the
#: example and never edits it would otherwise have its sample rules (page-size defaults, sample
#: employer rules) silently steer their real resumes; while this line survives, the file is withheld.
TEMPLATE_SENTINEL = "TEMPLATE-NOT-CUSTOMIZED"
_TEMPLATE_LINE = re.compile(rf"^{TEMPLATE_SENTINEL}\b", re.MULTILINE)


def personalization_status(root: Path) -> tuple[Path | None, str | None]:
    """(path, warning). `path` is the personalization file to use, or None when there is none or it
    is still the unedited template; `warning` explains the second case so the skills can relay it.
    Matches only at the start of a line, so a real file that merely mentions the word is fine."""
    path = find_personalization(root)
    if path is None:
        return None, None
    if _TEMPLATE_LINE.search(path.read_text(encoding="utf-8", errors="replace")):
        return None, (
            f"{RESUME_DIR / PERSONALIZATION_NAME} still has the {TEMPLATE_SENTINEL} line, so it is "
            "ignored (it is the unedited example). Replace the sample rules with your own and delete "
            "that line."
        )
    return path, None


#: The only `## ` sections the skills read. Anything else (a typo like "## resume generator") would be
#: silently ignored, so `personalization_problems` names it.
KNOWN_SECTIONS = ("all", "resume-generator", "outreach-writer")
#: Text that exists only in `personalization.example.md`. Seen in a real file it means a sample rule
#: was left behind after the template line was deleted.
SAMPLE_MARKERS = ("Acme Corp", "Initech", "ToolA", "ToolB", "Certified Example Architect", "Springfield")

_SECTION_HEADING = re.compile(r"^##(?!#)\s+(.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
# Legal/generic words dropped when matching a company by its short name ("Ford Motor Company" -> "Ford").
_COMPANY_FILLER = re.compile(
    r"\b(inc|corp|corporation|llc|ltd|co|company|motor|motors|group|holdings)\b\.?", re.IGNORECASE
)


def personalization_rules(text: str) -> dict[str, list[str]]:
    """Section name (lowercase, from `## ` headings) -> its bullet rules. `###` sub-headings are
    ignored, indented continuation lines are folded into the bullet above them, and text before the
    first `## ` heading is not a rule."""
    rules: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = _SECTION_HEADING.match(line)
        if heading:
            current = heading.group(1).strip().lower()
            rules.setdefault(current, [])
            continue
        if current is None:
            continue
        bullet = _BULLET.match(line)
        if bullet:
            rules[current].append(bullet.group(1).strip())
        elif line[:1] in (" ", "\t") and line.strip() and rules[current]:
            rules[current][-1] += " " + line.strip()
    return rules


def personalization_problems(text: str) -> list[str]:
    """Non-blocking lint of a personalization file's structure. Empty list = fine."""
    problems: list[str] = []
    sections = personalization_rules(text)
    if not any(name in sections for name in KNOWN_SECTIONS):
        problems.append(
            "no recognized section: add at least one of " + ", ".join(f"'## {n}'" for n in KNOWN_SECTIONS)
        )
    for name in sections:
        if name not in KNOWN_SECTIONS:
            problems.append(
                f"unknown section '## {name}' is ignored (expected: "
                + ", ".join(f"'## {n}'" for n in KNOWN_SECTIONS) + ")"
            )
    for name in KNOWN_SECTIONS:
        for rule in sections.get(name, []):
            problems.extend(_role_tag_problems(rule))
            problems.extend(_abbrev_tag_problems(rule))
    for marker in SAMPLE_MARKERS:
        if marker in text:
            problems.append(f"still contains sample text {marker!r} from the example; replace or delete it")
    return problems


# A rule that only applies to some job titles starts with a tag: `- [role: program manager, TPM] ...`.
_ROLE_TAG = re.compile(r"^\[\s*role\s*:\s*([^\]]*)\]\s*", re.IGNORECASE)
_ROLE_TAG_ANYWHERE = re.compile(r"\[\s*roles?\s*:", re.IGNORECASE)


def _norm(text: str) -> str:
    """Lowercase, punctuation and whitespace collapsed to single spaces, for whole-word matching."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _split_role_tag(rule: str) -> tuple[list[str], str] | None:
    """(phrases, rule text without the tag) for a bullet that starts with a valid `[role: ...]` tag."""
    match = _ROLE_TAG.match(rule)
    if not match:
        return None
    phrases = [p.strip() for p in re.split(r"[,|;]", match.group(1)) if p.strip()]
    return (phrases, rule[match.end():].strip()) if phrases else None


def _role_tag_problems(rule: str) -> list[str]:
    tag = _ROLE_TAG.match(rule)
    if tag and not _split_role_tag(rule):
        return ["an empty '[role: ]' tag lists no job titles, so that rule can never apply"]
    if tag:
        return []
    if _ROLE_TAG_ANYWHERE.search(rule):
        return [
            f"role tag must be at the start of the bullet, written '[role: title, title]': {rule[:60]!r}"
        ]
    return []


def role_rules(text: str, role: str) -> list[dict[str, object]]:
    """Rules in the recognized sections whose leading `[role: a, b]` tag matches the job title `role`
    (each tag phrase is matched as whole words, case-insensitively, so "program manager" matches
    "Technical Program Manager, Hardware" but "tpm" does not match "Attempt"). Deterministic: the
    skills apply a tagged rule only when it is returned here and ignore every other tagged rule."""
    title = _norm(role)
    if not title:
        return []
    sections = personalization_rules(text)
    hits: list[dict[str, object]] = []
    for section in KNOWN_SECTIONS:
        for rule in sections.get(section, []):
            tagged = _split_role_tag(rule)
            if tagged is None:
                continue
            phrases, body = tagged
            if any(re.search(rf"\b{re.escape(_norm(p))}\b", title) for p in phrases if _norm(p)):
                hits.append({"section": section, "roles": phrases, "rule": body})
    return hits


def jd_title(path: Path) -> str | None:
    """The job title of a job-hunter `JD_*.txt` export: its first non-empty line (None if unreadable)."""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip():
                    return line.strip()[:200]
    except OSError:
        return None
    return None


def jd_company(path: Path) -> str | None:
    """The company folder name when the JD lives at data/output/<Company_Name>/..., else None."""
    parent = Path(path).resolve().parent
    return parent.name if parent.parent.name == "output" else None


def company_rules(text: str, company: str) -> list[dict[str, str]]:
    """Rules in the recognized sections whose text names `company` (a deterministic whole-word match on
    the full name and on its short form, e.g. "Ford_Motor_Company" -> "Ford"). A hint for the skills,
    which still read the whole file: this only tells them, without judgment, which rules mention this
    employer. It cannot see rules like "for every other company"."""
    name = re.sub(r"\s+", " ", company.replace("_", " ")).strip()
    if not name:
        return []
    variants = {name.lower()}
    short = re.sub(r"\s+", " ", _COMPANY_FILLER.sub("", name)).strip().lower()
    if len(short) >= 4:
        variants.add(short)
    patterns = [re.compile(rf"\b{re.escape(v)}\b") for v in variants]
    sections = personalization_rules(text)
    hits: list[dict[str, str]] = []
    for section in KNOWN_SECTIONS:
        for rule in sections.get(section, []):
            if any(p.search(rule.lower()) for p in patterns):
                hits.append({"section": section, "rule": rule})
    return hits


# A naming override is a bullet that starts with `[abbrev: SlMot]` followed by the output folder name:
# `- [abbrev: SlMot] Slate_Motors`. It only names files; the skills never treat it as a writing rule.
_ABBREV_TAG = re.compile(r"^\[\s*abbrev\s*:\s*([^\]]*)\]\s*(.*)$", re.IGNORECASE)
_ABBREV_ANYWHERE = re.compile(r"\[\s*abbrev\s*:", re.IGNORECASE)
# Trailing legal words never form an initial ("Acme_Corp_Inc" -> "AC"), unless they are all that is left.
_LEGAL_SUFFIX = {"inc", "incorporated", "corp", "corporation", "llc", "ltd", "co", "the"}


def _abbrev_token(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text)


def _abbrev_tag_problems(rule: str) -> list[str]:
    match = _ABBREV_TAG.match(rule)
    if match and _abbrev_token(match.group(1)) and _norm(match.group(2)):
        return []
    if match or _ABBREV_ANYWHERE.search(rule):
        return [
            "a company abbreviation must be written '[abbrev: SHORT] Company_Name' at the start of "
            f"the bullet, so it is ignored: {rule[:60]!r}"
        ]
    return []


def _abbrev_overrides(text: str) -> dict[str, str]:
    """normalized company name -> abbreviation, from `[abbrev: X] Company_Name` bullets in the
    recognized sections (first one wins)."""
    found: dict[str, str] = {}
    for section, rules in personalization_rules(text).items():
        if section not in KNOWN_SECTIONS:
            continue
        for rule in rules:
            match = _ABBREV_TAG.match(rule)
            if not match:
                continue
            abbrev, name = _abbrev_token(match.group(1)), _norm(match.group(2))
            if abbrev and name:
                found.setdefault(name, abbrev)
    return found


def company_abbrev(company: str, personalization_text: str | None = None) -> str:
    """Short, filename-safe tag for an output folder name such as "Ford_Motor_Company" -> "FMC".
    A `[abbrev: X] Company_Name` bullet in personalization.md wins. Otherwise: two or more words give
    their initials in capitals ("Honda_Research_Institute" -> "HRI", "General_Motors" -> "GM"; legal
    suffixes such as Inc/Corp/LLC are skipped), and a single word is kept whole ("Apple"). Deterministic,
    so a CV, letter and email for one employer always share the tag. Returns "" for an empty name."""
    key = _norm(company)
    if not key:
        return ""
    if personalization_text:
        override = _abbrev_overrides(personalization_text).get(key)
        if override:
            return override
    words = [w for w in re.split(r"[^A-Za-z0-9]+", company) if w]
    kept = [w for w in words if w.lower() not in _LEGAL_SUFFIX] or words
    if len(kept) == 1:
        return kept[0]
    return "".join(w[0] for w in kept).upper()


def find_cover_sample(root: Path) -> Path | None:
    found = _newest_dated(root / RESUME_DIR, _COVER)
    return found.resolve() if found else None


def find_review_evidence(root: Path) -> Path | None:
    found = _newest_dated(root / RESUME_DIR, _EVIDENCE)
    return found.resolve() if found else None
