from datetime import UTC, date, datetime

import pytest

from job_hunter.jd_export import (
    JobHasNoDescription,
    JobNotFound,
    export_jd,
    html_to_text,
    render_jd_text,
    sanitize,
)
from job_hunter.models import Job, LocationConfidence
from job_hunter.storage import Storage

TODAY = date(2026, 9, 26)


def _job(job_id="42", title="ADAS Systems Engineer II (Hybrid)", company="Acme Motors, Inc.",
         description="<p>Build <b>ADAS</b> features.</p><ul><li>C++</li><li>Python &amp; ROS</li></ul>",
         **over):
    values = dict(
        source_key="acme", source_platform="test", company=company, job_id=job_id, title=title,
        url=f"https://example.com/jobs/{job_id}", us_eligible=True,
        location_confidence=LocationConfidence.HIGH, description=description, content_hash="h",
        location_raw="Detroit, MI", department="Autonomy",
        posted_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    values.update(over)
    return Job(**values)


@pytest.fixture
def env(tmp_path):
    storage = Storage(tmp_path / "data" / "jobs.sqlite3")
    yield storage, tmp_path
    storage.close()


def _export(storage, root, job_id="42", today=TODAY):
    return export_jd(
        storage, "acme", job_id, project_root=root, output_root=root / "data" / "output", today=today
    )


# --- pure helpers ---------------------------------------------------------------------------

def test_html_to_text_keeps_paragraphs_and_bullets_and_decodes_entities():
    text = html_to_text("<p>Build <b>ADAS</b> features.</p><ul><li>C++</li><li>Python &amp; ROS</li></ul><p>A&nbsp;B</p>")
    assert text == "Build ADAS features.\n\n- C++\n- Python & ROS\n\nA B"


def test_html_to_text_drops_scripts_and_styles_and_keeps_plain_text_unchanged():
    assert html_to_text("Hello<script>alert(1)</script><style>p{}</style> world") == "Hello world"
    assert html_to_text("Salary < 100k and > 50k\nSecond line") == "Salary < 100k and > 50k\nSecond line"
    assert html_to_text("a<br>b<br/>c") == "a\nb\nc"


def test_sanitize_allows_only_safe_filename_characters():
    assert sanitize("Acme Motors, Inc.", "Company") == "Acme_Motors_Inc"
    assert sanitize("../../etc/passwd", "Job") == "etcpasswd"
    assert sanitize("..\\..\\windows", "Job") == "windows"
    assert sanitize("C++ / C# Engineer", "Job") == "C_C_Engineer"
    assert sanitize("   ", "Job") == "Job"
    assert sanitize("---___", "Job") == "Job"
    assert sanitize("Ünïcödé Rôle", "Job") == "ncd_Rle"
    assert "/" not in sanitize("a/b", "x") and "\\" not in sanitize("a\\b", "x")


def test_render_jd_text_has_the_documented_layout():
    job = {"title": "ADAS Engineer", "location_raw": "Detroit, MI", "department": "Autonomy",
           "posted_at": "2026-09-20T00:00:00+00:00", "job_id": "42", "url": "https://x.test/42",
           "company": "Acme", "source_key": "acme", "salary_evidence": "$100,000 - $120,000"}
    text = render_jd_text(job, "Build things.")
    assert text == (
        "ADAS Engineer\nDetroit, MI\nAutonomy\n\nSummary\nPosted: 2026-09-20\nJob ID: 42\n"
        "Job URL: https://x.test/42\nSource: Acme (acme)\n\nDescription\nBuild things.\n\n"
        "Pay & Benefits\n$100,000 - $120,000\n"
    )
    bare = render_jd_text({**job, "department": None, "salary_evidence": None, "posted_at": None,
                           "location_raw": None}, "Body")
    assert bare.startswith("ADAS Engineer\nNot specified\n\nSummary\nPosted: Not specified\n")
    assert "Pay & Benefits" not in bare and bare.endswith("Body\n")


# --- export ---------------------------------------------------------------------------------

def test_export_writes_the_file_and_returns_a_relative_prompt(env):
    storage, root = env
    storage.upsert_job(_job())
    result = _export(storage, root)
    assert result.created is True
    assert result.path == (root / "data" / "output" / "Acme_Motors_Inc"
                           / "JD_Acme_Motors_Inc_ADAS_Systems_Engineer_II_2026-09-26.txt").resolve()
    assert result.relative_path == "data/output/Acme_Motors_Inc/JD_Acme_Motors_Inc_ADAS_Systems_Engineer_II_2026-09-26.txt"
    assert result.prompt == f"Use the resume-generator skill on {result.relative_path}"
    body = result.path.read_text(encoding="utf-8")
    assert body.startswith("ADAS Systems Engineer II (Hybrid)\nDetroit, MI\nAutonomy\n")
    assert "Posted: 2026-09-20" in body and "Job ID: 42" in body
    assert "- C++\n- Python & ROS" in body and "<" not in body.split("Description")[1]
    assert str(root) not in result.relative_path


def test_reexport_of_identical_content_reuses_the_file(env):
    storage, root = env
    storage.upsert_job(_job())
    first = _export(storage, root)
    second = _export(storage, root)
    assert (second.created, second.path) == (False, first.path)
    assert len(list(first.path.parent.glob("JD_*.txt"))) == 1


def test_changed_description_the_same_day_gets_a_numbered_file_and_keeps_the_old_one(env):
    storage, root = env
    storage.upsert_job(_job())
    first = _export(storage, root)
    original = first.path.read_text(encoding="utf-8")
    storage.connection.execute(
        "UPDATE jobs SET description='<p>Completely different role now.</p>' WHERE job_id='42'"
    )
    storage.connection.commit()
    second = _export(storage, root)
    assert second.created is True and second.path.name.endswith("_2026-09-26_2.txt")
    assert first.path.read_text(encoding="utf-8") == original  # the JD an earlier CV used is intact
    third = _export(storage, root)
    assert (third.created, third.path) == (False, second.path)


def test_a_different_day_gets_its_own_dated_file(env):
    storage, root = env
    storage.upsert_job(_job())
    a = _export(storage, root, today=date(2026, 9, 26))
    b = _export(storage, root, today=date(2026, 9, 27))
    assert a.path != b.path and b.created is True


def test_missing_job_and_empty_description_write_nothing(env):
    storage, root = env
    with pytest.raises(JobNotFound):
        _export(storage, root, job_id="nope")
    storage.upsert_job(_job(job_id="7", description="   "))
    with pytest.raises(JobHasNoDescription):
        _export(storage, root, job_id="7")
    storage.upsert_job(_job(job_id="8", description="<p> </p><br>"))  # markup with no real text
    with pytest.raises(JobHasNoDescription):
        _export(storage, root, job_id="8")
    assert not (root / "data" / "output").exists()


def test_hostile_titles_and_companies_cannot_escape_the_output_folder(env):
    storage, root = env
    storage.upsert_job(_job(job_id="9", company="../../evil", title="..\\..\\x/y\\z <script> " + "A" * 400))
    result = _export(storage, root, job_id="9")
    output_root = (root / "data" / "output").resolve()
    assert output_root in result.path.resolve().parents
    assert len(result.path.name) < 200
    assert all(part.replace("_", "").replace("-", "").replace(".", "").isalnum()
               for part in result.path.relative_to(output_root).parts)


def test_pay_section_appears_only_when_the_job_has_salary_evidence(env):
    storage, root = env
    storage.upsert_job(_job())
    storage.connection.execute("UPDATE jobs SET salary_evidence='$90,000 - $110,000' WHERE job_id='42'")
    storage.connection.commit()
    assert "Pay & Benefits\n$90,000 - $110,000" in _export(storage, root).path.read_text(encoding="utf-8")


def test_get_job_for_jd_is_the_only_reader_of_description(env):
    storage, _ = env
    storage.upsert_job(_job())
    row = storage.get_job_for_jd("acme", "42")
    assert row["description"].startswith("<p>Build")
    assert "description" not in storage.get_job_snapshot("acme", "42")
    assert storage.get_job_for_jd("acme", "missing") is None
