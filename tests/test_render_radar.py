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
):
    return {
        "source_key": source_key,
        "job_id": job_id,
        "posted_at": posted_at,
        "location_raw": location_raw,
        "visa_sponsorship": visa_sponsorship,
        "sponsorship_evidence": sponsorship_evidence,
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
                    _candidate("x", "1"),
                    _candidate("x", "2"),
                    _candidate("x", "3"),
                    _candidate("x", "4"),
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
                    _candidate("x", "1", posted_at="2026-08-25T00:00:00Z"),  # 6 days old -> New
                    _candidate("x", "2", posted_at="2026-08-01T00:00:00Z"),  # 30 days old -> not New
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


def test_sponsorship_tags_and_never_excludes_a_job(tmp_path):
    """The core requirement: sponsorship status is a tag, never a filter — a
    not_available job must still appear (and be listed) exactly like any other job at
    its score, just carrying a different tag. 'unmentioned' carries no tag at all."""
    now = datetime(2026, 8, 31, tzinfo=UTC)
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(
            _search_json(
                [
                    _candidate("x", "1", visa_sponsorship="not_available", sponsorship_evidence="will not be sponsored"),
                    _candidate("x", "2", visa_sponsorship="available", sponsorship_evidence="sponsorship is available"),
                    _candidate("x", "3", visa_sponsorship="unmentioned"),
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
    # All three land in Strong (score 80) — sponsorship status changed nothing about inclusion.
    assert stats["strong"] == 3

    html = output_path.read_text()
    no_idx = html.index("No Sponsorship Role")
    yes_idx = html.index("Sponsorship OK Role")
    unmentioned_idx = html.index("Unmentioned Role")
    assert 'tag-sponsor-no">No Sponsorship' in html[max(0, no_idx - 400) : no_idx]
    assert "will not be sponsored" in html
    assert 'tag-sponsor-yes">Sponsorship OK' in html[max(0, yes_idx - 400) : yes_idx]
    assert 'tag-sponsor' not in html[max(0, unmentioned_idx - 400) : unmentioned_idx]


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
