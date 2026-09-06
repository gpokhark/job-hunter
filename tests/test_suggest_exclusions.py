import sys
from pathlib import Path

from job_hunter.config import CandidateProfile
from job_hunter.models import Job, LocationConfidence
from job_hunter.prefilter import evaluate_prefilter
from job_hunter.storage import Storage

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from suggest_exclusions import (  # noqa: E402
    FeedbackDecision,
    _build_title_sets,
    _collect_candidates,
    _drop_already_present,
    _feedback_decisions,
    _load_all_jobs,
    _print_hard_exclude_removal_bucket,
    _print_not_fixable_bucket,
    _print_positive_gate_bucket,
    _print_rescue_caution_bucket,
    _print_strong_relevance_bucket,
)


def make_job(**updates):
    values = dict(
        source_key="acme", source_platform="test", company="Acme", job_id="1",
        title="ADAS Engineer", url="https://example.com/1",
        us_eligible=True, location_confidence=LocationConfidence.HIGH,
    )
    values.update(updates)
    return Job(**values)


def test_generic_term_rejected_due_to_one_protected_collision():
    """"simulation" looks specific to the irrelevant batch but also appears in a
    protected (already-good) title — must be rejected, not suggested."""
    irrelevant = [
        "CAD Automation and Mixed-Signal Simulation Engineer",
        "Staff High Voltage System Architect and Simulation Engineer",
    ]
    protected = ["Software Engineer - Simulation Validation"]
    high_confidence, _below, _examples = _collect_candidates(irrelevant, protected, min_support=2)
    assert "simulation" not in {term for term, _ in high_confidence}


def test_specific_multiword_phrase_survives():
    irrelevant = [
        "Verification Platform Engineer, Platform Architecture",
        "Simulation and Control Systems Engineer - Platform Architecture",
    ]
    protected = ["Senior Robotics Engineer", "ADAS Systems Engineer for Perception"]
    high_confidence, _below, _examples = _collect_candidates(irrelevant, protected, min_support=2)
    assert "platform architecture" in {term for term, _ in high_confidence}


def test_below_threshold_terms_shown_separately_from_high_confidence():
    irrelevant = ["CPU Verification Engineer"]  # unique — no repeat
    protected: list[str] = []
    high_confidence, below_threshold, _examples = _collect_candidates(
        irrelevant, protected, min_support=2
    )
    assert high_confidence == []
    assert "cpu verification" in {term for term, _ in below_threshold}


def test_irrelevant_label_overrides_that_jobs_own_stale_assessment_score():
    """Regression for a real bug found against live data: a job tagged irrelevant but still
    sitting at its old >=50 score must not count as its own protected collision — the label
    corrects the score, it doesn't compete with it. Without this, a phrase unique to that
    job's title could never be suggested, since it would always collide with itself."""
    feedback_rows = [
        {
            "source_key": "apple", "job_id": "1",
            "title": "Verification Platform Engineer, Platform Architecture",
            "label": "irrelevant",
        },
    ]
    assessment_rows = [
        {
            "source_key": "apple", "job_id": "1",
            "title": "Verification Platform Engineer, Platform Architecture",
            "score": 68,
        },
    ]
    irrelevant_titles, protected_titles = _build_title_sets(feedback_rows, assessment_rows)
    assert irrelevant_titles == ["Verification Platform Engineer, Platform Architecture"]
    assert protected_titles == []


def test_untagged_job_influences_neither_irrelevant_nor_protected_sets():
    """A job with no job_feedback row and a score below 50 must not appear in, or
    influence, either list — absence of feedback is never itself a signal."""
    feedback_rows = [
        {"source_key": "x", "job_id": "1", "title": "Really Irrelevant Role", "label": "irrelevant"},
    ]
    assessment_rows = [
        # no feedback, score < 50
        {"source_key": "x", "job_id": "2", "title": "Untagged Low-Score Role", "score": 30},
        # no feedback, score >= 50
        {"source_key": "x", "job_id": "3", "title": "Untagged High-Score Role", "score": 82},
    ]
    irrelevant_titles, protected_titles = _build_title_sets(feedback_rows, assessment_rows)
    assert irrelevant_titles == ["Really Irrelevant Role"]
    assert protected_titles == ["Untagged High-Score Role"]
    assert "Untagged Low-Score Role" not in protected_titles
    assert "Untagged Low-Score Role" not in irrelevant_titles


# --- multi-field suggestions (docs/feedback-exclusion-plan.md §13) ---


def test_drop_already_present_filters_case_insensitively():
    # _collect_candidates always yields lowercase terms; `existing` (profile.<field> as
    # written in the YAML) may not be — the comparison must still catch it.
    high = [("foo", 3), ("bar", 2)]
    below = [("baz", 1)]
    new_high, new_below = _drop_already_present(high, below, existing=["FOO"])
    assert new_high == [("bar", 2)]
    assert new_below == [("baz", 1)]


