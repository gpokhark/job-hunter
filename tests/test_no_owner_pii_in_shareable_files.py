"""Local safety net: the contact details in the owner's real `config/candidate_profile.yaml` (name,
email, phone, LinkedIn and GitHub handles) must not appear in any file that could be committed or
shared, i.e. every git-tracked file plus every untracked file that is not git-ignored.

It reads whoever's real profile is on the machine running the tests, so it protects every user of
the repo, not one person. It skips when there is no real profile (a fresh clone, CI) or only the
example placeholders. Failures list file paths and field names only, never the values themselves.

The surname is matched as a whole word (length 5+). If yours is also an ordinary word (say "Baker"),
set JOB_HUNTER_PII_SKIP_SURNAME=1 to skip just that check; the full name, email, phone and handles
are still enforced.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from job_hunter.config import CandidateProfile, _is_placeholder, load_profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "config" / "candidate_profile.yaml"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

_BINARY = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".sqlite3", ".db", ".gz", ".zip", ".woff2"}
_MAX_BYTES = 5_000_000


def _owner_terms(profile: CandidateProfile) -> dict[str, str]:
    """field -> lowercase search term, only for values that look real (not example placeholders)."""
    contact = profile.contact
    terms: dict[str, str] = {}
    name = (contact.name or "").strip()
    if len(name.split()) >= 2 and not _is_placeholder(name):
        terms["name"] = name.lower()
        surname = name.split()[-1].strip(".,").lower()
        if len(surname) >= 5:
            terms["surname"] = surname
    email = (contact.email or "").strip()
    if email and not _is_placeholder(email):
        terms["email"] = email.lower()
    digits = re.sub(r"\D", "", contact.phone or "")[-10:]
    if len(digits) == 10 and digits[3:6] != "555" and not _is_placeholder(contact.phone or ""):
        terms["phone"] = digits
    for field, value in (("linkedin", contact.linkedin), ("github", contact.github)):
        handle = (value or "").strip().rstrip("/").rsplit("/", 1)[-1].lower()
        if len(handle) >= 4 and not _is_placeholder(value or "") and "your" not in handle:
            terms[field] = handle
    return terms


def _shareable_files() -> list[Path]:
    def _git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args, "-z"], cwd=ROOT, capture_output=True, text=True).stdout
        return [p for p in out.split("\0") if p]

    names = set(_git("ls-files")) | set(_git("ls-files", "--others", "--exclude-standard"))
    return sorted(ROOT / n for n in names)


def test_owner_contact_details_are_absent_from_every_shareable_file():
    if not PROFILE.exists():
        pytest.skip("no real config/candidate_profile.yaml on this machine")
    try:
        terms = _owner_terms(load_profile(PROFILE))
    except Exception:  # an unreadable profile is someone else's test to fail
        pytest.skip("config/candidate_profile.yaml could not be parsed")
    if os.environ.get("JOB_HUNTER_PII_SKIP_SURNAME"):
        terms.pop("surname", None)
    if not terms:
        pytest.skip("profile contact block holds only example placeholders")

    leaks: list[str] = []
    for path in _shareable_files():
        if path.suffix.lower() in _BINARY or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        digits = None
        for field, term in terms.items():
            if field == "phone":
                digits = re.sub(r"\D", "", text) if digits is None else digits
                found = term in digits
            elif field == "surname":
                found = re.search(rf"\b{re.escape(term)}\b", text) is not None
            else:
                found = term in text
            if found:
                leaks.append(f"{path.relative_to(ROOT)}: {field}")
    hint = (
        "\n(if a 'surname' hit is just an ordinary word, set JOB_HUNTER_PII_SKIP_SURNAME=1)"
        if any(line.endswith(": surname") for line in leaks)
        else ""
    )
    assert not leaks, (
        "owner contact details found in shareable files (values withheld):\n" + "\n".join(leaks) + hint
    )


def test_owner_terms_ignore_example_placeholders():
    profile = CandidateProfile.model_validate(
        {
            "contact": {
                "name": "Your Name",
                "email": "you@example.com",
                "phone": "+1 555 555 0100",
                "linkedin": "https://www.linkedin.com/in/your-handle",
                "github": "https://github.com/your-handle",
            }
        }
    )
    assert _owner_terms(profile) == {}


def test_owner_terms_pick_up_real_looking_values():
    profile = CandidateProfile.model_validate(
        {
            "contact": {
                "name": "Alex Rivera",
                "email": "alex@mail.test",
                "phone": "+1 313 867 5309",
                "linkedin": "https://www.linkedin.com/in/alexrivera/",
                "github": "https://github.com/arivera",
            }
        }
    )
    assert _owner_terms(profile) == {
        "name": "alex rivera",
        "surname": "rivera",
        "email": "alex@mail.test",
        "phone": "3138675309",
        "linkedin": "alexrivera",
        "github": "arivera",
    }
