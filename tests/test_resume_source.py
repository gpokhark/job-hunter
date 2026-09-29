import os
import time
from pathlib import Path

import pytest

from job_hunter.config import CandidateProfile
from job_hunter.resume_source import (
    company_rules,
    find_cover_sample,
    find_personalization,
    find_review_evidence,
    is_example,
    jd_company,
    jd_title,
    personalization_problems,
    personalization_rules,
    personalization_status,
    resolve_master_resume,
    role_rules,
)


def _resume_dir(root: Path) -> Path:
    directory = root / "config" / "resume"
    directory.mkdir(parents=True)
    return directory


def test_newest_dated_file_wins_by_filename_not_mtime(tmp_path):
    directory = _resume_dir(tmp_path)
    old = directory / "main_resume_2025-01-05.md"
    new = directory / "main_resume_2026-03-09.md"
    new.write_text("new")
    time.sleep(0.01)
    old.write_text("old")  # older date but *newer* mtime: must still lose
    os.utime(new, (1, 1))
    resolved = resolve_master_resume(tmp_path)
    assert resolved.path == new.resolve()
    assert resolved.source == "dated"


def test_names_that_do_not_match_or_are_not_real_dates_are_ignored(tmp_path):
    directory = _resume_dir(tmp_path)
    (directory / "main_resume_2026-13-45.md").write_text("bad month")
    (directory / "main_resume_latest.md").write_text("no date")
    (directory / "resume_2026-01-01.md").write_text("wrong prefix")
    (directory / "main_resume_2026-01-02.md.bak").write_text("wrong suffix")
    (directory / "main_resume_2026-02-02.md").mkdir()  # a directory, not a file
    good = directory / "main_resume_2024-06-01.md"
    good.write_text("ok")
    assert resolve_master_resume(tmp_path).path == good.resolve()


def test_explicit_path_beats_everything_and_must_exist(tmp_path):
    directory = _resume_dir(tmp_path)
    (directory / "main_resume_2026-03-09.md").write_text("dated")
    explicit = tmp_path / "elsewhere.md"
    explicit.write_text("explicit")
    resolved = resolve_master_resume(tmp_path, explicit=explicit)
    assert (resolved.path, resolved.source) == (explicit.resolve(), "explicit")
    with pytest.raises(FileNotFoundError, match="elsewhere-missing.md"):
        resolve_master_resume(tmp_path, explicit=tmp_path / "elsewhere-missing.md")


def test_profile_resume_path_is_only_a_fallback(tmp_path):
    fallback = tmp_path / "config" / "my_resume.md"
    fallback.parent.mkdir()
    fallback.write_text("profile")
    profile = CandidateProfile(resume_path=Path("config/my_resume.md"))  # relative to root
    resolved = resolve_master_resume(tmp_path, profile=profile)
    assert (resolved.path, resolved.source) == (fallback.resolve(), "profile")
    # a dated file appears -> it wins without touching the profile
    directory = _resume_dir(tmp_path)
    dated = directory / "main_resume_2026-01-01.md"
    dated.write_text("dated")
    assert resolve_master_resume(tmp_path, profile=profile).path == dated.resolve()


def test_nothing_found_explains_how_to_fix_it(tmp_path):
    with pytest.raises(FileNotFoundError, match=r"config/resume/main_resume_<YYYY-MM-DD>\.md"):
        resolve_master_resume(tmp_path)
    profile = CandidateProfile(resume_path=Path("config/missing.md"))
    with pytest.raises(FileNotFoundError):
        resolve_master_resume(tmp_path, profile=profile)


def test_is_example_flags_placeholder_filenames():
    assert is_example(Path("config/resume.example.md"))
    assert not is_example(Path("config/resume/main_resume_2026-01-01.md"))


