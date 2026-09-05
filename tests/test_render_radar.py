import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from render_radar import _default_title, build  # noqa: E402


def _search_json(candidates: list[dict]) -> dict:
    return {
        "summary": {"sources_succeeded": 20, "sources_attempted": 21},
        "candidates": candidates,
    }


def _candidate(
    source_key,
    job_id,
    posted_at=None,
    location_raw="Detroit, MI",
    visa_sponsorship="unmentioned",
    sponsorship_evidence=None,
    company="Acme",
    title="Engineer",
    url="https://example.com/1",
):
    return {
        "source_key": source_key,
        "job_id": job_id,
        "posted_at": posted_at,
        "location_raw": location_raw,
        "visa_sponsorship": visa_sponsorship,
        "sponsorship_evidence": sponsorship_evidence,
        "company": company,
        "title": title,
        "url": url,
    }


def _assessment(source_key, job_id, score, company="Acme", title="Engineer", url="https://example.com/1"):
    return {
        "source_key": source_key,
        "job_id": job_id,
        "score": score,
        "company": company,
        "title": title,
        "url": url,
        "matches": ["m1"],
        "gaps": ["g1"],
    }


def test_build_groups_by_score_and_tags_tiers(tmp_path):
    now = datetime(2026, 8, 31, tzinfo=UTC)
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(
            _search_json(
                [
                    _candidate("x", "1", title="Exceptional Role"),
                    _candidate("x", "2", title="Strong Role"),
                    _candidate("x", "3", title="Review Role"),
                    _candidate("x", "4", title="Weak Role"),
                ]
            )
        )
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(
        json.dumps(
            [
                _assessment("x", "1", 92, title="Exceptional Role"),
                _assessment("x", "2", 82, title="Strong Role"),
                _assessment("x", "3", 60, title="Review Role"),
                _assessment("x", "4", 30, title="Weak Role"),
            ]
        )
    )
    output_path = tmp_path / "out.html"

    stats = build(
        search_path=search_path,
        assessments_path=assessments_path,
        output_path=output_path,
        title="Test Radar",
        keyword_label=None,
        new_days=10,
        now=now,
    )

    assert stats == {"strong": 2, "review": 1, "below_50": 1, "never_reviewed": 0}
    html = output_path.read_text()
    assert "Exceptional Role" in html
    assert "Weak Role" not in html  # below 50 must never be listed
    assert 'tag-exceptional">90+' in html
    assert 'tag-strong">80+' in html


def test_never_reviewed_candidate_excluded_and_counted(tmp_path):
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_json([_candidate("x", "1"), _candidate("x", "2")])))
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(json.dumps([_assessment("x", "1", 80, title="Reviewed Role")]))
    output_path = tmp_path / "out.html"

    stats = build(
        search_path=search_path,
        assessments_path=assessments_path,
        output_path=output_path,
        title="Test Radar",
        keyword_label=None,
        new_days=10,
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )

    assert stats["never_reviewed"] == 1
    html = output_path.read_text()
    assert "1 candidate(s) were never reviewed" in html


def test_new_tag_uses_posting_recency_window(tmp_path):
    now = datetime(2026, 8, 31, tzinfo=UTC)
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(
            _search_json(
                [
                    _candidate("x", "1", posted_at="2026-08-25T00:00:00Z", title="Fresh Role"),  # 6 days old -> New
                    _candidate("x", "2", posted_at="2026-08-01T00:00:00Z", title="Older Role"),  # 30 days old -> not New
                ]
            )
        )
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(
        json.dumps(
            [
                _assessment("x", "1", 80, title="Fresh Role"),
                _assessment("x", "2", 80, title="Older Role"),
            ]
        )
    )
    output_path = tmp_path / "out.html"

    build(
        search_path=search_path,
        assessments_path=assessments_path,
        output_path=output_path,
        title="Test Radar",
        keyword_label=None,
        new_days=10,
        now=now,
    )
    html = output_path.read_text()
    fresh_idx = html.index("Fresh Role")
    older_idx = html.index("Older Role")
    assert 'tag-new">New' in html[max(0, fresh_idx - 400) : fresh_idx]
    assert 'tag-new">New' not in html[max(0, older_idx - 400) : older_idx]


