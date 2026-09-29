"""Guards for the resume/outreach skills' portability: they and their tracked templates must hold no
personal data and no employer-specific content. Everything per-user lives in the git-ignored
config/resume/personalization.md, started from config/resume/personalization.example.md."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PORTABLE_SKILLS = [
    ROOT / "skills" / name / "SKILL.md"
    for name in (
        "resume-generator", "outreach-writer", "salary-compare",
        "job-hunter", "job-scout", "job-reviewer", "job-radar", "job-feedback",
    )
]  # onboard-source is repo maintenance and legitimately names real employers/sites
PERSONALIZED_SKILLS = PORTABLE_SKILLS[:2]  # the two that read personalization.md
TEMPLATES = [
    ROOT / "config" / "resume" / "personalization.example.md",
    ROOT / "config" / "resume" / "README.md",
    ROOT / "config" / "candidate_profile.example.yaml",
    ROOT / "config" / "resume.example.md",
]

# Employer/domain words that leaked into examples once; a portable skill has no reason to name them.
EMPLOYER_WORDS = re.compile(
    r"\b(Ford|Valeo|Woven|Honda|Toyota|ADAS|Robotics|Milford|Northville|Detroit|Michigan|GM)\b"
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<![\d.])(?:\+?1[ -.]?)?\(?\b\d{3}\)?[ -.]\d{3}[ -.]\d{4}\b")
PROFILE_URL = re.compile(r"(?:linkedin\.com/in|github\.com)/([\w-]+)")

FAKE_EMAIL_DOMAINS = ("example.com", "example.org", "mail.test", "xxxxx")
FAKE_HANDLE = re.compile(r"your-handle|janedoe|example|xxxx|^jd$", re.IGNORECASE)


def _id(path: Path) -> str:
    return str(path.relative_to(ROOT))


@pytest.mark.parametrize("path", PORTABLE_SKILLS, ids=_id)
def test_portable_skills_name_no_employers(path):
    assert not EMPLOYER_WORDS.findall(path.read_text())


@pytest.mark.parametrize("path", PORTABLE_SKILLS + TEMPLATES, ids=_id)
def test_no_real_looking_pii_in_skills_or_templates(path):
    text = path.read_text()
    bad_emails = [e for e in EMAIL.findall(text) if not any(d in e for d in FAKE_EMAIL_DOMAINS)]
    assert not bad_emails, f"real-looking email: {bad_emails}"
    bad_phones = [p for p in PHONE.findall(text) if "555" not in p]
    assert not bad_phones, f"real-looking phone: {bad_phones}"
    bad_handles = [h for h in PROFILE_URL.findall(text) if not FAKE_HANDLE.search(h)]
    assert not bad_handles, f"real-looking profile handle: {bad_handles}"


def test_personalization_example_exists_with_the_three_sections():
    text = (ROOT / "config" / "resume" / "personalization.example.md").read_text()
    for heading in ("## all", "## resume-generator", "## outreach-writer"):
        assert heading in text


@pytest.mark.parametrize("path", PERSONALIZED_SKILLS, ids=_id)
def test_personalized_skills_point_users_at_the_example(path):
    assert "personalization.example.md" in path.read_text()