def test_optional_files_are_found_or_none(tmp_path):
    assert find_personalization(tmp_path) is None
    assert find_cover_sample(tmp_path) is None
    assert find_review_evidence(tmp_path) is None
    directory = _resume_dir(tmp_path)
    (directory / "personalization.md").write_text("tone: plain")
    (directory / "cover_letter_2025-01-01.md").write_text("old")
    (directory / "cover_letter_2026-02-02.md").write_text("new")
    (directory / "Review_Evidence_2026-04-04.md").write_text("ieee")
    assert find_personalization(tmp_path) == (directory / "personalization.md").resolve()
    assert find_cover_sample(tmp_path) == (directory / "cover_letter_2026-02-02.md").resolve()
    assert find_review_evidence(tmp_path) == (directory / "Review_Evidence_2026-04-04.md").resolve()


def test_personalization_status_none_and_plain_file(tmp_path):
    assert personalization_status(tmp_path) == (None, None)
    directory = _resume_dir(tmp_path)
    (directory / "personalization.md").write_text("## all\n- keep it short\n")
    path, warning = personalization_status(tmp_path)
    assert path == (directory / "personalization.md").resolve() and warning is None


def test_personalization_status_ignores_an_uncustomized_template(tmp_path):
    directory = _resume_dir(tmp_path)
    (directory / "personalization.md").write_text(
        "# Personalization\n\nTEMPLATE-NOT-CUSTOMIZED: delete this line.\n\n## all\n- sample rule\n"
    )
    path, warning = personalization_status(tmp_path)
    assert path is None
    assert "TEMPLATE-NOT-CUSTOMIZED" in warning and "personalization.md" in warning


def test_personalization_status_sentinel_only_counts_at_the_start_of_a_line(tmp_path):
    directory = _resume_dir(tmp_path)
    (directory / "personalization.md").write_text(
        "Notes: I removed the TEMPLATE-NOT-CUSTOMIZED line already.\n## all\n- real rule\n"
    )
    path, warning = personalization_status(tmp_path)
    assert path is not None and warning is None


def test_shipped_example_is_flagged_as_a_template_when_copied_unedited(tmp_path):
    example = Path(__file__).resolve().parents[1] / "config" / "resume" / "personalization.example.md"
    directory = _resume_dir(tmp_path)
    (directory / "personalization.md").write_text(example.read_text())
    path, warning = personalization_status(tmp_path)
    assert path is None and warning


SAMPLE = """# Personalization

## all
- Never use the word "synergy".
- The client for all work at Initech was Acme Corp. Name Acme only when applying to Acme.

## resume-generator
### Employer rules
- When applying to Ford Motor Company, add a summary bullet about combined Ford experience.
  Omit it for every other employer.
- Keep the Technical Skills block to 2 lines.

## outreach-writer
- Sign off with "Best regards,".
"""


def test_personalization_rules_parses_bullets_and_continuation_lines():
    rules = personalization_rules(SAMPLE)
    assert list(rules) == ["all", "resume-generator", "outreach-writer"]
    assert rules["all"][0] == 'Never use the word "synergy".'
    assert rules["resume-generator"][0].endswith("Omit it for every other employer.")
    assert len(rules["resume-generator"]) == 2  # the ### heading is not a section


def test_personalization_problems_clean_file_has_none():
    clean = SAMPLE.replace("Initech", "Globex").replace("Acme", "Globex")
    assert personalization_problems(clean) == []


def test_personalization_problems_flags_unknown_section_and_sample_leftovers():
    text = "## resume generator\n- keep it short\n\n## all\n- The client was Acme Corp.\n"
    problems = personalization_problems(text)
    assert any("resume generator" in p and "unknown section" in p for p in problems)
    assert any("Acme Corp" in p and "sample" in p for p in problems)


def test_personalization_problems_flags_a_file_with_no_recognized_section():
    problems = personalization_problems("just some notes\n- a rule\n")
    assert any("no recognized section" in p for p in problems)


def test_company_rules_match_the_employer_named_in_a_rule():
    hits = company_rules(SAMPLE, "Ford_Motor_Company")
    assert [h["section"] for h in hits] == ["resume-generator"]
    assert "combined Ford experience" in hits[0]["rule"]
    assert company_rules(SAMPLE, "Globex_Inc") == []
    assert company_rules(SAMPLE, "") == []