def test_url_title_company_come_from_the_fresh_candidate_not_the_stale_assessment(tmp_path):
    """Regression: a job's assessment is cached by content_hash and reused whenever the
    description hasn't changed — but that means its stored url/title/company are a
    snapshot frozen at whatever moment it was last actually reviewed. An adapter fix
    (e.g. a corrected URL) made after that must still show up immediately, since it has
    nothing to do with the LLM's judgment being stale. Real case: a Ford URL fix didn't
    appear in a report because the cached assessment predated it."""
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(
            _search_json(
                [
                    _candidate(
                        "ford",
                        "69384",
                        company="Ford Motor Company",
                        title="ADAS Rearview Camera Program Sign-Off Leader",
                        url="https://efds.fa.em5.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/69384",
                    )
                ]
            )
        )
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(
        json.dumps(
            [
                _assessment(
                    "ford",
                    "69384",
                    80,
                    company="Ford Motor Company",
                    title="ADAS Rearview Camera Program Sign-Off Leader",
                    # Stale snapshot from before the URL fix — must not win.
                    url="https://efds.fa.em5.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails/69384",
                )
            ]
        )
    )
    output_path = tmp_path / "out.html"

    build(
        search_path=search_path,
        assessments_path=assessments_path,
        output_path=output_path,
        title="Test Radar",
        keyword_label=None,
        new_days=10,
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    html = output_path.read_text()
    assert "hcmUI/CandidateExperience" in html
    assert "hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails" not in html


def test_sponsorship_tags_and_never_excludes_a_job(tmp_path):
    """The core requirement: sponsorship status is a tag, never a filter — a
    not_available job must still appear (and be listed) exactly like any other job at
    its score, just carrying a different tag. Only the two explicit, actionable states
    (available/not_available) get a tag — a blanket "Not Stated" label was tried and
    found to add no value, so unmentioned (and a missing field, for an older archive)
    both carry no tag at all."""
    now = datetime(2026, 8, 31, tzinfo=UTC)
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(
            _search_json(
                [
                    _candidate(
                        "x", "1", visa_sponsorship="not_available",
                        sponsorship_evidence="will not be sponsored", title="No Sponsorship Role",
                    ),
                    _candidate(
                        "x", "2", visa_sponsorship="available",
                        sponsorship_evidence="sponsorship is available", title="Sponsorship OK Role",
                    ),
                    _candidate("x", "3", visa_sponsorship="unmentioned", title="Unmentioned Role"),
                    _candidate("x", "4", visa_sponsorship=None, title="Predates Feature Role"),
                ]
            )
        )
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(
        json.dumps(
            [
                _assessment("x", "1", 80, title="No Sponsorship Role"),
                _assessment("x", "2", 80, title="Sponsorship OK Role"),
                _assessment("x", "3", 80, title="Unmentioned Role"),
                _assessment("x", "4", 80, title="Predates Feature Role"),
            ]
        )
    )
    output_path = tmp_path / "out.html"

    stats = build(
        search_path=search_path,
        assessments_path=assessments_path,
        output_path=output_path,
        title="Test Radar",
        keyword_label=None,
        new_days=10,
        now=now,
    )
    # All four land in Strong (score 80) — sponsorship status changed nothing about inclusion.
    assert stats["strong"] == 4

    html = output_path.read_text()
    no_idx = html.index("No Sponsorship Role")
    yes_idx = html.index("Sponsorship OK Role")
    unmentioned_idx = html.index("Unmentioned Role")
    predates_idx = html.index("Predates Feature Role")
    assert 'tag-sponsor-no">No Sponsorship' in html[max(0, no_idx - 400) : no_idx]
    assert "will not be sponsored" in html
    assert 'tag-sponsor-yes">Sponsorship OK' in html[max(0, yes_idx - 400) : yes_idx]
    assert 'tag-sponsor' not in html[max(0, unmentioned_idx - 400) : unmentioned_idx]
    assert 'tag-sponsor' not in html[max(0, predates_idx - 400) : predates_idx]


def test_empty_group_renders_fallback_message(tmp_path):
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_json([_candidate("x", "1")])))
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(json.dumps([_assessment("x", "1", 60, title="Only Review Role")]))
    output_path = tmp_path / "out.html"

    stats = build(
        search_path=search_path,
        assessments_path=assessments_path,
        output_path=output_path,
        title="Test Radar",
        keyword_label=None,
        new_days=10,
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    assert stats["strong"] == 0
    html = output_path.read_text()
    assert "No candidates scored 75 or above" in html


def test_default_title_derivation():
    assert _default_title(None) == "Candidate Radar"
    assert _default_title("product manager") == "Product Manager Radar"
    assert _default_title("ADAS, Robotics") == "ADAS & Robotics Radar"
