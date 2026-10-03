import json
from datetime import UTC, datetime

from job_hunter.active_pool import StoredJob
from job_hunter.config import CandidateProfile
from job_hunter.models import Job, LocationConfidence
from job_hunter.why_missed import ArchiveInfo, explain, render_text, suggest_terms

NOW = datetime(2026, 10, 1, tzinfo=UTC)
PROFILE = CandidateProfile(
    target_title_terms=["vehicle test", "robotics"], target_domains=["validation", "ADAS"]
)


def make_job(**updates):
    values = dict(
        source_key="ford", source_platform="test", company="Ford", job_id="71202",
        title="Vehicle Calibration & Test Supervisor", url="https://example.com/71202",
        us_eligible=True, location_confidence=LocationConfidence.HIGH,
        location_evidence="structured U.S. country", department=None,
        posted_at=datetime(2026, 9, 30, tzinfo=UTC),
        first_seen_at=datetime(2026, 9, 30, 17, 28, tzinfo=UTC),
        last_seen_at=datetime(2026, 10, 1, 3, 24, tzinfo=UTC),
        description="validation validation verification vehicle", content_hash="h1",
    )
    values.update(updates)
    return Job(**values)


def stored(job=None, status="active", missing=0):
    return StoredJob(job or make_job(), status, missing)


def pool():
    return [
        make_job(),
        make_job(job_id="2", source_key="apple", title="Camera Calibration and Test Engineer"),
        make_job(job_id="3", source_key="abb", title="Test Supervisor"),
    ]


def run(job=None, **overrides):
    kwargs = dict(
        profile=PROFILE, max_age_days=30, now=NOW,
        source_health={"last_status": "ok", "last_job_count": 842},
        archive=ArchiveInfo("data/searches/default_2026-09-30.json", False, "ok"),
        assessment=None, pool=pool,
    )
    kwargs.update(overrides)
    return explain(stored(job), **kwargs)


def stage(result, name):
    return next(s for s in result.stages if s.name == name)


def test_a_gate_rejection_is_reported_with_its_rule_and_the_empty_department():
    result = run()
    assert [s.name for s in result.stages] == [
        "collected", "U.S.-eligible", "recency", "prefilter", "in archive", "assessed",
    ]
    prefilter = stage(result, "prefilter")
    assert prefilter.ok is False
    assert "no_positive_match" in prefilter.detail and "department is empty" in prefilter.detail
    assert result.verdict.startswith("Stopped at: prefilter")
    assert stage(result, "collected").ok is True
    assert "ok (842 jobs)" in stage(result, "collected").detail


def test_suggestions_list_real_substrings_with_exact_gain_and_preview_commands():
    result = run()
    terms = {s.term: s for s in result.suggestions}
    assert "test supervisor" in terms and "calibration" in terms
    assert terms["test supervisor"].gain == 2      # this job and the ABB one
    assert terms["calibration"].gain == 2          # this job and the Apple one
    assert "calibration test" not in terms         # not a substring of the real title ('&' between)
    assert all(s.term in "vehicle calibration & test supervisor" for s in result.suggestions)
    assert result.suggestions == sorted(result.suggestions, key=lambda s: (s.gain, -len(s.term), s.term))
    assert 'uv run python scripts/diff_profile.py --add "target_title_terms:test supervisor"' in result.preview_commands


def test_broad_suggestions_are_flagged():
    result = run(pool=pool)
    broad = suggest_terms(make_job(), PROFILE, pool(), broad_threshold=1)
    assert any(s.broad for s in broad)
    assert not any(s.broad for s in result.suggestions)


def test_no_suggestions_for_other_rules_or_with_a_keyword_override():
    assert run(job=make_job(title="Robotics Supervisor")).suggestions == []
    assert run(keywords=["adas"]).suggestions == []


def test_a_closed_job_stops_at_collected():
    result = explain(
        stored(status="closed", missing=3), profile=PROFILE, max_age_days=30, now=NOW,
        source_health=None, archive=None, assessment=None, pool=pool,
    )
    assert result.verdict.startswith("Stopped at: collected")
    assert "missed 3 run(s)" in stage(result, "collected").detail