def test_feedback_decisions_recompute_against_current_profile(tmp_path):
    """The whole point of the extension: a feedback row's decision is recomputed fresh
    against the CURRENT profile, not derived from a stale score."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(job_id="1", title="Perception Engineer"))
    profile = CandidateProfile(target_domains=["perception"])
    jobs_by_key = _load_all_jobs(db_path)
    feedback_rows = [{"source_key": "acme", "job_id": "1", "label": "relevant"}]
    decisions = _feedback_decisions(feedback_rows, jobs_by_key, profile)
    assert len(decisions) == 1
    assert decisions[0].decision.passes is True


def test_feedback_decisions_skips_job_purged_from_storage(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path):
        pass
    jobs_by_key = _load_all_jobs(db_path)
    feedback_rows = [{"source_key": "acme", "job_id": "missing", "label": "relevant"}]
    decisions = _feedback_decisions(feedback_rows, jobs_by_key, CandidateProfile())
    assert decisions == []


def test_print_hard_exclude_removal_bucket_flags_relevant_job(tmp_path, capsys):
    """The highest-severity case: exclude_title_terms/exclude_terms have no rescue
    mechanism at all, so a real match blocked by one is a pure false negative."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path):
        pass
    profile = CandidateProfile(exclude_title_terms=["intern"])
    job = make_job(job_id="1", title="Perception Intern Program")
    decisions = [FeedbackDecision(job=job, label="relevant", decision=evaluate_prefilter(job, profile))]
    _print_hard_exclude_removal_bucket(decisions, profile=profile, database_path=db_path, max_age_days=30)
    out = capsys.readouterr().out
    assert 'REMOVE "intern" from exclude_title_terms' in out
    assert "Perception Intern Program" in out


def test_print_hard_exclude_removal_bucket_empty_when_no_match(tmp_path, capsys):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path):
        pass
    profile = CandidateProfile()
    job = make_job(job_id="1", title="ADAS Engineer")
    decisions = [FeedbackDecision(job=job, label="relevant", decision=evaluate_prefilter(job, profile))]
    _print_hard_exclude_removal_bucket(decisions, profile=profile, database_path=db_path, max_age_days=30)
    out = capsys.readouterr().out
    assert "none — no relevant/okay feedback on a hard-excluded job" in out


def test_print_positive_gate_bucket_suggests_new_positive_term(tmp_path, capsys):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path):
        pass
    profile = CandidateProfile(target_domains=["ADAS"])
    j1 = make_job(job_id="1", title="Perception Systems Engineer")
    j2 = make_job(job_id="2", title="Perception Systems Lead")
    decisions = [
        FeedbackDecision(job=j1, label="relevant", decision=evaluate_prefilter(j1, profile)),
        FeedbackDecision(job=j2, label="okay", decision=evaluate_prefilter(j2, profile)),
    ]
    _print_positive_gate_bucket(
        decisions, irrelevant_titles=[], profile=profile, database_path=db_path, max_age_days=30, min_support=2
    )
    out = capsys.readouterr().out
    assert 'ADD "perception' in out
    assert "target_domains" in out


def test_print_strong_relevance_bucket_suggests_rescue_term(tmp_path, capsys):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path):
        pass
    profile = CandidateProfile(target_domains=["ADAS"], soft_exclude_terms=["validation"])
    j1 = make_job(job_id="1", title="ADAS Camera Validation Engineer")
    j2 = make_job(job_id="2", title="ADAS Camera Validation Lead")
    decisions = [
        FeedbackDecision(job=j1, label="relevant", decision=evaluate_prefilter(j1, profile)),
        FeedbackDecision(job=j2, label="okay", decision=evaluate_prefilter(j2, profile)),
    ]
    _print_strong_relevance_bucket(
        decisions, irrelevant_titles=[], profile=profile, database_path=db_path, max_age_days=30, min_support=2
    )
    out = capsys.readouterr().out
    assert 'ADD "camera' in out
    assert "strong_relevance_terms" in out


def test_print_rescue_caution_bucket_flags_irrelevant_rescued_job(capsys):
    """No auto-suggestion here — just a loud, specific call-out that a strong_relevance_terms
    word rescued a job the human separately marked irrelevant."""
    profile = CandidateProfile(
        target_domains=["ADAS"], soft_exclude_terms=["validation"], strong_relevance_terms=["camera"]
    )
    job = make_job(job_id="1", title="ADAS Camera Validation Engineer")
    decisions = [FeedbackDecision(job=job, label="irrelevant", decision=evaluate_prefilter(job, profile))]
    _print_rescue_caution_bucket(decisions)
    out = capsys.readouterr().out
    assert '"camera" rescued' in out
    assert "ADAS Camera Validation Engineer" in out


def test_print_not_fixable_bucket_lists_non_us_eligible_relevant_jobs(capsys):
    profile = CandidateProfile()
    job = make_job(job_id="1", title="ADAS Engineer", us_eligible=False)
    decisions = [FeedbackDecision(job=job, label="relevant", decision=evaluate_prefilter(job, profile))]
    _print_not_fixable_bucket(decisions)
    out = capsys.readouterr().out
    assert "not us_eligible" in out
    assert "ADAS Engineer" in out
