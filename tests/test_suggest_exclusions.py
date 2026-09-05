import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from suggest_exclusions import _build_title_sets, _collect_candidates  # noqa: E402


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