def test_stale_posting_and_ineligible_location_are_reported():
    old = run(job=make_job(posted_at=datetime(2020, 1, 1, tzinfo=UTC)))
    assert stage(old, "recency").ok is False
    foreign = run(job=make_job(us_eligible=False, location_evidence="Germany"))
    assert stage(foreign, "U.S.-eligible").ok is False and "Germany" in stage(foreign, "U.S.-eligible").detail


def test_job_that_passes_but_is_missing_from_an_archive_whose_source_failed():
    job = make_job(title="Robotics Supervisor")
    result = run(job=job, archive=ArchiveInfo("data/searches/default_2026-09-30.json", False, "failed"))
    assert stage(result, "prefilter").ok is True
    assert stage(result, "in archive").ok is False
    assert "stale-source fallback" in stage(result, "in archive").detail
    assert result.verdict.startswith("Stopped at: in archive")


def test_assessment_states():
    job = make_job(title="Robotics Supervisor")
    in_archive = ArchiveInfo("a.json", True, "ok")
    scored = run(job=job, archive=in_archive, assessment={"score": 62, "content_hash": "h1"})
    assert stage(scored, "assessed").ok is True and "62" in stage(scored, "assessed").detail
    assert "should appear" in scored.verdict
    stale = run(job=job, archive=in_archive, assessment={"score": 62, "content_hash": "OLD"})
    assert stage(stale, "assessed").ok is False and "changed" in stage(stale, "assessed").detail
    missing = run(job=job, archive=in_archive, assessment=None)
    assert stage(missing, "assessed").ok is False and "not assessed" in stage(missing, "assessed").detail


def test_no_archive_is_not_applicable_not_a_failure():
    result = run(job=make_job(title="Robotics Supervisor"), archive=None, assessment={"score": 70, "content_hash": "h1"})
    assert stage(result, "in archive").ok is None
    assert "No search archive" in result.verdict


def test_to_dict_is_json_serializable_and_render_text_names_every_stage():
    result = run()
    json.dumps(result.to_dict())
    text = render_text(result)
    for name in ("collected", "U.S.-eligible", "recency", "prefilter", "in archive", "assessed"):
        assert name in text
    assert "Verdict: Stopped at: prefilter" in text
    assert "test supervisor" in text and "job-feedback" in text
    assert "Nothing was changed" in text


def test_suggestions_for_a_job_also_blocked_earlier_are_annotated():
    result = explain(
        stored(status="closed", missing=3), profile=PROFILE, max_age_days=30, now=NOW,
        source_health=None, archive=None, assessment=None, pool=pool,
    )
    assert result.suggestions  # still useful vocabulary
    assert result.also_blocked_by == ["collected"]
    assert "also blocked by collected" in render_text(result)
    assert run().also_blocked_by == []
    assert "also blocked by" not in render_text(run())


def test_gain_text_says_including_this_one_only_when_the_job_is_in_the_pool():
    assert "including this one" in render_text(run())
    others = lambda: [j for j in pool() if j.source_key != "ford"]  # noqa: E731
    result = run(pool=others)
    assert result.job_in_pool is False
    assert "including this one" not in render_text(result)


def test_failed_source_detail_says_the_fallback_needs_the_filters_to_pass():
    result = run(job=make_job(title="Robotics Supervisor"), archive=ArchiveInfo("a.json", False, "failed"))
    assert "if it passes the filters" in stage(result, "in archive").detail


def test_why_missed_uses_the_shared_broad_threshold():
    import inspect

    from job_hunter import why_missed
    from job_hunter.vocabulary import BROAD_TERM_THRESHOLD

    assert inspect.signature(why_missed.suggest_terms).parameters["broad_threshold"].default == BROAD_TERM_THRESHOLD
    assert inspect.signature(why_missed.explain).parameters["broad_threshold"].default == BROAD_TERM_THRESHOLD
