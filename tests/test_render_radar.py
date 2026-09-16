import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from render_radar import _default_title, build  # noqa: E402


def _search_json(candidates: list[dict], source_health: list[dict] | None = None) -> dict:
    return {
        "summary": {"sources_succeeded": 20, "sources_attempted": 21},
        "candidates": candidates,
        "source_health": source_health or [],
    }


def _candidate(
    source_key,
    job_id,
    posted_at=None,
    first_seen_at=None,
    location_raw="Detroit, MI",
    visa_sponsorship="unmentioned",
    sponsorship_evidence=None,
    salary_evidence=None,
    work_arrangement="unknown",
    company="Acme",
    title="Engineer",
    url="https://example.com/1",
):
    return {
        "source_key": source_key,
        "job_id": job_id,
        "posted_at": posted_at,
        "first_seen_at": first_seen_at,
        "location_raw": location_raw,
        "visa_sponsorship": visa_sponsorship,
        "sponsorship_evidence": sponsorship_evidence,
        "salary_evidence": salary_evidence,
        "work_arrangement": work_arrangement,
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

    assert stats == {
        "strong": 2,
        "review": 1,
        "below_50": 1,
        "never_reviewed": 0,
        "source_issues": 0,
        "failed": 0,
    }
    html = output_path.read_text()
    assert "Exceptional Role" in html
    assert "Weak Role" in html  # below-50 candidates are listed in their own section
    # No separate 90+/80+ text tag — the score number's own color (via the row's
    # tier-* class) is the only tier signal now.
    assert 'tier-exceptional"' in html
    assert 'tier-strong"' in html
    assert "90+" not in html
    assert "80+" not in html


def test_score_gradient_covers_the_full_50_to_100_range(tmp_path):
    """The score color is a five-step gradient across the whole 50-100 range, not just
    a hard cutoff at 80/90 — a 55 (barely "for review") and a 68 must read differently
    from each other and from an 88, not all fall back to the same uncolored default.
    Below 50 stays uncolored: those jobs are excluded from the chat-facing summary
    entirely, so there's no reason for a reader to be comparing shades of "not it"."""
    now = datetime(2026, 8, 31, tzinfo=UTC)
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(
            _search_json(
                [
                    _candidate("x", "1", title="Fair Role"),
                    _candidate("x", "2", title="Moderate Role"),
                    _candidate("x", "3", title="Promising Role"),
                    _candidate("x", "4", title="Strong Role"),
                    _candidate("x", "5", title="Exceptional Role"),
                    _candidate("x", "6", title="Below Fifty Role"),
                ]
            )
        )
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(
        json.dumps(
            [
                _assessment("x", "1", 55, title="Fair Role"),
                _assessment("x", "2", 65, title="Moderate Role"),
                _assessment("x", "3", 72, title="Promising Role"),
                _assessment("x", "4", 85, title="Strong Role"),
                _assessment("x", "5", 95, title="Exceptional Role"),
                _assessment("x", "6", 45, title="Below Fifty Role"),
            ]
        )
    )
    output_path = tmp_path / "out.html"
    build(
        search_path=search_path, assessments_path=assessments_path, output_path=output_path,
        title="Test Radar", keyword_label=None, new_days=10, now=now,
    )
    html = output_path.read_text()
    for title, tier in [
        ("Fair Role", "fair"),
        ("Moderate Role", "moderate"),
        ("Promising Role", "promising"),
        ("Strong Role", "strong"),
        ("Exceptional Role", "exceptional"),
    ]:
        idx = html.index(title)
        assert f'tier-{tier}"' in html[max(0, idx - 400) : idx]
    below_idx = html.index("Below Fifty Role")
    assert 'tier-plain"' in html[max(0, below_idx - 400) : below_idx]


def test_never_reviewed_candidate_excluded_from_scored_groups_but_listed_separately(tmp_path):
    """A never-reviewed candidate must not appear in Strong/Review/Below-50 (no score to
    place it), but it must still be listed — in its own "Not LLM Reviewed" section — rather
    than silently omitted, so a job newly surfaced by a refilter isn't invisible until review
    catches up to it."""
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(_search_json([_candidate("x", "1"), _candidate("x", "2", title="Unreviewed Role")]))
    )
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
    assert "Not LLM Reviewed" in html
    assert "Unreviewed Role" in html
    assert "Not yet reviewed by the local model" in html
    # Not counted among the scored groups' rows.
    assert stats["strong"] == 1 and stats["review"] == 0 and stats["below_50"] == 0


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
    # "New" is the one tag that renders immediately before the title text itself
    # (inside .job-title-line, so .job's own start position is unaffected) — every
    # other tag renders in its own column after the whole job block.
    assert 'tag-new">New' in html[max(0, fresh_idx - 200) : fresh_idx]
    assert 'tag-new">New' not in html[max(0, older_idx - 200) : older_idx]


