from job_hunter.config import CandidateProfile, ContactInfo, contact_problems


def _ok(**over):
    values = {"name": "Jane Doe", "email": "jane.doe@mail.test", "phone": "+1 313 555 0142"}
    values.update(over)
    return ContactInfo(**values)


def test_name_parts_use_the_first_and_last_token():
    contact = ContactInfo(name="  Jane Q. Doe ")
    assert (contact.first_name, contact.last_name) == ("Jane", "Doe")
    assert ContactInfo(name="Cher").last_name == "Cher"
    assert ContactInfo().first_name is None and ContactInfo().last_name is None


def test_a_complete_contact_has_no_problems():
    assert contact_problems(_ok()) == []
    assert contact_problems(_ok(linkedin="https://www.linkedin.com/in/jd", github="https://github.com/jd")) == []


def test_missing_required_fields_are_reported():
    problems = contact_problems(ContactInfo())
    assert any(p.startswith("name:") for p in problems)
    assert any(p.startswith("email:") for p in problems)
    assert not any(p.startswith("phone:") for p in problems)  # optional


def test_placeholder_values_are_reported_even_when_optional():
    problems = contact_problems(ContactInfo(
        name="Your Name", email="you@example.com", phone="+1 555 555 0100",
        linkedin="https://www.linkedin.com/in/your-handle", github="https://github.com/your-handle",
    ))
    joined = "\n".join(problems)
    for field in ("name", "email", "phone", "linkedin", "github"):
        assert f"{field}:" in joined, field
    assert all("placeholder" in p for p in problems)


def test_whitespace_only_counts_as_missing():
    assert any(p.startswith("name:") for p in contact_problems(_ok(name="   ")))


def test_profile_has_an_empty_contact_by_default_and_parses_a_yaml_block():
    assert CandidateProfile().contact == ContactInfo()
    profile = CandidateProfile.model_validate({"contact": {"name": "Jane Doe", "email": "j@mail.test"}})
    assert profile.contact.email == "j@mail.test"