def test_company_rules_ignore_sections_that_are_not_recognized():
    assert company_rules("## notes\n- Ford is great\n", "Ford") == []


ROLE_SAMPLE = """## resume-generator
- [role: program manager, project manager, TPM] Lead each recent role with program-management work.
- [role: data engineer] Put the Python and SQL skills first.
- Keep the Technical Skills block to 2 lines.

## all
- [Role: Program Manager | Product Owner] Keep reverse-chronological order.
"""


def test_role_rules_match_a_tag_against_the_job_title_by_whole_words():
    hits = role_rules(ROLE_SAMPLE, "Technical Program Manager, Off-the-Shelf Hardware, DeepMind")
    assert {(h["section"], h["rule"]) for h in hits} == {
        ("resume-generator", "Lead each recent role with program-management work."),
        ("all", "Keep reverse-chronological order."),
    }
    assert all(h["roles"] and all(isinstance(r, str) for r in h["roles"]) for h in hits)


def test_role_rules_ignore_untagged_bullets_and_non_matching_tags():
    hits = role_rules(ROLE_SAMPLE, "Senior Data Engineer")
    assert [h["rule"] for h in hits] == ["Put the Python and SQL skills first."]
    assert role_rules(ROLE_SAMPLE, "Chef") == []
    assert role_rules(ROLE_SAMPLE, "") == []


def test_role_rules_do_not_match_inside_a_longer_word():
    assert role_rules("## all\n- [role: tpm] x\n", "Attempt Lead") == []
    assert role_rules("## all\n- [role: tpm] x\n", "TPM, Hardware") != []


def test_role_rules_only_read_recognized_sections():
    assert role_rules("## notes\n- [role: engineer] x\n", "Engineer") == []


def test_personalization_problems_flags_broken_role_tags():
    text = "## all\n- [role: ] empty\n- Use this [role: engineer] mid-bullet\n- [roles: manager] typo\n"
    problems = personalization_problems(text)
    assert any("empty" in p and "role" in p for p in problems)
    assert any("start of the bullet" in p for p in problems)
    assert any("[roles:" in p or "role tag" in p for p in problems)


def test_a_valid_role_tag_is_not_a_problem():
    assert personalization_problems("## all\n- [role: program manager] lead with programs\n") == []


def test_jd_title_and_company_come_from_the_exported_jd_file(tmp_path):
    folder = tmp_path / "data" / "output" / "Acme_Corp"
    folder.mkdir(parents=True)
    jd = folder / "JD_Acme_Corp_Program_Manager_2026-09-28.txt"
    jd.write_text("\n\nTechnical Program Manager, Hardware\nMountain View\n")
    assert jd_title(jd) == "Technical Program Manager, Hardware"
    assert jd_company(jd) == "Acme_Corp"
    elsewhere = tmp_path / "notes.txt"
    elsewhere.write_text("Some Title\n")
    assert jd_title(elsewhere) == "Some Title"
    assert jd_company(elsewhere) is None
    assert jd_title(tmp_path / "missing.txt") is None


def test_stray_dated_file_directly_in_config_is_not_the_dated_master(tmp_path):
    stray = tmp_path / "config" / "main_resume_2026-08-12.md"
    stray.parent.mkdir()
    stray.write_text("stray")
    with pytest.raises(FileNotFoundError):
        resolve_master_resume(tmp_path)
    # only the profile fallback can reach it
    profile = CandidateProfile(resume_path=Path("config/main_resume_2026-08-12.md"))
    resolved = resolve_master_resume(tmp_path, profile=profile)
    assert (resolved.path, resolved.source) == (stray.resolve(), "profile")


def test_example_resume_in_config_is_never_the_dated_master(tmp_path):
    example = tmp_path / "config" / "resume.example.md"
    example.parent.mkdir()
    example.write_text("placeholder")
    with pytest.raises(FileNotFoundError):
        resolve_master_resume(tmp_path)
    assert is_example(example)