def test_row_grid_column_count_is_constant_regardless_of_tags(tmp_path):
    """Regression: summary's grid tracks are positional (auto-placement fills them in
    DOM order, not by track name) — a row with zero secondary tags must still emit an
    (empty) .tags element, or .row-end would silently shift into .tags' own track and
    the date/feedback column would misalign across rows exactly like the title column
    used to before tags moved out of a variable-width column ahead of it."""
    now = datetime(2026, 8, 31, tzinfo=UTC)
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(
            _search_json(
                [
                    _candidate("x", "1", title="No Tags Role", work_arrangement="onsite"),
                    _candidate("x", "2", title="Tagged Role", work_arrangement="remote"),
                ]
            )
        )
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(
        json.dumps(
            [
                _assessment("x", "1", 80, title="No Tags Role"),
                _assessment("x", "2", 80, title="Tagged Role"),
            ]
        )
    )
    output_path = tmp_path / "out.html"
    build(
        search_path=search_path, assessments_path=assessments_path, output_path=output_path,
        title="Test Radar", keyword_label=None, new_days=10, now=now,
    )
    html = output_path.read_text()
    no_tags_idx = html.index("No Tags Role")
    tagged_idx = html.index("Tagged Role")
    # Both rows must still have a .tags element (even an empty one) as a direct child
    # of the summary grid, immediately followed by .row-end — regardless of whether
    # that particular row actually has a secondary tag to show.
    assert '<span class="tags"></span>\n        <span class="row-end">' in html[no_tags_idx : no_tags_idx + 600]
    assert '<span class="tags"><span class="tag tag-remote">' in html[tagged_idx : tagged_idx + 600]


def test_apply_link_always_visible_and_below_feedback_buttons_in_both_sections(tmp_path):
    """The "View posting" link must never require expanding a row to reach — it sits
    in .row-end (inside <summary>, always visible) below the feedback buttons, in the
    exact same order, for a scored row and a never-reviewed one alike."""
    now = datetime(2026, 8, 31, tzinfo=UTC)
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(
            _search_json(
                [
                    _candidate("x", "1", title="Scored Role", url="https://example.com/scored"),
                    _candidate("x", "2", title="Unreviewed Role", url="https://example.com/unreviewed"),
                ]
            )
        )
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(json.dumps([_assessment("x", "1", 80, title="Scored Role")]))
    output_path = tmp_path / "out.html"
    build(
        search_path=search_path, assessments_path=assessments_path, output_path=output_path,
        title="Test Radar", keyword_label=None, new_days=10, now=now,
    )
    html = output_path.read_text()

    scored_idx = html.index("Scored Role")
    scored_summary_end = html.index("</summary>", scored_idx)
    scored_block = html[scored_idx:scored_summary_end]
    assert "https://example.com/scored" in scored_block  # inside <summary>, not row-detail
    assert scored_block.index("feedback-buttons") < scored_block.index("apply-link")

    unreviewed_idx = html.index("Unreviewed Role")
    unreviewed_top_end = html.index("</div>", unreviewed_idx)
    unreviewed_block = html[unreviewed_idx:unreviewed_top_end]
    assert "https://example.com/unreviewed" in unreviewed_block
    assert unreviewed_block.index("feedback-buttons") < unreviewed_block.index("apply-link")

    # Never a filter/gate on the link's own visibility — the row-detail section (only
    # matches/gaps/sponsorship-evidence, no link) must not contain the URL a second time.
    row_detail_start = html.index('<div class="row-detail">', scored_idx)
    row_detail_end = html.index("</details>", row_detail_start)
    assert "https://example.com/scored" not in html[row_detail_start:row_detail_end]


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
    # Tags render inside .job, right after the title — see test_new_tag_uses_posting_
    # recency_window's comment for why a before-the-title column was removed.
    assert 'tag-sponsor-no">No Sponsorship' in html[no_idx : no_idx + 400]
    assert "will not be sponsored" in html
    assert 'tag-sponsor-yes">Sponsorship OK' in html[yes_idx : yes_idx + 400]
    assert 'tag-sponsor' not in html[unmentioned_idx : unmentioned_idx + 400]
    assert 'tag-sponsor' not in html[predates_idx : predates_idx + 400]


def test_job_meta_shows_location_and_salary_in_the_always_visible_summary(tmp_path):
    """Location and salary must be visible without expanding the row — they render as
    one job-meta line inside the always-visible <summary>, not hidden inside the
    click-to-expand detail. Salary is still never a filter: it never changes inclusion,
    and a row with no salary_evidence just shows location alone (no placeholder),
    mirroring sponsorship's "unmentioned carries no tag" reasoning."""
    now = datetime(2026, 8, 31, tzinfo=UTC)
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(
            _search_json(
                [
                    _candidate(
                        "x", "1", salary_evidence="$76,100.00 to $114,300.00",
                        title="Salary Stated Role",
                    ),
                    _candidate("x", "2", salary_evidence=None, title="No Salary Role"),
                ]
            )
        )
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(
        json.dumps(
            [
                _assessment("x", "1", 80, title="Salary Stated Role"),
                _assessment("x", "2", 80, title="No Salary Role"),
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
    assert stats["strong"] == 2

    html = output_path.read_text()
    stated_idx = html.index("Salary Stated Role")
    no_salary_idx = html.index("No Salary Role")
    # Both the location and the salary line sit in the <summary> — before the
    # click-to-expand <div class="row-detail"> even starts, not inside it. Salary gets
    # its own .job-salary span (a distinct color from location's muted default).
    stated_summary_end = html.index("</summary>", stated_idx)
    no_salary_summary_end = html.index("</summary>", no_salary_idx)
    assert (
        'job-location">Detroit, MI</span> · <span class="job-salary">$76,100.00 to $114,300.00'
        in html[stated_idx:stated_summary_end]
    )
    assert 'job-location">Detroit, MI<' in html[no_salary_idx:no_salary_summary_end]
    assert "job-salary" not in html[no_salary_idx:no_salary_summary_end]
    assert "$76,100.00" not in html[no_salary_idx:no_salary_summary_end]


def test_work_arrangement_tags_and_never_excludes_a_job(tmp_path):
    """Same tag-not-filter requirement as sponsorship: work arrangement never changes
    inclusion, just which tag (if any) a row carries. Only remote/hybrid get a tag —
    onsite (the unremarkable default) and unknown (uninformative) carry none, mirroring
    sponsorship's "unmentioned carries no tag" reasoning."""
    now = datetime(2026, 8, 31, tzinfo=UTC)
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(
            _search_json(
                [
                    _candidate("x", "1", work_arrangement="remote", title="Remote Role"),
                    _candidate("x", "2", work_arrangement="hybrid", title="Hybrid Role"),
                    _candidate("x", "3", work_arrangement="onsite", title="Onsite Role"),
                    _candidate("x", "4", work_arrangement="unknown", title="Unknown Role"),
                ]
            )
        )
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(
        json.dumps(
            [
                _assessment("x", "1", 80, title="Remote Role"),
                _assessment("x", "2", 80, title="Hybrid Role"),
                _assessment("x", "3", 80, title="Onsite Role"),
                _assessment("x", "4", 80, title="Unknown Role"),
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
    # All four land in Strong (score 80) — work arrangement changed nothing about inclusion.
    assert stats["strong"] == 4

    html = output_path.read_text()
    remote_idx = html.index("Remote Role")
    hybrid_idx = html.index("Hybrid Role")
    onsite_idx = html.index("Onsite Role")
    unknown_idx = html.index("Unknown Role")
    # Tags render inside .job, right after the title — see test_new_tag_uses_posting_
    # recency_window's comment for why a before-the-title column was removed.
    assert 'tag-remote">Remote' in html[remote_idx : remote_idx + 400]
    assert 'tag-hybrid">Hybrid' in html[hybrid_idx : hybrid_idx + 400]
    assert "tag-remote" not in html[onsite_idx : onsite_idx + 400]
    assert "tag-hybrid" not in html[onsite_idx : onsite_idx + 400]
    assert "tag-remote" not in html[unknown_idx : unknown_idx + 400]
    assert "tag-hybrid" not in html[unknown_idx : unknown_idx + 400]


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


def test_feedback_buttons_carry_correct_data_attributes(tmp_path):
    """The click-and-submit feedback mechanism (docs/feedback-exclusion-plan.md) depends on
    each row's buttons carrying the right identifiers — a mismatch here would silently tag
    the wrong job."""
    candidate = _candidate("ford", "77", title="ADAS Engineer", company="Ford Motor Company")
    candidate["department"] = "ADAS Team"
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_json([candidate])))

    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(
        json.dumps([_assessment("ford", "77", 82, title="ADAS Engineer", company="Ford Motor Company")])
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
    assert 'data-source-key="ford"' in html
    assert 'data-job-id="77"' in html
    assert 'data-company="Ford Motor Company"' in html
    assert 'data-title="ADAS Engineer"' in html
    assert 'data-department="ADAS Team"' in html
    assert 'data-score="82"' in html
    assert 'data-label="relevant"' in html
    assert 'data-label="okay"' in html
    assert 'data-label="irrelevant"' in html
    assert 'radar-feedback-search.json' in html  # __SEARCH_STEM__ substitution


def test_never_reviewed_row_matches_scored_row_layout(tmp_path):
    """A never-reviewed job gets the exact same layout as a scored row (audited
    end to end, not just "has the tags somewhere"): an NR placeholder where the score
    goes, the New tag before the title, location shown in job-meta (this section never
    showed it at all before), other tags in their own column, and feedback buttons —
    just no score/matches/gaps, since there's no assessment to draw them from."""
    candidate = _candidate(
        "ford", "77", title="ADAS Engineer", company="Ford Motor Company",
        posted_at="2026-08-28T00:00:00Z",  # 3 days before `now` below -> [New]
        visa_sponsorship="not_available",
    )
    candidate["department"] = "ADAS Team"
    candidate["work_arrangement"] = "hybrid"
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_json([candidate])))
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(json.dumps([]))
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
    assert stats == {
        "strong": 0,
        "review": 0,
        "below_50": 0,
        "never_reviewed": 1,
        "source_issues": 0,
        "failed": 0,
    }
    html = output_path.read_text()
    assert 'data-source-key="ford"' in html
    assert 'data-job-id="77"' in html
    assert 'data-label="relevant"' in html
    title_idx = html.index("ADAS Engineer")
    # NR sits where the score goes, immediately before .job — mirroring _row_html's
    # own <span class="score">...</span><span class="job"> adjacency exactly.
    assert 'class="score score-nr" title="Not yet reviewed by the local model">NR</span>\n        <span class="job">' in html[max(0, title_idx - 300) : title_idx]
    # New renders immediately before the title text itself, same as a scored row.
    assert 'tag-new">New' in html[max(0, title_idx - 100) : title_idx]
    # Location (job-meta) — this section rendered no location/salary at all before.
    assert 'job-location">Detroit, MI<' in html[title_idx : title_idx + 400]
    # Secondary tags (arrangement, sponsorship) sit in their own column after .job.
    assert 'tag-hybrid">Hybrid' in html[title_idx : title_idx + 700]
    assert 'tag-sponsor-no">No Sponsorship' in html[title_idx : title_idx + 700]


def test_build_surfaces_non_ok_source_health_grouped_and_ordered(tmp_path):
    """Every source_health entry that isn't 'ok' must appear in the Collection issues
    section, ordered failed -> warning -> unsupported (most actionable first) and
    alphabetically by company within each group — a source that collected fine (ok)
    must not appear at all."""
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(
            _search_json(
                [],
                source_health=[
                    {"source_key": "acme", "company": "Acme", "status": "ok", "message": None},
                    {
                        "source_key": "zeta",
                        "company": "Zeta Motors",
                        "status": "unsupported",
                        "message": "Akamai blocks every request.",
                    },
                    {
                        "source_key": "beta",
                        "company": "Beta Corp",
                        "status": "failed",
                        "message": "Connection timed out.",
                    },
                    {
                        "source_key": "widget",
                        "company": "Widget Inc",
                        "status": "warning",
                        "message": "Job count dropped 80%.",
                    },
                    {
                        "source_key": "alpha",
                        "company": "Alpha Robotics",
                        "status": "failed",
                        "message": "DNS resolution failed.",
                    },
                ],
            )
        )
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(json.dumps([]))
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
    assert stats["source_issues"] == 4
    assert stats["failed"] == 2
    html = output_path.read_text()
    assert "Acme" not in html  # the ok source never appears in Collection issues
    failed_pos = html.index("Alpha Robotics")
    beta_pos = html.index("Beta Corp")
    warning_pos = html.index("Widget Inc")
    unsupported_pos = html.index("Zeta Motors")
    assert failed_pos < beta_pos < warning_pos < unsupported_pos
    assert "Connection timed out." in html
    assert "DNS resolution failed." in html
    assert "Job count dropped 80%." in html
    assert "Akamai blocks every request." in html
    assert 'tag-source-failed">Failed' in html
    assert 'tag-source-warning">Warning' in html
    assert 'tag-source-unsupported">Unsupported' in html


def test_default_title_derivation():
    assert _default_title(None) == "Candidate Radar"
    assert _default_title("product manager") == "Product Manager Radar"
    assert _default_title("ADAS, Robotics") == "ADAS & Robotics Radar"


def test_eyebrow_date_uses_the_calendar_date_of_whatever_tzinfo_now_carries(tmp_path):
    """The report's eyebrow date must reflect the day the run actually happened on, not
    tomorrow's UTC date for a late-evening US run. `build()` formats its `date_str` using
    whatever tzinfo `now` is given rather than forcing a UTC conversion; production passes a
    real system-local `now` (see cli.py's call site), and this proves the formatting itself is
    correct given an explicitly non-UTC one."""
    from zoneinfo import ZoneInfo

    late_eastern = datetime(2026, 9, 8, 22, 30, tzinfo=ZoneInfo("America/New_York"))
    assert late_eastern.astimezone(UTC).date() == datetime(2026, 9, 9).date()

    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_json([_candidate("x", "1")])))
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(json.dumps([_assessment("x", "1", 80)]))
    output_path = tmp_path / "out.html"

    build(
        search_path=search_path,
        assessments_path=assessments_path,
        output_path=output_path,
        title="Test Radar",
        keyword_label=None,
        new_days=10,
        now=late_eastern,
    )

    html = output_path.read_text()
    assert "2026-09-08" in html
    assert "2026-09-09" not in html


def test_undated_job_falls_back_to_first_seen_and_long_standing_tag(tmp_path):
    """A candidate with no posted_at at all falls back to a clearly-labeled "First seen
    {date}" (never confused with a real posting date) and, once first seen a long time ago
    (past undated_stale_days), a "Long-standing" tag instead of [New] — display-only, the job
    still appears in its normal score-based section regardless."""
    now = datetime(2026, 9, 5, tzinfo=UTC)
    first_seen_at = (now - timedelta(days=66)).isoformat()
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(_search_json([_candidate("x", "1", posted_at=None, first_seen_at=first_seen_at)]))
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(json.dumps([_assessment("x", "1", 80)]))
    output_path = tmp_path / "out.html"

    build(
        search_path=search_path,
        assessments_path=assessments_path,
        output_path=output_path,
        title="Test Radar",
        keyword_label=None,
        new_days=10,
        undated_new_days=15,
        undated_stale_days=45,
        now=now,
    )

    html = output_path.read_text()
    assert "First seen" in html
    assert "Date unknown" not in html
    assert 'tag-new">New' not in html
    assert 'tag-long-standing">Long-standing' in html


def test_undated_job_recently_first_seen_gets_new_tag_not_long_standing(tmp_path):
    now = datetime(2026, 9, 5, tzinfo=UTC)
    first_seen_at = (now - timedelta(days=3)).isoformat()
    search_path = tmp_path / "search.json"
    search_path.write_text(
        json.dumps(_search_json([_candidate("x", "1", posted_at=None, first_seen_at=first_seen_at)]))
    )
    assessments_path = tmp_path / "assessments.json"
    assessments_path.write_text(json.dumps([_assessment("x", "1", 80)]))
    output_path = tmp_path / "out.html"

    build(
        search_path=search_path,
        assessments_path=assessments_path,
        output_path=output_path,
        title="Test Radar",
        keyword_label=None,
        new_days=10,
        undated_new_days=15,
        undated_stale_days=45,
        now=now,
    )

    html = output_path.read_text()
    assert "First seen" in html
    assert 'tag-new">New' in html
    assert 'tag-long-standing">Long-standing' not in html
